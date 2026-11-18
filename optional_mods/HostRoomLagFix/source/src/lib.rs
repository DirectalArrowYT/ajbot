//! ajdirect: send each fighter's inputs straight to the other fighter.
//!
//! In a room where the host only watches, both fighters are clients of the host. Stock AJ sends every input
//! fighter A -> host -> fighter B (ServerTransitKeyHistory, then the host forwards ClientTransitKeyHistory), so
//! each input crosses two network legs plus a host frame, and the lockstep stalls whenever it is late.
//! This mod (on both fighters) also sends the exact key-history array A gives the host directly to B, and B feeds
//! it to the game through the same ClientTransitKeyHistory_Implementation the host's copy uses. The game keeps only
//! frames it doesn't have yet, so whichever copy comes first wins and the later one is ignored. The host path stays
//! as it is, so a dropped direct packet costs nothing.
//!
//! Safety: a peer is used only after its inputs over a 240-frame window hash-match the copy the host delivered for
//! the same frames (so packets from other matches are never used), and every directly fed frame is compared with
//! the host's copy when that arrives (mismatches are logged). Does nothing on the host (listen server).
//!
//! Transports: UDP (`peer=ip:port` lines in ajdirect.ini, `listen=port`) and Steam networking messages to Steam
//! friends who are playing AJ (`steam=1`, default on).
use std::collections::VecDeque;
use std::ffi::c_void;
use std::io::Write;
use std::net::{SocketAddr, UdpSocket};
use std::sync::Mutex;
use windows_sys::Win32::Foundation::{BOOL, HMODULE};
use windows_sys::Win32::System::Diagnostics::Debug::FlushInstructionCache;
use windows_sys::Win32::System::LibraryLoader::{GetModuleFileNameW, GetModuleHandleA, GetModuleHandleW, GetProcAddress};
use windows_sys::Win32::System::Memory::{
    VirtualAlloc, VirtualProtect, MEM_COMMIT, MEM_RESERVE, PAGE_EXECUTE_READWRITE, PAGE_PROTECTION_FLAGS,
};
use windows_sys::Win32::System::Threading::GetCurrentProcess;

const NAME: &str = "ajdirect";
// RVAs in AJGAME.exe (Steam build, md5 d2f75cc86fe560bdbbd7ac7f36212967)
const RVA_SEND_SERVER: usize = 0x5556300; // ServerTransitKeyHistory send wrapper (this, const TArray<Key>&)
const RVA_TICK_SEND: usize = 0x5e50990; // battle controller TickActor(this, dt, type, fn): once per game frame
const RVA_CLIENT_IMPL: usize = 0x5e3bc10; // ClientTransitKeyHistory_Implementation (this, const TArray<Key>&)
const RVA_GET_NET_MODE: usize = 0x3448400; // AActor::GetNetMode
const OFF_STEP: usize = 0x8a4; // controller: lockstep frame being stepped
const PRO_SEND: &[u8] = &[0x48, 0x89, 0x5c, 0x24, 0x08];
const PRO_TICK: &[u8] = &[0x40, 0x53, 0x56, 0x57, 0x48, 0x81, 0xec, 0x90, 0x01, 0x00, 0x00];
const PRO_CLIENT: &[u8] = &[0x40, 0x56, 0x41, 0x56, 0x41, 0x57];

const WIN: usize = 240; // frames per bond-check window
const CHAN: i32 = 0x414a44;
const MAGIC: &[u8; 4] = b"AJD1";

type Fn2 = unsafe extern "system" fn(*mut u8, *const TArr) -> usize;
type NetMode = unsafe extern "system" fn(*mut u8) -> u32;

#[repr(C)]
struct TArr { ptr: *mut u8, num: i32, max: i32 }

#[repr(C)]
struct Entry { hdr: u64, n: i32, pad: i32, inner: TArr }

static mut ORIG_SEND: usize = 0;
static mut ORIG_TICK: usize = 0;
static mut ORIG_CLIENT: usize = 0;
static mut GET_NET_MODE: usize = 0;
static mut IN_INJECT: bool = false;

fn exe_dir() -> std::path::PathBuf {
    unsafe {
        let mut buf = [0u16; 1024];
        let n = GetModuleFileNameW(std::ptr::null_mut(), buf.as_mut_ptr(), buf.len() as u32) as usize;
        let exe = std::path::PathBuf::from(String::from_utf16_lossy(&buf[..n]));
        exe.parent().map(|p| p.to_path_buf()).unwrap_or_default()
    }
}

/// "ajdirect", or "ajdirect-N" when the test rig runs several copies from one folder (AJ_INSTANCE=N)
fn stem() -> String {
    match std::env::var("AJ_INSTANCE") { Ok(n) if !n.is_empty() && n != "0" => format!("{NAME}-{n}"), _ => NAME.to_string() }
}

fn log(s: &str) {
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(exe_dir().join(format!("{}.log", stem()))) {
        let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
        let _ = writeln!(f, "{t:.3} {s}");
    }
}

// ---------------------------------------------------------------- per-player input record
/// Inputs of one player by frame: items[i] = 12-byte key of frame base+i. Same append rule as the game's store
/// (0x145e3bfa0): an entry ending at frame f with n items covers frames f-n+1..=f.
#[derive(Default)]
struct Rec { base: u32, items: Vec<[u8; 12]>, at: Vec<u64> } // at = tick the frame first arrived

impl Rec {
    fn end(&self) -> u32 { self.base + self.items.len() as u32 }
    fn clear(&mut self) { self.base = 0; self.items.clear(); self.at.clear(); }
    /// returns number of new frames
    fn add(&mut self, f: u32, data: &[[u8; 12]], tick: u64) -> usize {
        if data.is_empty() { return 0; }
        let first = (f + 1).saturating_sub(data.len() as u32);
        if self.items.is_empty() || first > self.end() || f + 600 < self.end() {
            self.clear();
            self.base = first;
        }
        let end = self.end();
        if f < end { return 0; }
        let new = (f - end + 1) as usize;
        let start = data.len() - new.min(data.len());
        for d in &data[start..] { self.items.push(*d); self.at.push(tick); }
        new
    }
    fn get(&self, f: u32) -> Option<&[u8; 12]> {
        if f < self.base { return None; }
        self.items.get((f - self.base) as usize)
    }
    /// FNV-1a over frames f-WIN+1..=f, only if all present and not all identical
    fn hash(&self, f: u32) -> Option<u64> {
        if f + 1 < WIN as u32 { return None; }
        let lo = f + 1 - WIN as u32;
        if lo < self.base || f >= self.end() { return None; }
        let s = &self.items[(lo - self.base) as usize..=(f - self.base) as usize];
        if s.iter().all(|x| x == &s[0]) { return None; }
        let mut h: u64 = 0xcbf29ce484222325;
        for x in s { for b in x { h ^= *b as u64; h = h.wrapping_mul(0x100000001b3); } }
        Some(h)
    }
}

// ---------------------------------------------------------------- packets
struct Pkt { k: u8, checks: Vec<(u32, u64)>, entries: Vec<(u64, i32, Vec<[u8; 12]>)> }

fn parse_entries(arr: &TArr) -> Vec<(u64, i32, Vec<[u8; 12]>)> {
    let mut v = Vec::new();
    if arr.ptr.is_null() || arr.num <= 0 || arr.num > 64 { return v; }
    unsafe {
        for i in 0..arr.num as usize {
            let e = &*(arr.ptr.add(i * 32) as *const Entry);
            let mut items = Vec::new();
            if !e.inner.ptr.is_null() && e.inner.num > 0 && e.inner.num <= 4096 {
                for j in 0..e.inner.num as usize {
                    let mut x = [0u8; 12];
                    std::ptr::copy_nonoverlapping(e.inner.ptr.add(j * 12), x.as_mut_ptr(), 12);
                    items.push(x);
                }
            }
            v.push((e.hdr, e.n, items));
        }
    }
    v
}

fn hdr_frame(h: u64) -> u32 { (h as u32) & 0xffffff }
fn hdr_mask(h: u64) -> u8 { (h >> 56) as u8 }

fn encode(p: &Pkt) -> Vec<u8> {
    let mut b = Vec::with_capacity(512);
    b.extend_from_slice(MAGIC);
    b.push(p.k);
    b.push(p.checks.len() as u8);
    b.extend_from_slice(&(p.entries.len() as u16).to_le_bytes());
    for (f, h) in &p.checks { b.extend_from_slice(&f.to_le_bytes()); b.extend_from_slice(&h.to_le_bytes()); }
    for (h, n, items) in &p.entries {
        b.extend_from_slice(&h.to_le_bytes());
        b.extend_from_slice(&n.to_le_bytes());
        b.extend_from_slice(&(items.len() as u32).to_le_bytes());
        for x in items { b.extend_from_slice(x); }
    }
    b
}

fn decode(b: &[u8]) -> Option<Pkt> {
    if b.len() < 8 || &b[0..4] != MAGIC { return None; }
    let k = b[4];
    let nc = b[5] as usize;
    let ne = u16::from_le_bytes([b[6], b[7]]) as usize;
    if k > 1 || nc > 8 || ne > 64 { return None; }
    let mut o = 8;
    let rd = |o: &mut usize, n: usize| -> Option<&[u8]> { let s = b.get(*o..*o + n)?; *o += n; Some(s) };
    let mut checks = Vec::new();
    for _ in 0..nc {
        let f = u32::from_le_bytes(rd(&mut o, 4)?.try_into().ok()?);
        let h = u64::from_le_bytes(rd(&mut o, 8)?.try_into().ok()?);
        checks.push((f, h));
    }
    let mut entries = Vec::new();
    for _ in 0..ne {
        let h = u64::from_le_bytes(rd(&mut o, 8)?.try_into().ok()?);
        let n = i32::from_le_bytes(rd(&mut o, 4)?.try_into().ok()?);
        let c = u32::from_le_bytes(rd(&mut o, 4)?.try_into().ok()?) as usize;
        if c > 4096 { return None; }
        let mut items = Vec::with_capacity(c);
        for _ in 0..c { items.push(rd(&mut o, 12)?.try_into().ok()?); }
        entries.push((h, n, items));
    }
    Some(Pkt { k, checks, entries })
}

// ---------------------------------------------------------------- Steam
#[derive(Clone, Copy, PartialEq, Debug)]
enum Peer { Udp(SocketAddr), Steam(u64) }

struct Steam {
    msgs: usize,
    send: unsafe extern "C" fn(usize, *const u8, *const u8, u32, i32, i32) -> i32,
    recv: unsafe extern "C" fn(usize, i32, *mut *mut u8, i32) -> i32,
    release: unsafe extern "C" fn(*mut u8),
    friends: usize,
    fcount: unsafe extern "C" fn(usize, i32) -> i32,
    fbyidx: unsafe extern "C" fn(usize, i32, i32) -> u64,
    fgame: unsafe extern "C" fn(usize, u64, *mut [u64; 3]) -> bool,
    appid: u32,
}

unsafe fn steam_init() -> Result<Steam, String> {
    let m = GetModuleHandleA(b"steam_api64.dll\0".as_ptr());
    if m.is_null() { return Err("steam_api64.dll not loaded".into()); }
    macro_rules! f { ($n:expr) => { match GetProcAddress(m, concat!($n, "\0").as_ptr()) { Some(p) => p as usize, None => return Err(format!("no {}", $n)) } } }
    let msgs_get: unsafe extern "C" fn() -> usize = std::mem::transmute(f!("SteamAPI_SteamNetworkingMessages_SteamAPI_v002"));
    let friends_get: unsafe extern "C" fn() -> usize = std::mem::transmute(f!("SteamAPI_SteamFriends_v017"));
    let utils_get: unsafe extern "C" fn() -> usize = std::mem::transmute(f!("SteamAPI_SteamUtils_v010"));
    let getapp: unsafe extern "C" fn(usize) -> u32 = std::mem::transmute(f!("SteamAPI_ISteamUtils_GetAppID"));
    let s = Steam {
        msgs: 0,
        send: std::mem::transmute(f!("SteamAPI_ISteamNetworkingMessages_SendMessageToUser")),
        recv: std::mem::transmute(f!("SteamAPI_ISteamNetworkingMessages_ReceiveMessagesOnChannel")),
        release: std::mem::transmute(f!("SteamAPI_SteamNetworkingMessage_t_Release")),
        friends: 0,
        fcount: std::mem::transmute(f!("SteamAPI_ISteamFriends_GetFriendCount")),
        fbyidx: std::mem::transmute(f!("SteamAPI_ISteamFriends_GetFriendByIndex")),
        fgame: std::mem::transmute(f!("SteamAPI_ISteamFriends_GetFriendGamePlayed")),
        appid: 0,
    };
    let msgs = msgs_get();
    let friends = friends_get();
    let utils = utils_get();
    if msgs == 0 || friends == 0 || utils == 0 { return Err("Steam interfaces not ready".into()); }
    Ok(Steam { msgs, friends, appid: getapp(utils), ..s })
}

fn identity(id: u64) -> [u8; 136] {
    let mut b = [0u8; 136];
    b[0..4].copy_from_slice(&16i32.to_le_bytes()); // k_ESteamNetworkingIdentityType_SteamID
    b[4..8].copy_from_slice(&8i32.to_le_bytes());
    b[8..16].copy_from_slice(&id.to_le_bytes());
    b
}

// ---------------------------------------------------------------- state
struct Cfg { enabled: bool, steam: bool, listen: u16, peers: Vec<SocketAddr>, steam_peers: Vec<u64>, stats_every: u64 }

fn read_cfg() -> Cfg {
    let mut c = Cfg { enabled: true, steam: true, listen: 7790, peers: vec![], steam_peers: vec![], stats_every: 600 };
    if let Ok(t) = std::fs::read_to_string(exe_dir().join(format!("{}.ini", stem()))) {
        for line in t.lines() {
            let line = line.trim();
            if line.starts_with('#') || line.starts_with(';') { continue; }
            let Some((k, v)) = line.split_once('=') else { continue };
            let (k, v) = (k.trim(), v.trim());
            match k {
                "enabled" => c.enabled = v != "0",
                "steam" => c.steam = v != "0",
                "listen" => c.listen = v.parse().unwrap_or(c.listen),
                "peer" => { if let Ok(a) = v.parse() { c.peers.push(a) } }
                "steam_peer" => { if let Ok(a) = v.parse() { c.steam_peers.push(a) } }
                "stats_every" => c.stats_every = v.parse().unwrap_or(c.stats_every).max(60),
                _ => {}
            }
        }
    }
    c
}

#[derive(Default)]
struct Stats { ticks: u64, stall: u64, sent: u64, rx: u64, rx_bad: u64, injected_frames: u64, mismatch: u64,
               compared: u64, lead_sum: i64, lead_n: u64, lead_pos: u64 }

struct St {
    cfg: Cfg,
    udp: Option<UdpSocket>,
    steam: Option<Steam>,
    steam_cands: Vec<u64>,
    ctrl: usize,
    own_k: Option<u8>,
    own: Rec,
    host: [Rec; 2],   // inputs the host delivered (stock path)
    direct: [Rec; 2], // inputs we fed from the peer
    bonded: Option<Peer>,
    queue: VecDeque<(Peer, Pkt)>,
    tick: u64,
    last_step: u32,
    st: Stats,
}

static STATE: Mutex<Option<St>> = Mutex::new(None);

impl St {
    fn reset_match(&mut self, why: &str) {
        if self.st.ticks > 0 { self.flush_stats(why); }
        self.own.clear();
        for r in self.host.iter_mut().chain(self.direct.iter_mut()) { r.clear(); }
        self.own_k = None;
        if self.bonded.take().is_some() { log("unbonded (new match)"); }
        self.queue.clear();
        self.st = Stats::default();
    }

    fn flush_stats(&mut self, why: &str) {
        let s = &self.st;
        let lead = if s.lead_n > 0 { s.lead_sum as f64 / s.lead_n as f64 } else { 0.0 };
        log(&format!("stats[{why}] ticks {} stall_ticks {} ({:.1}%) sent {} rx {} rx_rejected {} fed_frames {} \
compared {} MISMATCH {} direct_lead_ticks avg {:.2} (direct first on {}/{} frames) bonded {:?}",
            s.ticks, s.stall, 100.0 * s.stall as f64 / s.ticks.max(1) as f64, s.sent, s.rx, s.rx_bad, s.injected_frames,
            s.compared, s.mismatch, lead, s.lead_pos, s.lead_n, self.bonded));
    }

    fn peers(&self) -> Vec<Peer> {
        let mut v: Vec<Peer> = self.cfg.peers.iter().map(|a| Peer::Udp(*a)).collect();
        // Bonded to a Steam peer: send only to it (until a window check fails), not to every friend in AJ.
        if let Some(Peer::Steam(id)) = self.bonded { v.push(Peer::Steam(id)); return v; }
        for id in self.cfg.steam_peers.iter().chain(self.steam_cands.iter()) {
            if !v.contains(&Peer::Steam(*id)) { v.push(Peer::Steam(*id)); }
        }
        v
    }

    fn send_to(&mut self, p: Peer, b: &[u8]) {
        match p {
            Peer::Udp(a) => { if let Some(u) = &self.udp { let _ = u.send_to(b, a); } }
            Peer::Steam(id) => {
                if let Some(s) = &self.steam {
                    let ident = identity(id);
                    unsafe { (s.send)(s.msgs, ident.as_ptr(), b.as_ptr(), b.len() as u32, 1 | 32, CHAN); }
                }
            }
        }
        self.st.sent += 1;
    }

    fn refresh_friends(&mut self) {
        let Some(s) = &self.steam else { return };
        let mut v = Vec::new();
        unsafe {
            let n = (s.fcount)(s.friends, 4);
            for i in 0..n.clamp(0, 2000) {
                let id = (s.fbyidx)(s.friends, i, 4);
                let mut info = [0u64; 3];
                if (s.fgame)(s.friends, id, &mut info) && (info[0] & 0xffffff) as u32 == s.appid && s.appid != 0 {
                    v.push(id);
                }
            }
        }
        if v != self.steam_cands { log(&format!("steam friends in AJ: {}", v.len())); }
        self.steam_cands = v;
    }

    fn poll(&mut self) {
        let mut buf = [0u8; 65536];
        if let Some(u) = &self.udp {
            let mut got = Vec::new();
            for _ in 0..256 {
                match u.recv_from(&mut buf) { Ok((n, a)) => got.push((Peer::Udp(a), buf[..n].to_vec())), Err(_) => break }
            }
            for (p, b) in got { self.take(p, &b); }
        }
        if let Some(s) = &self.steam {
            let mut got = Vec::new();
            unsafe {
                let mut ptrs = [std::ptr::null_mut::<u8>(); 64];
                let n = (s.recv)(s.msgs, CHAN, ptrs.as_mut_ptr(), 64);
                for &m in ptrs.iter().take(n.clamp(0, 64) as usize) {
                    if m.is_null() { continue; }
                    let data = *(m as *const *const u8);
                    let len = *(m.add(8) as *const i32);
                    let id = *(m.add(24) as *const u64);
                    if !data.is_null() && len > 0 && len < 65536 {
                        got.push((Peer::Steam(id), std::slice::from_raw_parts(data, len as usize).to_vec()));
                    }
                    (s.release)(m);
                }
            }
            for (p, b) in got { self.take(p, &b); }
        }
    }

    fn take(&mut self, p: Peer, b: &[u8]) {
        self.st.rx += 1;
        let Some(pk) = decode(b) else { self.st.rx_bad += 1; return };
        if Some(pk.k) == self.own_k || self.own_k.is_none() { self.st.rx_bad += 1; return; }
        // every entry must be for the sender's player only
        if pk.entries.iter().any(|e| hdr_mask(e.0) != 1 << pk.k) { self.st.rx_bad += 1; return; }
        // bond check against what the host delivered for that player
        let host = &self.host[pk.k as usize];
        let mut ok = 0; let mut bad = 0;
        for (f, h) in &pk.checks { if let Some(mine) = host.hash(*f) { if mine == *h { ok += 1 } else { bad += 1 } } }
        if bad > 0 {
            self.st.rx_bad += 1;
            if self.bonded == Some(p) { log(&format!("peer {p:?} failed a window check: unbonding")); self.bonded = None; }
            return;
        }
        if self.bonded.is_none() && ok > 0 {
            self.bonded = Some(p);
            log(&format!("bonded to {p:?} (player {}), host data through frame {}", pk.k, host.end()));
        }
        if self.bonded != Some(p) { self.st.rx_bad += 1; return; }
        self.queue.push_back((p, pk));
    }

    /// feed queued direct packets to the game (game thread, inside TickActor)
    unsafe fn inject(&mut self) {
        while let Some((_, pk)) = self.queue.pop_front() {
            let k = pk.k as usize;
            // own record first, to count new frames and arrival ticks
            let mut new = 0;
            for (h, _, items) in &pk.entries { new += self.direct[k].add(hdr_frame(*h), items, self.tick); }
            if new == 0 { continue; }
            let mut entries: Vec<Entry> = pk.entries.iter().map(|(h, n, items)| Entry {
                hdr: *h, n: *n, pad: 0,
                inner: TArr { ptr: items.as_ptr() as *mut u8, num: items.len() as i32, max: items.len() as i32 },
            }).collect();
            let arr = TArr { ptr: entries.as_mut_ptr() as *mut u8, num: entries.len() as i32, max: entries.len() as i32 };
            let orig: Fn2 = std::mem::transmute(ORIG_CLIENT);
            IN_INJECT = true;
            orig(self.ctrl as *mut u8, &arr);
            IN_INJECT = false;
            self.st.injected_frames += new as u64;
            drop(entries);
        }
    }
}

fn with<R>(f: impl FnOnce(&mut St) -> R) -> Option<R> {
    let mut g = STATE.lock().ok()?;
    g.as_mut().map(f)
}

unsafe fn is_client(this: *mut u8) -> bool {
    let f: NetMode = std::mem::transmute(GET_NET_MODE);
    f(this) == 3
}

// ---------------------------------------------------------------- hooks
unsafe extern "system" fn hook_send(this: *mut u8, keys: *const TArr) -> usize {
    if !keys.is_null() && is_client(this) {
        let entries = parse_entries(&*keys);
        with(|s| {
            if s.ctrl != this as usize { s.reset_match("new controller"); s.ctrl = this as usize; }
            for (h, _, items) in &entries {
                let m = hdr_mask(*h);
                let k = match m { 1 => 0u8, 2 => 1u8, _ => continue };
                if s.own_k != Some(k) { s.own_k = Some(k); log(&format!("local player {k}")); }
                let f = hdr_frame(*h);
                if f + 600 < s.own.end() { s.reset_match("own frames restarted"); s.own_k = Some(k); }
                s.own.add(f, items, s.tick);
            }
            if !s.cfg.enabled || s.own_k.is_none() { return; }
            let k = s.own_k.unwrap_or(0);
            let last = s.own.end().saturating_sub(1);
            let mut checks = Vec::new();
            let mut f = last / 60 * 60;
            while checks.len() < 4 && f >= WIN as u32 {
                if let Some(h) = s.own.hash(f) { checks.push((f, h)); }
                f -= 60;
            }
            let own: Vec<_> = entries.iter().filter(|e| hdr_mask(e.0) == 1 << k).cloned().collect();
            if own.is_empty() { return; }
            let b = encode(&Pkt { k, checks, entries: own });
            for p in s.peers() { s.send_to(p, &b); }
        });
    }
    let orig: Fn2 = std::mem::transmute(ORIG_SEND);
    orig(this, keys)
}

unsafe extern "system" fn hook_client(this: *mut u8, keys: *const TArr) -> usize {
    if !IN_INJECT && !keys.is_null() {
        let entries = parse_entries(&*keys);
        with(|s| {
            if this as usize != s.ctrl { return; }
            for (h, _, items) in &entries {
                let k = match hdr_mask(*h) { 1 => 0usize, 2 => 1usize, _ => continue };
                let f = hdr_frame(*h);
                let before = s.host[k].end();
                if f + 600 < before { s.reset_match("host frames restarted"); }
                let before = s.host[k].end();
                s.host[k].add(f, items, s.tick);
                // compare with what we fed directly, and measure how much earlier it came
                for fr in before..s.host[k].end() {
                    if let (Some(d), Some(hh)) = (s.direct[k].get(fr), s.host[k].get(fr)) {
                        s.st.compared += 1;
                        if d != hh {
                            s.st.mismatch += 1;
                            if s.st.mismatch <= 5 { log(&format!("MISMATCH player {k} frame {fr}: direct {d:02x?} host {hh:02x?}")); }
                        }
                        let ta = s.direct[k].at[(fr - s.direct[k].base) as usize];
                        let lead = s.tick as i64 - ta as i64;
                        s.st.lead_sum += lead; s.st.lead_n += 1;
                        if lead > 0 { s.st.lead_pos += 1; }
                    }
                }
            }
        });
    }
    let orig: Fn2 = std::mem::transmute(ORIG_CLIENT);
    orig(this, keys)
}

type FnTick = unsafe extern "system" fn(*mut u8, f32, u32, *mut u8);
unsafe extern "system" fn hook_tick(this: *mut u8, dt: f32, tt: u32, tf: *mut u8) {
    with(|s| {
        if this as usize != s.ctrl || s.ctrl == 0 { return; }
        if !is_client(this) { return; }
        s.tick += 1;
        s.st.ticks += 1;
        let step = *(this.add(OFF_STEP) as *const u32);
        if step == s.last_step { s.st.stall += 1; }
        s.last_step = step;
        if s.cfg.enabled {
            if s.cfg.steam && s.steam.is_none() && s.tick % 300 == 1 {
                match unsafe { steam_init() } {
                    Ok(st) => { log(&format!("steam ready, app {}", st.appid)); s.steam = Some(st); }
                    Err(e) => { if s.tick % 18000 == 1 { log(&format!("steam not available: {e}")); } }
                }
            }
            if s.cfg.steam && s.tick % 300 == 1 { s.refresh_friends(); }
            s.poll();
            s.inject();
        }
        if s.st.ticks % s.cfg.stats_every == 0 { s.flush_stats("periodic"); }
    });
    let orig: FnTick = std::mem::transmute(ORIG_TICK);
    orig(this, dt, tt, tf)
}

// ---------------------------------------------------------------- install
unsafe fn alloc_near(base: usize, size: usize) -> usize {
    let mut a = base + 0xb40_0000;
    while a < base + 0x7000_0000 {
        let p = VirtualAlloc(a as *const c_void, size, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE) as usize;
        if p != 0 { return p; }
        a += 0x1_0000;
    }
    0
}

/// Detour `site` (whose first bytes are `pro`, position independent) to `hook`. Returns the trampoline.
unsafe fn detour(site: usize, pro: &[u8], hook: usize, mem: &mut usize) -> usize {
    let cur = *mem;
    let w = |at: &mut usize, b: &[u8]| { std::ptr::copy_nonoverlapping(b.as_ptr(), *at as *mut u8, b.len()); *at += b.len(); };
    // trampoline: original prologue, then jmp [rip] back to site+len
    let tramp = cur;
    let mut p = cur;
    w(&mut p, pro);
    w(&mut p, &[0xff, 0x25, 0, 0, 0, 0]);
    w(&mut p, &((site + pro.len()) as u64).to_le_bytes());
    // relay: jmp [rip] hook
    let relay = p;
    w(&mut p, &[0xff, 0x25, 0, 0, 0, 0]);
    w(&mut p, &(hook as u64).to_le_bytes());
    *mem = (p + 15) & !15;
    let mut old: PAGE_PROTECTION_FLAGS = 0;
    VirtualProtect(site as *const c_void, 5, PAGE_EXECUTE_READWRITE, &mut old);
    let rel = (relay as i64 - (site as i64 + 5)) as i32;
    let mut j = [0xe9u8, 0, 0, 0, 0];
    j[1..].copy_from_slice(&rel.to_le_bytes());
    std::ptr::copy_nonoverlapping(j.as_ptr(), site as *mut u8, 5);
    VirtualProtect(site as *const c_void, 5, old, &mut old);
    FlushInstructionCache(GetCurrentProcess(), site as *const c_void, 5);
    tramp
}

unsafe fn install() -> Result<String, String> {
    let base = GetModuleHandleW(std::ptr::null()) as usize;
    if base == 0 { return Err("no exe module".into()); }
    let at = |rva: usize, n: usize| std::slice::from_raw_parts((base + rva) as *const u8, n);
    for (rva, pro) in [(RVA_SEND_SERVER, PRO_SEND), (RVA_TICK_SEND, PRO_TICK), (RVA_CLIENT_IMPL, PRO_CLIENT)] {
        if at(rva, pro.len()) != pro {
            return Err(format!("code at rva {rva:#x} differs (different game build?)"));
        }
    }
    let mut mem = alloc_near(base, 0x1000);
    if mem == 0 { return Err("no memory near the exe".into()); }
    let start = mem;
    GET_NET_MODE = base + RVA_GET_NET_MODE;
    ORIG_SEND = detour(base + RVA_SEND_SERVER, PRO_SEND, hook_send as *const () as usize, &mut mem);
    ORIG_CLIENT = detour(base + RVA_CLIENT_IMPL, PRO_CLIENT, hook_client as *const () as usize, &mut mem);
    ORIG_TICK = detour(base + RVA_TICK_SEND, PRO_TICK, hook_tick as *const () as usize, &mut mem);
    FlushInstructionCache(GetCurrentProcess(), start as *const c_void, 0x1000);
    Ok(format!("hooks at {start:#x}"))
}

fn setup() -> String {
    let cfg = read_cfg();
    let mut notes = Vec::new();
    // UDP only when peers are configured (LAN/testing); Steam play needs no open port.
    let udp = if cfg.enabled && !cfg.peers.is_empty() {
        match UdpSocket::bind(("0.0.0.0", cfg.listen)) {
            Ok(u) => { let _ = u.set_nonblocking(true); notes.push(format!("udp {}", cfg.listen)); Some(u) }
            Err(e) => { notes.push(format!("udp {} failed: {e}", cfg.listen)); None }
        }
    } else { None };
    notes.push(format!("enabled {} steam {} peers {:?} steam_peers {:?}", cfg.enabled, cfg.steam, cfg.peers, cfg.steam_peers));
    let st = St {
        cfg, udp, steam: None, steam_cands: vec![], ctrl: 0, own_k: None, own: Rec::default(),
        host: Default::default(), direct: Default::default(), bonded: None, queue: VecDeque::new(), tick: 0,
        last_step: 0, st: Stats::default(),
    };
    if let Ok(mut g) = STATE.lock() { *g = Some(st); }
    notes.join(", ")
}

// ---------------------------------------------------------------- crash diagnostics (logs only, never handles)
static mut EXC_LOGGED: u32 = 0;

unsafe extern "system" fn veh(ep: *mut c_void) -> i32 {
    let rec = *(ep as *const *const u8);
    let ctx = *((ep as *const *const u8).add(1));
    if rec.is_null() || ctx.is_null() { return 0; }
    let code = *(rec as *const u32);
    if code < 0xC000_0000 || code == 0xC000_0096 && false { return 0; }
    if EXC_LOGGED >= 20 { return 0; }
    EXC_LOGGED += 1;
    let addr = *(rec.add(0x10) as *const u64);
    let rip = *(ctx.add(0xF8) as *const u64);
    let rsp = *(ctx.add(0x98) as *const u64);
    let me = hook_send as *const () as u64;
    let mut frames = Vec::new();
    for i in 0..96u64 {
        let v = *((rsp + i * 8) as *const u64);
        if (0x1_4000_0000..0x1_4a00_0000).contains(&v) || (v > me.saturating_sub(0x10_0000) && v < me + 0x10_0000) {
            frames.push(format!("{v:#x}"));
        }
    }
    let mut extra = String::new();
    if code == 0xC000_0005 { extra = format!(" access {} at {:#x}", *(rec.add(0x20) as *const u64), *(rec.add(0x28) as *const u64)); }
    let modof = |a: u64| -> String {
        type GmhEx = unsafe extern "system" fn(u32, *const u16, *mut HMODULE) -> BOOL;
        let k = GetModuleHandleA(b"kernel32.dll\0".as_ptr());
        let Some(f) = GetProcAddress(k, b"GetModuleHandleExW\0".as_ptr()) else { return String::new() };
        let f: GmhEx = std::mem::transmute(f);
        let mut h: HMODULE = std::mem::zeroed();
        if f(6, a as *const u16, &mut h) == 0 { return "?".into(); }
        let mut buf = [0u16; 260];
        let n = GetModuleFileNameW(h, buf.as_mut_ptr(), 260) as usize;
        let name = String::from_utf16_lossy(&buf[..n]);
        let short = name.rsplit(['\\', '/']).next().unwrap_or("").to_string();
        format!("{short}+{:#x}", a - h as u64)
    };
    let frames: Vec<String> = frames.iter().map(|s| { let v = u64::from_str_radix(&s[2..], 16).unwrap_or(0); format!("{s}={}", modof(v)) }).collect();
    extra.push_str(&format!(" in {}", modof(rip)));
    log(&format!("EXCEPTION {code:#x} at {addr:#x} rip {rip:#x}{extra} (ajdirect code near {me:#x}) stack: {}", frames.join(" ")));
    0
}

#[no_mangle]
pub extern "system" fn DllMain(_h: HMODULE, reason: u32, _r: *mut c_void) -> BOOL {
    if reason == 1 {
        std::panic::set_hook(Box::new(|i| log(&format!("PANIC: {i}"))));
        unsafe {
            type AddVeh = unsafe extern "system" fn(u32, unsafe extern "system" fn(*mut c_void) -> i32) -> *mut c_void;
            let k = GetModuleHandleA(b"kernel32.dll\0".as_ptr());
            if let Some(f) = GetProcAddress(k, b"AddVectoredExceptionHandler\0".as_ptr()) {
                let f: AddVeh = std::mem::transmute(f);
                f(1, veh);
            }
        }
        let notes = setup();
        match unsafe { install() } {
            Ok(m) => log(&format!("{NAME}: {m}; {notes}")),
            Err(e) => { log(&format!("{NAME}: NOT applied: {e}")); if let Ok(mut g) = STATE.lock() { *g = None; } }
        }
    }
    1
}

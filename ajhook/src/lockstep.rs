//! Lockstep bot link: virtual XInput pads served from inside the game, and a
//! frame gate that parks the game thread between bot steps.
//!
//! Pads: AJGAME.exe delay-imports XINPUT1_4.dll (XInputGetState = ordinal 2).
//! Its delay-load IAT slot is pointed at `get_state_hook`, which answers the
//! first `vpad_count` slots from memory and passes the rest to the real
//! XInput. No uinput device, no Steam/SDL/winebus in the path, and the state
//! changes exactly when the bot says.
//!
//! UE only polls a disconnected slot again after WM_DEVICECHANGE, so either
//! set AJHOOK_VPADS=<n> in the game's environment (slots are live from the
//! first poll) or enable them at run time with `N<n>`, which posts that
//! message to the game's windows.
//!
//! Gate: every PeekMessageW on the game thread (two per frame) checks
//! GFrameCounter. While the gate is on and the counter reached `target`, the
//! thread parks there - still running queued native jobs (OpenLevel) - until
//! the bot sends the next step. `Z<n> <pad0> <pad1>` sets the pads, lets n
//! frames run and answers once the game parks again. If no step comes for
//! IDLE_RELEASE the gate lets go by itself, so a crashed bot never freezes
//! the game.

use std::ffi::c_void;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Condvar, Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};

use windows_sys::Win32::Foundation::{BOOL, HWND, LPARAM};
use windows_sys::Win32::System::LibraryLoader::{GetModuleHandleA, GetProcAddress, LoadLibraryA};
use windows_sys::Win32::System::Memory::{VirtualProtect, PAGE_PROTECTION_FLAGS, PAGE_READWRITE};
use windows_sys::Win32::UI::WindowsAndMessaging::{EnumThreadWindows, PostMessageW};

/// u64 GFrameCounter (RVA, image base 0x140000000). Read by the log-line
/// formatter at 0x1411be546; bumped once per engine tick.
const RVA_GFRAME_COUNTER: usize = 0xa1e37d8;
const MAX_PADS: usize = 4;
const IDLE_RELEASE: Duration = Duration::from_secs(3);
const STEP_TIMEOUT: Duration = Duration::from_secs(10);

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct Gamepad {
    buttons: u16,
    lt: u8,
    rt: u8,
    lx: i16,
    ly: i16,
    rx: i16,
    ry: i16,
}

#[repr(C)]
pub struct XState {
    packet: u32,
    pad: Gamepad,
}

impl Gamepad {
    /// 24 hex chars = the 12-byte XINPUT_GAMEPAD, little endian.
    fn from_hex(s: &str) -> Option<Gamepad> {
        if s.len() != 24 {
            return None;
        }
        let mut b = [0u8; 12];
        for (i, out) in b.iter_mut().enumerate() {
            *out = u8::from_str_radix(s.get(i * 2..i * 2 + 2)?, 16).ok()?;
        }
        let w = |i: usize| i16::from_le_bytes([b[i], b[i + 1]]);
        Some(Gamepad {
            buttons: u16::from_le_bytes([b[0], b[1]]),
            lt: b[2],
            rt: b[3],
            lx: w(4),
            ly: w(6),
            rx: w(8),
            ry: w(10),
        })
    }
}

static VPADS: AtomicUsize = AtomicUsize::new(0);
/// First XInput slot the virtual pads answer: N2 = slots 0-1 (lockstep farm,
/// no real controllers), N1@1 = slot 1 only, behind a human on slot 0 (Human
/// vs Bot - a pad inside the game that Steam Input cannot hide).
static VBASE: AtomicUsize = AtomicUsize::new(0);
/// Real-time pads (no gate): ms since START of the last state the bot sent.
/// A bot that stops sending - crashed, killed - must not leave a button held,
/// so after LIVE_TIMEOUT the pads read neutral (still connected).
static LAST_SET_MS: AtomicU64 = AtomicU64::new(0);
const LIVE_TIMEOUT_MS: u64 = 2000;
static PACKET: AtomicU32 = AtomicU32::new(1);

fn now_ms() -> u64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_millis() as u64 + 1
}
static REAL_GET: AtomicUsize = AtomicUsize::new(0);
static INSTALLED: AtomicBool = AtomicBool::new(false);
static GATE_ON: AtomicBool = AtomicBool::new(false);
static KEYS_MUTED: AtomicBool = AtomicBool::new(false);

/// Keyboard messages are dropped (Zkeys mute) - only while the gate is on, so
/// a trainer that dies leaves the keyboard working once the gate lets go.
pub fn keys_muted() -> bool {
    KEYS_MUTED.load(Ordering::Relaxed) && GATE_ON.load(Ordering::Relaxed)
}
static GAME_TID: AtomicU32 = AtomicU32::new(0);

fn pads() -> &'static Mutex<[Gamepad; MAX_PADS]> {
    static P: OnceLock<Mutex<[Gamepad; MAX_PADS]>> = OnceLock::new();
    P.get_or_init(|| Mutex::new([Gamepad::default(); MAX_PADS]))
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

pub fn vpad_count() -> usize {
    VPADS.load(Ordering::Relaxed)
}

pub fn installed() -> bool {
    INSTALLED.load(Ordering::Relaxed)
}

type GetStateFn = unsafe extern "system" fn(u32, *mut XState) -> u32;

/// What the game's XInputGetState now is.
pub unsafe extern "system" fn get_state_hook(idx: u32, out: *mut XState) -> u32 {
    let base = VBASE.load(Ordering::Relaxed);
    let i = (idx as usize).wrapping_sub(base);
    if (idx as usize) >= base && i < VPADS.load(Ordering::Relaxed) {
        if out.is_null() {
            return 87; // ERROR_INVALID_PARAMETER
        }
        // Gate on: lockstep owns the pads and steps them, no timeout.
        let stale = !GATE_ON.load(Ordering::Relaxed)
            && now_ms().saturating_sub(LAST_SET_MS.load(Ordering::Relaxed)) > LIVE_TIMEOUT_MS;
        let g = if stale { Gamepad::default() } else { lock(pads())[i] };
        out.write(XState { packet: PACKET.load(Ordering::Relaxed), pad: g });
        return 0;
    }
    match REAL_GET.load(Ordering::Relaxed) {
        0 => 1167, // ERROR_DEVICE_NOT_CONNECTED
        f => std::mem::transmute::<usize, GetStateFn>(f)(idx, out),
    }
}

#[inline]
unsafe fn rd32(p: *const u8) -> u32 {
    std::ptr::read_unaligned(p as *const u32)
}

/// Point a delay-load import slot at `repl`. The delay helper is never run
/// for that slot afterwards, so the caller must not need the old value
/// (until the first call it is only the helper's stub).
unsafe fn hook_delay_import(base: *mut u8, dll: &str, ordinal: u16, name: &str, repl: usize) -> bool {
    if base.is_null() || rd32(base) & 0xffff != 0x5a4d {
        return false;
    }
    let nt = base.add(rd32(base.add(0x3c)) as usize);
    if rd32(nt) != 0x0000_4550 {
        return false;
    }
    // PE32+ DataDirectory at nt+0x18+0x70; delay import = entry 13.
    let dir = rd32(nt.add(0x18 + 0x70 + 13 * 8)) as usize;
    if dir == 0 {
        return false;
    }
    let want = dll.to_ascii_lowercase();
    let mut d = base.add(dir);
    loop {
        let name_rva = rd32(d.add(4)) as usize;
        if name_rva == 0 {
            return false;
        }
        let iat = rd32(d.add(12)) as usize;
        let int = rd32(d.add(16)) as usize;
        let dname = std::ffi::CStr::from_ptr(base.add(name_rva) as *const i8)
            .to_string_lossy()
            .to_ascii_lowercase();
        if dname == want {
            let mut i = 0usize;
            loop {
                let e = std::ptr::read_unaligned(base.add(int + i * 8) as *const u64);
                if e == 0 {
                    break;
                }
                let hit = if e & (1u64 << 63) != 0 {
                    (e & 0xffff) as u16 == ordinal
                } else {
                    let p = base.add(e as usize + 2) as *const i8;
                    std::ffi::CStr::from_ptr(p).to_bytes() == name.as_bytes()
                };
                if hit {
                    let slot = base.add(iat + i * 8) as *mut u64;
                    let mut old: PAGE_PROTECTION_FLAGS = 0;
                    VirtualProtect(slot as *const c_void, 8, PAGE_READWRITE, &mut old);
                    slot.write_volatile(repl as u64);
                    VirtualProtect(slot as *const c_void, 8, old, &mut old);
                    return true;
                }
                i += 1;
            }
        }
        d = d.add(32); // sizeof(IMAGE_DELAYLOAD_DESCRIPTOR)
    }
}

/// Install the XInput hook. Call as early as possible: UE polls every slot
/// once at startup and afterwards only the ones it saw connected.
pub fn install(game_tid: u32) -> String {
    GAME_TID.store(game_tid, Ordering::Relaxed);
    if let Some(n) = std::env::var("AJHOOK_VPADS").ok().and_then(|v| v.trim().parse::<usize>().ok()) {
        VPADS.store(n.min(MAX_PADS), Ordering::Relaxed);
    }
    unsafe {
        let m = LoadLibraryA(c"xinput1_4.dll".as_ptr() as *const u8);
        if !m.is_null() {
            if let Some(p) = GetProcAddress(m, c"XInputGetState".as_ptr() as *const u8) {
                REAL_GET.store(p as usize, Ordering::Relaxed);
            }
        }
        let exe = GetModuleHandleA(std::ptr::null()) as *mut u8;
        if hook_delay_import(exe, "xinput1_4.dll", 2, "XInputGetState", get_state_hook as *const () as usize) {
            INSTALLED.store(true, Ordering::Relaxed);
            format!("xinput: hooked XInputGetState, {} virtual pads", vpad_count())
        } else {
            "xinput: XInputGetState not found in the delay imports".into()
        }
    }
}

pub fn set_game_tid(tid: u32) {
    GAME_TID.store(tid, Ordering::Relaxed);
}

unsafe extern "system" fn post_devchange(hwnd: HWND, _: LPARAM) -> BOOL {
    // WM_DEVICECHANGE / DBT_DEVNODES_CHANGED: UE re-polls every XInput slot.
    PostMessageW(hwnd, 0x0219, 7, 0);
    1
}

fn announce_devices() {
    let tid = GAME_TID.load(Ordering::Relaxed);
    if tid != 0 {
        unsafe { EnumThreadWindows(tid, Some(post_devchange), 0) };
    }
}

fn set_pads(tokens: &[&str]) -> Result<(), String> {
    let mut parsed = Vec::with_capacity(tokens.len());
    for t in tokens {
        parsed.push(Gamepad::from_hex(t).ok_or_else(|| format!("!bad pad state {t:?} (24 hex chars)"))?);
    }
    if parsed.len() > MAX_PADS {
        return Err("!too many pads".into());
    }
    let mut p = lock(pads());
    for (i, g) in parsed.into_iter().enumerate() {
        p[i] = g;
    }
    PACKET.fetch_add(1, Ordering::Relaxed);
    LAST_SET_MS.store(now_ms(), Ordering::Relaxed);
    Ok(())
}

/// One pad only (index from the first virtual slot): each bot thread owns
/// its pad and must not overwrite the other's.
fn set_one(i: usize, hex: &str) -> Result<(), String> {
    let g = Gamepad::from_hex(hex).ok_or_else(|| format!("!bad pad state {hex:?} (24 hex chars)"))?;
    if i >= MAX_PADS {
        return Err("!pad index out of range".into());
    }
    lock(pads())[i] = g;
    PACKET.fetch_add(1, Ordering::Relaxed);
    LAST_SET_MS.store(now_ms(), Ordering::Relaxed);
    Ok(())
}

// ------------------------------------------------------------------ gate

struct Gate {
    on: bool,
    target: u64,
    parked: Option<u64>,
    last: Instant,
}

fn gate() -> &'static (Mutex<Gate>, Condvar) {
    static G: OnceLock<(Mutex<Gate>, Condvar)> = OnceLock::new();
    G.get_or_init(|| {
        (Mutex::new(Gate { on: false, target: 0, parked: None, last: Instant::now() }), Condvar::new())
    })
}

pub fn frame() -> u64 {
    unsafe {
        let base = GetModuleHandleA(std::ptr::null()) as usize;
        std::ptr::read_volatile((base + RVA_GFRAME_COUNTER) as *const u64)
    }
}

/// Game thread, from every PeekMessageW. `service` runs queued native jobs
/// while the thread is parked.
pub unsafe fn on_pump(service: unsafe fn(), log: fn(&str)) {
    if !GATE_ON.load(Ordering::Relaxed) {
        return;
    }
    let f = frame();
    let (m, cv) = gate();
    let mut g = lock(m);
    if !g.on || f < g.target {
        return;
    }
    g.parked = Some(f);
    cv.notify_all();
    loop {
        if !g.on || g.target > f {
            break;
        }
        if g.last.elapsed() > IDLE_RELEASE {
            g.on = false;
            GATE_ON.store(false, Ordering::Relaxed);
            log(&format!("lockstep: no step for {IDLE_RELEASE:?} at frame {f}, gate released"));
            break;
        }
        g = cv.wait_timeout(g, Duration::from_millis(1)).map(|r| r.0).unwrap_or_else(|e| e.into_inner().0);
        drop(g);
        service();
        g = lock(m);
    }
    g.parked = None;
    cv.notify_all();
}

/// The real controllers, read from inside the game (so a pad that Steam
/// Input shows only to the game counts): "<slot>:<24 hex>" per connected
/// slot of the real XInputGetState, "-" for none. Works while the gate holds
/// the game frozen - the combo trainer waits on a human's press that way.
fn real_states() -> String {
    let f = REAL_GET.load(Ordering::Relaxed);
    if f == 0 {
        return "!xinput hook not installed".into();
    }
    let mut out = Vec::new();
    for idx in 0..4u32 {
        let mut st = XState { packet: 0, pad: Gamepad::default() };
        if unsafe { std::mem::transmute::<usize, GetStateFn>(f)(idx, &mut st) } == 0 {
            let g = st.pad;
            let mut b = Vec::with_capacity(12);
            b.extend_from_slice(&g.buttons.to_le_bytes());
            b.push(g.lt);
            b.push(g.rt);
            for v in [g.lx, g.ly, g.rx, g.ry] {
                b.extend_from_slice(&v.to_le_bytes());
            }
            out.push(format!("{idx}:{}", b.iter().map(|x| format!("{x:02x}")).collect::<String>()));
        }
    }
    if out.is_empty() { "-".into() } else { out.join(" ") }
}

/// `Zreal`                  the real controllers' states (see real_states)
/// `Z<n> [pad0 [pad1 ..]]`  set pads, run n frames, park; "frame=<f>"
/// `Zoff`                   release the gate (game runs freely again)
/// `Zset pad0 [pad1 ..]`    set pads without touching the gate
/// `Z?`                     status
/// `Zpad <i> <hex>`         set virtual pad i only (real-time play)
/// `N<n>`                   virtual pad count (0 = real XInput only), slots 0..n
/// `N<n>@<b>`               n virtual pads on slots b..b+n (behind real ones)
/// Replies "vpads=<n>" (base 0, the farm's form) or "vpads=<n> base=<b>".
pub fn command(op: u8, rest: &str) -> String {
    let rest = rest.trim();
    if op == b'N' {
        if !installed() {
            return "!xinput hook not installed".into();
        }
        if !rest.is_empty() {
            let (n, b) = match rest.split_once('@') {
                Some((n, b)) => (n.parse::<usize>(), b.parse::<usize>()),
                None => (rest.parse::<usize>(), Ok(0)),
            };
            match (n, b) {
                (Ok(n), Ok(b)) if b + n <= MAX_PADS => {
                    if n > 0 {
                        // Fresh pads start neutral.
                        *lock(pads()) = [Gamepad::default(); MAX_PADS];
                        LAST_SET_MS.store(now_ms(), Ordering::Relaxed);
                    }
                    VBASE.store(b, Ordering::Relaxed);
                    VPADS.store(n, Ordering::Relaxed);
                    announce_devices();
                }
                _ => return "!usage: N<0-4>[@<first slot>]".into(),
            }
        }
        let b = VBASE.load(Ordering::Relaxed);
        return if b == 0 {
            format!("vpads={}", vpad_count())
        } else {
            format!("vpads={} base={b}", vpad_count())
        };
    }
    let (m, cv) = gate();
    match rest {
        "off" => {
            let mut g = lock(m);
            g.on = false;
            GATE_ON.store(false, Ordering::Relaxed);
            cv.notify_all();
            return format!("off frame={}", frame());
        }
        "?" => {
            let g = lock(m);
            return format!("on={} frame={} target={} parked={:?} vpads={}", g.on, frame(), g.target,
                           g.parked, vpad_count());
        }
        _ => {}
    }
    let mut it = rest.split_whitespace();
    let first = it.next().unwrap_or("");
    let pads_tok: Vec<&str> = it.collect();
    if first == "set" {
        return match set_pads(&pads_tok) {
            Ok(()) => "set".into(),
            Err(e) => e,
        };
    }
    if first == "real" {
        return real_states();
    }
    if first == "keys" {
        // Zkeys mute|pass: while muted (and the gate is on) the game never
        // sees keyboard messages - the combo trainer reads the keyboard itself,
        // and presses made while the game is held must not reach P1 later.
        let mute = pads_tok.first() == Some(&"mute");
        KEYS_MUTED.store(mute, Ordering::Relaxed);
        return format!("keys {}", if mute { "muted" } else { "pass" });
    }
    if first == "pad" {
        let (Some(i), Some(hex)) = (pads_tok.first().and_then(|s| s.parse::<usize>().ok()), pads_tok.get(1)) else {
            return "!usage: Zpad <index> <24 hex>".into();
        };
        return match set_one(i, hex) {
            Ok(()) => "ok".into(),
            Err(e) => e,
        };
    }
    let Ok(n) = first.parse::<u64>() else {
        return "!usage: Z<frames> [pad hex..] | Zoff | Zset pad.. | Z?".into();
    };
    if let Err(e) = set_pads(&pads_tok) {
        return e;
    }
    let mut g = lock(m);
    let start = g.parked.unwrap_or_else(frame);
    g.on = true;
    GATE_ON.store(true, Ordering::Relaxed);
    g.last = Instant::now();
    g.target = start + n;
    cv.notify_all();
    let deadline = Instant::now() + STEP_TIMEOUT;
    loop {
        if let Some(f) = g.parked {
            if f >= g.target {
                return format!("frame={f}");
            }
        }
        if !g.on {
            return format!("!released frame={}", frame());
        }
        if Instant::now() > deadline {
            // Loading a map stalls the frame pump for seconds.
            return format!("!timeout frame={}", frame());
        }
        g = cv.wait_timeout(g, Duration::from_millis(50)).map(|r| r.0).unwrap_or_else(|e| e.into_inner().0);
    }
}

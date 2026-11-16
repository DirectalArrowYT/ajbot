//! ajhook - injects Lua into MY HERO ACADEMIA: All's Justice.
//!
//! The game ships Lua 5.2 as a real DLL (Lua524.dll) with the full C API
//! exported, and its battle logic is plaintext Lua. That makes a scripting
//! bridge cheap: IAT-hook `lua_pcallk` to capture the game's `lua_State`, then
//! run our own chunks on the game thread inside that same hook.
//!
//! Anything the game's Lua can see, a chunk sent here can see - character
//! state, distances, hitstun, current action ids. That is the observation
//! feed the bot needs.
//!
//! Transport is a local named pipe (\\.\pipe\ajhook) created with
//! PIPE_REJECT_REMOTE_CLIENTS, so it is not reachable over the network and
//! carries the default DACL (creating user + admins only).
//!
//! Build:  cargo build --release
//! Deploy: copy target/release/ajhook.dll into the game's Win64 folder,
//!         renamed to ajhook.asi (the existing ASI loader picks it up).
//! Use:    write a Lua chunk + newline to the pipe, read the result back.

mod lua;
mod sample;
mod roster;
mod dedupe;
mod match_tracker;
mod recorder;
mod lockstep;

use std::collections::VecDeque;
use std::ffi::{c_void, CStr, CString};
mod vhook;
use std::os::raw::{c_char, c_int};
use std::sync::atomic::{AtomicPtr, AtomicUsize, Ordering};
use std::sync::{Mutex, OnceLock};

use windows_sys::Win32::Foundation::{CloseHandle, FreeLibrary, BOOL, HANDLE, HMODULE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Storage::FileSystem::{ReadFile, WriteFile, PIPE_ACCESS_DUPLEX};
use windows_sys::Win32::System::LibraryLoader::{
    GetModuleHandleA, GetModuleHandleExW, GetProcAddress,
    GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS,
};
use windows_sys::Win32::System::Memory::{
    VirtualProtect, PAGE_EXECUTE_READWRITE, PAGE_PROTECTION_FLAGS, PAGE_READWRITE,
};
use windows_sys::Win32::System::Pipes::{
    ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, PIPE_READMODE_BYTE,
    PIPE_REJECT_REMOTE_CLIENTS, PIPE_TYPE_BYTE, PIPE_WAIT,
};

static API: OnceLock<lua::Api> = OnceLock::new();
static STATE: AtomicPtr<c_void> = AtomicPtr::new(std::ptr::null_mut());
static ORIG_PCALLK: AtomicUsize = AtomicUsize::new(0);
static ORIG_CALLK: AtomicUsize = AtomicUsize::new(0);
static ORIG_PCALL: AtomicUsize = AtomicUsize::new(0);
static ORIG_CALL: AtomicUsize = AtomicUsize::new(0);
static IN_HOOK: AtomicUsize = AtomicUsize::new(0);
/// Cheap "is there work?" flag so the hot path avoids taking the queue lock.
static PENDING: AtomicUsize = AtomicUsize::new(0);

/// A chunk re-run on a fixed pump cadence. This is how a complete snapshot is
/// collected: the character context rotates between calls, so a resident
/// sampler sees every character, where a one-shot poll sees only one.
static RESIDENT_ON: AtomicUsize = AtomicUsize::new(0);
static RESIDENT_EVERY: AtomicUsize = AtomicUsize::new(8);
static PUMPS: AtomicUsize = AtomicUsize::new(0);

fn resident() -> &'static Mutex<String> {
    static R: OnceLock<Mutex<String>> = OnceLock::new();
    R.get_or_init(|| Mutex::new(String::new()))
}

/// Aggregated samples, keyed by whatever the sampler returns as its first
/// token. Collected here rather than in a Lua global because the game runs a
/// separate lua_State per character: a table stashed in `_G` is only visible
/// to the one character that wrote it, so the enemy's samples were never
/// readable from our character's state.
fn samples() -> &'static Mutex<std::collections::HashMap<String, (String, u64)>> {
    static S: OnceLock<Mutex<std::collections::HashMap<String, (String, u64)>>> = OnceLock::new();
    S.get_or_init(|| Mutex::new(std::collections::HashMap::new()))
}

/// lua_State pointer -> small stable slot index, assigned in first-seen order.
/// One state per character, so a slot is effectively a character handle.
fn seen_states() -> &'static Mutex<std::collections::HashMap<usize, usize>> {
    static S: OnceLock<Mutex<std::collections::HashMap<usize, usize>>> = OnceLock::new();
    S.get_or_init(|| Mutex::new(std::collections::HashMap::new()))
}

fn state_slot(st: lua::State) -> usize {
    let key = st as usize;
    match seen_states().lock() {
        Ok(mut m) => {
            let next = m.len();
            *m.entry(key).or_insert(next)
        }
        Err(_) => 0,
    }
}

static SEQ: AtomicUsize = AtomicUsize::new(0);

// -------------------------------------------------------------- recording
//
// Native recording: while RECORD_WANT is set, pump() (game thread only, see
// maybe_record_tick) opens/writes/closes the output file directly, so a
// tick never round-trips through the Python pipe client. The path/want
// flag are set from pipe threads (see the 'W'/'X' opcodes) and only READ
// by the game thread, exactly like resident()/RESIDENT_ON already bridge
// pipe-thread configuration into game-thread execution - the open File
// itself is touched only from inside pump(), never from a pipe thread.
static RECORD_WANT: AtomicUsize = AtomicUsize::new(0);
static RECORD_TICKS: AtomicUsize = AtomicUsize::new(0);

/// Set by the 'W' opcode (the requested base path) and consumed the next
/// time maybe_record_tick() sees RECORD_WANT go from 0 to 1 - see
/// recorder::RecordSession::new/compute_path for how it is used.
fn record_want_path() -> &'static Mutex<String> {
    static P: OnceLock<Mutex<String>> = OnceLock::new();
    P.get_or_init(|| Mutex::new(String::new()))
}

/// Character-select cursor pushed by the UE4SS CursorExport mod via 'P'
/// ("side=0 pos=53 ..." verbatim) and served back by 'K' with its age. The
/// menu never pumps the Lua VM, so nothing native can read it - the mod does.
fn cursor_slot() -> &'static Mutex<Option<(String, std::time::Instant)>> {
    static C: OnceLock<Mutex<Option<(String, std::time::Instant)>>> = OnceLock::new();
    C.get_or_init(|| Mutex::new(None))
}

/// WidgetVsCharSelect holds two VsCharSelectInfo structs (216 bytes each)
/// back to back from offset 1832: the reflected `SelectInfo` (1P) and an
/// unreflected 2P copy right after it, so Lua can only see the first. We are
/// inside the game process, so read both straight from the widget's memory.
const SELECT_INFO_BASE: usize = 1832;
const SELECT_INFO_STRIDE: usize = 216;

pub(crate) fn read_bytes(addr: usize, len: usize) -> Option<Vec<u8>> {
    use windows_sys::Win32::System::Diagnostics::Debug::ReadProcessMemory;
    use windows_sys::Win32::System::Threading::GetCurrentProcess;
    if addr == 0 || len == 0 {
        return None;
    }
    let mut buf = vec![0u8; len];
    let mut got = 0usize;
    let ok = unsafe {
        ReadProcessMemory(
            GetCurrentProcess(),
            addr as *const c_void,
            buf.as_mut_ptr() as *mut c_void,
            len,
            &mut got,
        )
    };
    if ok == 0 || got < len {
        None
    } else {
        Some(buf)
    }
}

/// Control-mode screen settings, committed per team slot when the screen is
/// confirmed: WidgetComboMode (widget+2312 for 1P, +2320 for 2P) keeps a
/// TArray at +1784 of 3-byte entries (mode 0=Normal 1=Manual, auto change
/// combo, auto plus ultra). Emitted as " p1_ctl=0,1,1;1,0,0;..." per side.
fn read_control_settings(widget: usize) -> String {
    let mut out = String::new();
    for (tag, off) in [("p1", 2312usize), ("p2", 2320usize)] {
        let Some(p) = read_bytes(widget + off, 8) else { continue };
        let cm = usize::from_le_bytes(p[..8].try_into().unwrap());
        // Live per-row mode: each row's element widget (WidgetComboMode's
        // ComboModeElements TArray at +1672) keeps its Normal/Manual toggle in
        // the byte at +0x5e0 - it flips on every Left/Right press, before the
        // screen is confirmed, and is set from the remembered value on open.
        if let Some(arr) = read_bytes(cm + 1672, 12) {
            let data = usize::from_le_bytes(arr[..8].try_into().unwrap());
            let num = u32::from_le_bytes(arr[8..12].try_into().unwrap()) as usize;
            if (1..=8).contains(&num) {
                if let Some(ptrs) = read_bytes(data, num * 8) {
                    let rows: Vec<String> = ptrs
                        .chunks(8)
                        .map(|c| {
                            let el = usize::from_le_bytes(c.try_into().unwrap());
                            match read_bytes(el + 0x5e0, 1) {
                                Some(b) => b[0].to_string(),
                                None => "?".to_string(),
                            }
                        })
                        .collect();
                    out.push_str(&format!(" {tag}_rows={}", rows.join(",")));
                }
            }
        }
        // Cursor row on the control-mode screen (byte at +0x6d0 of the widget).
        if let Some(b) = read_bytes(cm + 0x6d0, 1) {
            out.push_str(&format!(" {tag}_crow={}", b[0]));
        }
        let Some(arr) = read_bytes(cm + 1784, 12) else { continue };
        let data = usize::from_le_bytes(arr[..8].try_into().unwrap());
        let num = u32::from_le_bytes(arr[8..12].try_into().unwrap()) as usize;
        if num == 0 || num > 8 {
            continue;
        }
        let Some(d) = read_bytes(data, num * 3) else { continue };
        let entries: Vec<String> = d
            .chunks(3)
            .map(|e| format!("{},{},{}", e[0], e[1], e[2]))
            .collect();
        out.push_str(&format!(" {tag}_ctl={}", entries.join(";")));
    }
    out
}

fn read_select_infos(widget: usize) -> String {
    use windows_sys::Win32::System::Diagnostics::Debug::ReadProcessMemory;
    use windows_sys::Win32::System::Threading::GetCurrentProcess;
    let mut out = String::new();
    for (i, tag) in ["p1", "p2"].iter().enumerate() {
        let base = widget + SELECT_INFO_BASE + i * SELECT_INFO_STRIDE;
        let mut buf = [0u8; 96];
        let mut got = 0usize;
        let ok = unsafe {
            ReadProcessMemory(
                GetCurrentProcess(),
                base as *const c_void,
                buf.as_mut_ptr() as *mut c_void,
                buf.len(),
                &mut got,
            )
        };
        if ok == 0 || got < buf.len() {
            continue;
        }
        let i32_at = |o: usize| i32::from_le_bytes([buf[o], buf[o + 1], buf[o + 2], buf[o + 3]]);
        out.push_str(&format!(
            " {tag}_side={} {tag}_pos={} {tag}_page={} {tag}_row={} {tag}_banner={} {tag}_complete={} {tag}_random={} {tag}_combo={}",
            buf[0],
            i32_at(4),
            i32_at(20),
            i32_at(32),
            i32_at(44),
            buf[52],
            buf[53],
            buf[76],
        ));
    }
    out
}

/// The live session: dedupe/teams/Roster/MatchTracker state plus whatever
/// output file is currently open, all owned and touched only from inside
/// pump() (game thread) - see the module comment above RECORD_WANT.
fn record_session() -> &'static Mutex<Option<recorder::RecordSession>> {
    static S: OnceLock<Mutex<Option<recorder::RecordSession>>> = OnceLock::new();
    S.get_or_init(|| Mutex::new(None))
}


/// Wall-clock gate for how often a full tick gets assembled and written,
/// independent of the (much higher) native pump rate - matches record.py's
/// --hz default of 20/s. Kept in a Mutex<Instant> rather than an atomic
/// since Instant has no lock-free representation.
fn record_next_write() -> &'static Mutex<Option<std::time::Instant>> {
    static N: OnceLock<Mutex<Option<std::time::Instant>>> = OnceLock::new();
    N.get_or_init(|| Mutex::new(None))
}

const RECORD_TICK_PERIOD_MS: u64 = 50;

struct Job {
    src: String,
    reply: std::sync::mpsc::Sender<String>,
    /// Native work to run on the game thread instead of a Lua chunk.
    native: Option<Box<dyn FnOnce() -> String + Send>>,
}

fn queue() -> &'static Mutex<VecDeque<Job>> {
    static Q: OnceLock<Mutex<VecDeque<Job>>> = OnceLock::new();
    Q.get_or_init(|| Mutex::new(VecDeque::new()))
}

fn log(msg: &str) {
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open("ajhook.log")
    {
        let _ = writeln!(f, "{msg}");
    }
}

// ---------------------------------------------------------------- IAT hook

/// Every loaded module's (base address, file name) in this process, via
/// ToolHelp32 rather than EnumProcessModules so no extra Psapi dependency is
/// needed. CPU-controlled fighters never produced a single sample through
/// the main executable's IAT hook alone - lua_pcallk/lua_callk are the only
/// entry points the game imports by name (lua_pcall/lua_call are not in the
/// import table at all), so whatever drives them either does not go through
/// these functions, or goes through a DIFFERENT module's own copy of the
/// import table, which patching only the main EXE's IAT would never see.
/// MS docs note CreateToolhelp32Snapshot can transiently fail while modules
/// are loading/unloading and recommend retrying.
unsafe fn all_modules() -> Vec<(*mut u8, String)> {
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Module32FirstW, Module32NextW, MODULEENTRY32W,
        TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32,
    };
    let mut out = Vec::new();
    let mut snap = INVALID_HANDLE_VALUE;
    for _ in 0..5 {
        snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, 0);
        if snap != INVALID_HANDLE_VALUE {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    if snap == INVALID_HANDLE_VALUE {
        return out;
    }
    let mut me: MODULEENTRY32W = std::mem::zeroed();
    me.dwSize = std::mem::size_of::<MODULEENTRY32W>() as u32;
    if Module32FirstW(snap, &mut me) != 0 {
        loop {
            let len = me.szModule.iter().position(|&c| c == 0).unwrap_or(me.szModule.len());
            let name = String::from_utf16_lossy(&me.szModule[..len]);
            out.push((me.modBaseAddr, name));
            if Module32NextW(snap, &mut me) == 0 {
                break;
            }
        }
    }
    CloseHandle(snap);
    out
}

#[inline]
unsafe fn rd32(p: *const u8) -> u32 {
    std::ptr::read_unaligned(p as *const u32)
}

/// Replace the import-table entry for `dll!func` in the module at `base`
/// with `replacement`, returning the original address. PE headers are
/// walked by raw offset so this doesn't depend on which windows-sys feature
/// happens to expose the structs.
unsafe fn hook_iat(base: *mut u8, dll: &str, func: &str, replacement: *const c_void) -> Option<usize> {
    if base.is_null() || rd32(base) & 0xffff != 0x5a4d {
        return None; // not MZ
    }
    let nt = base.add(rd32(base.add(0x3c)) as usize);
    if rd32(nt) != 0x0000_4550 {
        return None; // not PE\0\0
    }
    // PE32+: optional header at nt+0x18, DataDirectory at +0x70, import = entry 1
    let import_rva = rd32(nt.add(0x18 + 0x70 + 8)) as usize;
    if import_rva == 0 {
        return None;
    }

    let want_dll = dll.to_ascii_lowercase();
    let mut desc = base.add(import_rva);
    loop {
        let orig_first = rd32(desc) as usize;
        let name_rva = rd32(desc.add(12)) as usize;
        let first = rd32(desc.add(16)) as usize;
        if name_rva == 0 && first == 0 {
            break;
        }
        let name = CStr::from_ptr(base.add(name_rva) as *const c_char)
            .to_string_lossy()
            .to_ascii_lowercase();
        if name == want_dll {
            // Bound imports leave OriginalFirstThunk zero; fall back to FirstThunk.
            let names_tbl = if orig_first != 0 { orig_first } else { first };
            let mut i = 0usize;
            loop {
                let entry = std::ptr::read_unaligned(base.add(names_tbl + i * 8) as *const u64);
                if entry == 0 {
                    break;
                }
                if entry & (1u64 << 63) == 0 {
                    // IMAGE_IMPORT_BY_NAME: u16 hint, then the name
                    let import = base.add(entry as usize + 2) as *const c_char;
                    if CStr::from_ptr(import).to_bytes() == func.as_bytes() {
                        let slot = base.add(first + i * 8) as *mut u64;
                        let mut old: PAGE_PROTECTION_FLAGS = 0;
                        VirtualProtect(slot as *const c_void, 8, PAGE_READWRITE, &mut old);
                        let prev = *slot as usize;
                        *slot = replacement as u64;
                        VirtualProtect(slot as *const c_void, 8, old, &mut old);
                        return Some(prev);
                    }
                }
                i += 1;
            }
        }
        desc = desc.add(20); // sizeof(IMAGE_IMPORT_DESCRIPTOR)
    }
    None
}

// ------------------------------------------------------------ lua plumbing

/// Run a chunk on the game's state. Must be called on the game thread.
unsafe fn run_chunk(api: &lua::Api, st: lua::State, src: &str) -> String {
    let top = (api.gettop)(st);
    let name = CString::new("@ajhook").unwrap();
    let rc = (api.load_bufferx)(
        st,
        src.as_ptr() as *const c_char,
        src.len(),
        name.as_ptr(),
        std::ptr::null(),
    );
    let out = if rc != lua::OK {
        format!("compile error: {}", pop_str(api, st))
    } else {
        let rc = (api.pcallk)(st, 0, 1, 0, 0, std::ptr::null());
        if rc != lua::OK {
            format!("runtime error: {}", pop_str(api, st))
        } else {
            pop_str(api, st)
        }
    };
    (api.settop)(st, top);
    out
}

unsafe fn pop_str(api: &lua::Api, st: lua::State) -> String {
    let mut len: usize = 0;
    let p = (api.tolstring)(st, -1, &mut len);
    if p.is_null() {
        return String::new();
    }
    String::from_utf8_lossy(std::slice::from_raw_parts(p as *const u8, len)).into_owned()
}

/// Record the live state and run anything queued. Safe to call from any Lua
/// C-API boundary: we are on the game thread with a valid state, and the
/// chunk's stack usage is unwound afterwards.
unsafe fn pump(st: lua::State) {
    STATE.store(st as *mut c_void, Ordering::Relaxed);
    drain_native();

    let n = PUMPS.fetch_add(1, Ordering::Relaxed);
    let want_resident = RESIDENT_ON.load(Ordering::Relaxed) != 0
        && n % RESIDENT_EVERY.load(Ordering::Relaxed).max(1) == 0;
    let recording = RECORD_WANT.load(Ordering::Relaxed) != 0;
    if PENDING.load(Ordering::Relaxed) == 0 && !want_resident && !recording {
        return;
    }
    // Guard against re-entering ourselves while a chunk is running.
    if IN_HOOK.swap(1, Ordering::Acquire) != 0 {
        return;
    }
    if let Some(api) = API.get() {
        if want_resident {
            let src = resident().lock().ok().map(|s| s.clone()).unwrap_or_default();
            if !src.is_empty() {
                let out = run_chunk(api, st, &src);
                // Key by lua_State: the game gives each character its own, so
                // this is a stable per-character slot for the session. Keying
                // by role instead would collapse both benched teammates into
                // one record.
                if !out.is_empty() && !out.starts_with("runtime error")
                    && !out.starts_with("compile error")
                {
                    let slot = state_slot(st);
                    let seq = SEQ.fetch_add(1, Ordering::Relaxed) as u64;
                    if let Ok(mut m) = samples().lock() {
                        m.insert(format!("c{slot}"), (out, seq));
                    }
                }
            }
        }
        loop {
            let job = queue().lock().ok().and_then(|mut q| q.pop_front());
            match job {
                Some(job) => {
                    PENDING.fetch_sub(1, Ordering::Relaxed);
                    let res = match job.native {
                        Some(f) => f(),
                        None => run_chunk(api, st, &job.src),
                    };
                    let _ = job.reply.send(res);
                }
                None => break,
            }
        }
    }
    if recording {
        maybe_record_tick();
    } else {
        maybe_close_recording();
    }
    IN_HOOK.store(0, Ordering::Release);
}

/// Same text the 'S' opcode replies with: "#states=N seq=M" header, then one
/// "<slot> <fields> age=<n>" line per character - exactly what
/// `sample::snapshot()` (the ported half of `sampler.snapshot(hook)`) parses.
fn samples_text() -> String {
    let now = SEQ.load(Ordering::Relaxed) as u64;
    let states = seen_states().lock().map(|s| s.len()).unwrap_or(0);
    match samples().lock() {
        Ok(m) => {
            let mut lines = vec![format!("#states={states} seq={now}")];
            for (k, (fields, seq)) in m.iter() {
                lines.push(format!("{k} {fields} age={}", now.saturating_sub(*seq)));
            }
            lines.join("\n")
        }
        Err(_) => String::new(),
    }
}

/// Lazily creates the RecordSession (if RECORD_WANT was just set) and, on a
/// wall-clock cadence independent of the native pump rate, feeds it one
/// tick of samples() - dedupe/teams/Roster/MatchTracker and file output all
/// happen inside RecordSession::tick(), reproducing record.py's pipeline
/// and JSON schema. Only ever called from inside pump() (game thread), so
/// the session's open File is never touched from a pipe thread - see the
/// module-level comment above RECORD_WANT.
unsafe fn maybe_record_tick() {
    let mut guard = match record_session().lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    if guard.is_none() {
        let path = record_want_path().lock().map(|p| p.clone()).unwrap_or_default();
        *guard = Some(recorder::RecordSession::new(path.clone()));
        if let Ok(mut n) = record_next_write().lock() {
            *n = None;
        }
        log(&format!("recording session started (base path: {path:?})"));
    }

    let now = std::time::Instant::now();
    {
        let mut next = match record_next_write().lock() {
            Ok(n) => n,
            Err(_) => return,
        };
        if let Some(due) = *next {
            if now < due {
                return;
            }
        }
        *next = Some(now + std::time::Duration::from_millis(RECORD_TICK_PERIOD_MS));
    }

    if let Some(session) = guard.as_mut() {
        session.tick(&samples_text());
        RECORD_TICKS.store(session.ticks_written as usize, Ordering::Relaxed);
    }
}

/// Drops the RecordSession (if one is open), which flushes its file via
/// Drop semantics on the underlying std::fs::File. Only reached on a pump
/// that didn't already bail out early (i.e. one doing resident or queued-
/// job work), same as maybe_record_tick - in practice this closes promptly
/// since the resident sampler is normally running throughout a recording
/// session, but it is not guaranteed to be the literal next pump after 'X'
/// if neither resident nor a queued job happens to fire first.
unsafe fn maybe_close_recording() {
    if let Ok(mut g) = record_session().lock() {
        if g.take().is_some() {
            log("recording stopped");
        }
    }
}

unsafe extern "C" fn pcallk_hook(
    st: lua::State,
    nargs: c_int,
    nresults: c_int,
    errfunc: c_int,
    ctx: c_int,
    k: *const c_void,
) -> c_int {
    pump(st);
    let orig: lua::PcallK = std::mem::transmute(ORIG_PCALLK.load(Ordering::Relaxed));
    orig(st, nargs, nresults, errfunc, ctx, k)
}

unsafe extern "C" fn callk_hook(
    st: lua::State,
    nargs: c_int,
    nresults: c_int,
    ctx: c_int,
    k: *const c_void,
) {
    pump(st);
    let orig: lua::CallK = std::mem::transmute(ORIG_CALLK.load(Ordering::Relaxed));
    orig(st, nargs, nresults, ctx, k)
}

/// Classic (non-continuation) counterparts to pcallk_hook/callk_hook - see
/// lua.rs's Pcall/Call for why these are hooked too.
unsafe extern "C" fn pcall_hook(
    st: lua::State,
    nargs: c_int,
    nresults: c_int,
    errfunc: c_int,
) -> c_int {
    pump(st);
    let orig: lua::Pcall = std::mem::transmute(ORIG_PCALL.load(Ordering::Relaxed));
    orig(st, nargs, nresults, errfunc)
}

unsafe extern "C" fn call_hook(st: lua::State, nargs: c_int, nresults: c_int) {
    pump(st);
    let orig: lua::Call = std::mem::transmute(ORIG_CALL.load(Ordering::Relaxed));
    orig(st, nargs, nresults)
}

// ------------------------------------------------------- named pipe console

const PIPE_NAME: &str = r"\\.\pipe\ajhook";

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

unsafe fn pipe_fill(h: HANDLE, buf: &mut Vec<u8>) -> bool {
    let mut chunk = [0u8; 4096];
    let mut got: u32 = 0;
    let ok = ReadFile(
        h,
        chunk.as_mut_ptr(),
        chunk.len() as u32,
        &mut got,
        std::ptr::null_mut(),
    );
    if ok == 0 || got == 0 {
        return false;
    }
    buf.extend_from_slice(&chunk[..got as usize]);
    true
}

/// Wire format: ASCII decimal byte-length, '\n', then exactly that many bytes.
/// Length prefixing means chunk text is passed through verbatim - an earlier
/// newline-escaped format corrupted any Lua source containing its own escapes.
unsafe fn pipe_read_msg(h: HANDLE, buf: &mut Vec<u8>) -> Option<Vec<u8>> {
    loop {
        if let Some(nl) = buf.iter().position(|&b| b == b'\n') {
            let len: usize = std::str::from_utf8(&buf[..nl]).ok()?.trim().parse().ok()?;
            if buf.len() >= nl + 1 + len {
                let body = buf[nl + 1..nl + 1 + len].to_vec();
                buf.drain(..nl + 1 + len);
                return Some(body);
            }
        }
        if !pipe_fill(h, buf) {
            return None;
        }
    }
}

unsafe fn pipe_write_msg(h: HANDLE, s: &str) {
    let body = s.as_bytes();
    let mut out = format!("{}\n", body.len()).into_bytes();
    out.extend_from_slice(body);
    let mut written: u32 = 0;
    WriteFile(
        h,
        out.as_ptr(),
        out.len() as u32,
        &mut written,
        std::ptr::null_mut(),
    );
}

/// Messages are `<opcode><payload>`:
///   E<lua>   evaluate now, reply with the result
///   R<lua>   install (or clear, if empty) the resident sampler chunk
///   I<n>     run the resident chunk every n pumps
///   S        read aggregated samples (one "<key> <fields> age=<n>" per line)
///   C        clear aggregated samples
///   W<path>  start recording ticks natively (caller must already have
///            installed a resident sampler via R/I). `path` empty means
///            auto-name under runs/<timestamp>.jsonl like record.py's own
///            default; otherwise used as-is for the first match and
///            rotated as "<stem>.mN<suffix>" for later matches in the same
///            session, same as record.py's open_recording()
///   X        stop recording (flushes and closes the file)
///   T        recording status: "recording=<0|1> ticks=<n> path=<path>"
///   P<text>  store the character-select cursor (empty or "-" clears)
///   K        read it back: "<text> age_ms=<n>", or "none"
///   G        connected XInput slots as the game sees them, e.g. "0 1"
///   L<ctx> <map>  travel to /Game/... via FCommonStatics::OpenLevel (any
///            time: runs on the game thread from the frame pump)
///   Z<n> <pad>..  lockstep: set virtual pads, run n frames, park (see lockstep.rs)
///   Zkeys mute|pass  drop keyboard messages while the gate is on (combo trainer)
///   N<n>     number of virtual XInput pads (slots 0..n)
///   F        frame pump status: "frames=<n> game_tid=<tid> lua=<bool>"
///   A<size>  zeroed block from the game's allocator: "0x<addr>"
///   U<obj> <ufunction> [hex parms]  call a UFunction on obj through
///            UObject::ProcessEvent on the game thread; replies with the parms
///            buffer as hex afterwards (return value and out params included)
fn handle(msg: &[u8]) -> String {
    let (op, rest) = match msg.split_first() {
        Some((op, rest)) => (*op, String::from_utf8_lossy(rest).into_owned()),
        None => return "!empty message".into(),
    };
    match op {
        b'R' => {
            if let Ok(mut r) = resident().lock() {
                *r = rest;
                RESIDENT_ON.store(if r.is_empty() { 0 } else { 1 }, Ordering::Relaxed);
                return format!("resident {} bytes", r.len());
            }
            "!resident lock poisoned".into()
        }
        b'I' => match rest.trim().parse::<usize>() {
            Ok(n) if n > 0 => {
                RESIDENT_EVERY.store(n, Ordering::Relaxed);
                format!("every {n}")
            }
            _ => "!bad interval".into(),
        },
        b'E' => dispatch(rest),
        b'F' => format!("frames={} game_tid={} lua={}", FRAMES.load(Ordering::Relaxed),
                        GAME_TID.load(Ordering::Relaxed), !STATE.load(Ordering::Relaxed).is_null()),
        b'S' => {
            if samples().lock().is_err() {
                return "!samples lock poisoned".into();
            }
            samples_text()
        }
        b'C' => {
            if let Ok(mut m) = samples().lock() {
                m.clear();
            }
            "cleared".into()
        }
        b'W' => {
            // Empty payload is valid here (unlike 'S'-adjacent opcodes): it
            // means auto-name under runs/<timestamp>.jsonl, matching
            // record.py's own default when no --out is given.
            let path = rest.trim().to_string();
            if let Ok(mut p) = record_want_path().lock() {
                *p = path.clone();
            } else {
                return "!record_want_path lock poisoned".into();
            }
            RECORD_WANT.store(1, Ordering::Relaxed);
            RECORD_TICKS.store(0, Ordering::Relaxed);
            if path.is_empty() {
                "recording (auto-named path)".into()
            } else {
                format!("recording to {path}")
            }
        }
        b'X' => {
            RECORD_WANT.store(0, Ordering::Relaxed);
            "recording stopped".into()
        }
        b'T' => {
            let recording = RECORD_WANT.load(Ordering::Relaxed);
            let ticks = RECORD_TICKS.load(Ordering::Relaxed);
            let path = record_session()
                .lock()
                .ok()
                .and_then(|g| g.as_ref().map(|s| s.current_path().to_string()))
                .unwrap_or_default();
            format!("recording={recording} ticks={ticks} path={path}")
        }
        b'P' => {
            let text = rest.trim().to_string();
            if let Ok(mut c) = cursor_slot().lock() {
                *c = if text.is_empty() || text == "-" {
                    None
                } else {
                    Some((text, std::time::Instant::now()))
                };
            }
            "ok".into()
        }
        b'K' => match cursor_slot().lock() {
            Ok(c) => match c.as_ref() {
                Some((text, at)) => {
                    let mut out = format!("{text} age_ms={}", at.elapsed().as_millis());
                    if let Some(addr) = text
                        .split_whitespace()
                        .find_map(|t| t.strip_prefix("addr="))
                        .and_then(|v| v.parse::<usize>().ok())
                    {
                        out.push_str(&read_select_infos(addr));
                        out.push_str(&read_control_settings(addr));
                    }
                    out
                }
                None => "none".into(),
            },
            Err(_) => "!cursor lock poisoned".into(),
        },
        b'D' => {
            // D<addr> <len>: hex dump of this process's memory (debug aid for
            // reverse-engineering game structs; ReadProcessMemory on self, so
            // a bad address returns an error instead of crashing).
            let mut it = rest.split_whitespace();
            let parse = |s: Option<&str>| {
                s.and_then(|t| match t.strip_prefix("0x") {
                    Some(h) => usize::from_str_radix(h, 16).ok(),
                    None => t.parse::<usize>().ok(),
                })
            };
            let (Some(addr), Some(len)) = (parse(it.next()), parse(it.next())) else {
                return "!usage: D<addr> <len>".into();
            };
            let len = len.min(8192);
            let mut buf = vec![0u8; len];
            let mut got = 0usize;
            let ok = unsafe {
                windows_sys::Win32::System::Diagnostics::Debug::ReadProcessMemory(
                    windows_sys::Win32::System::Threading::GetCurrentProcess(),
                    addr as *const c_void,
                    buf.as_mut_ptr() as *mut c_void,
                    len,
                    &mut got,
                )
            };
            if ok == 0 && got == 0 {
                return "!read failed".into();
            }
            buf.truncate(got);
            buf.iter().map(|b| format!("{b:02x}")).collect()
        }
        b'M' => {
            // M<addr> <hex bytes>: write into this process's memory (the match
            // setup lives in native fields of UBattleSettingWork that Lua can't
            // reach). WriteProcessMemory on self, so a bad address returns an
            // error instead of crashing.
            let mut it = rest.split_whitespace();
            let addr = it.next().and_then(|t| match t.strip_prefix("0x") {
                Some(h) => usize::from_str_radix(h, 16).ok(),
                None => t.parse::<usize>().ok(),
            });
            let hex = it.next().unwrap_or("");
            let bytes: Option<Vec<u8>> = (0..hex.len())
                .step_by(2)
                .map(|i| hex.get(i..i + 2).and_then(|h| u8::from_str_radix(h, 16).ok()))
                .collect();
            let (Some(addr), Some(bytes)) = (addr, bytes) else {
                return "!usage: M<addr> <hex>".into();
            };
            if bytes.is_empty() || bytes.len() > 8192 {
                return "!bad length".into();
            }
            let mut put = 0usize;
            let ok = unsafe {
                windows_sys::Win32::System::Diagnostics::Debug::WriteProcessMemory(
                    windows_sys::Win32::System::Threading::GetCurrentProcess(),
                    addr as *const c_void,
                    bytes.as_ptr() as *const c_void,
                    bytes.len(),
                    &mut put,
                )
            };
            if ok == 0 {
                return "!write failed".into();
            }
            format!("wrote {put}")
        }
        b'L' => {
            // L<ctx addr> <map path>: travel to a map through the game's own
            // FCommonStatics::OpenLevel (what the VS menu calls once the
            // setup is in UBattleSettingWork). ctx is any UObject that lives in
            // the current world, e.g. the PlayerController.
            let mut it = rest.splitn(2, char::is_whitespace);
            let ctx = it.next().and_then(|t| usize::from_str_radix(t.trim_start_matches("0x"), 16).ok());
            let path = it.next().unwrap_or("").trim().to_string();
            let Some(ctx) = ctx.filter(|_| path.starts_with("/Game/")) else {
                return "!usage: L<ctx addr> /Game/...".into();
            };
            run_native(Box::new(move || unsafe { open_level(ctx, &path) }))
        }
        b'A' => {
            // A<size>: zeroed memory from the game's own allocator (GMalloc,
            // FMallocBinned2; Malloc is vtable slot 5), so a container the
            // bot builds inside a native struct can later be grown or freed by
            // the game - the control-mode map of a fresh game session is empty
            // until the menus fill it.
            let Some(n) = rest.trim().parse::<usize>().ok().filter(|n| *n > 0 && *n <= 65536) else {
                return "!usage: A<size>".into();
            };
            unsafe {
                let gm = *((GetModuleHandleA(std::ptr::null()) as usize + RVA_GMALLOC) as *const usize);
                if gm == 0 {
                    return "!no GMalloc".into();
                }
                type Malloc = unsafe extern "system" fn(usize, usize, u32) -> *mut c_void;
                let malloc: Malloc = std::mem::transmute(*(*(gm as *const *const usize)).add(5));
                let p = malloc(gm, n, 16);
                if p.is_null() {
                    return "!malloc failed".into();
                }
                std::ptr::write_bytes(p as *mut u8, 0, n);
                format!("{:#x}", p as usize)
            }
        }
        b'U' => {
            // UE4SS's "ProcessEvent" in this build is a folded `ret 0` stub, so
            // every function call made through UE4SS silently does nothing.
            // The real one is UObject vtable slot 78 (RVA_PROCESS_EVENT).
            let mut it = rest.split_whitespace();
            let num = |t: Option<&str>| t.and_then(|t| usize::from_str_radix(t.trim_start_matches("0x"), 16).ok());
            let (Some(obj), Some(func)) = (num(it.next()), num(it.next())) else {
                return "!usage: U<obj> <ufunction> [hex parms]".into();
            };
            let hex = it.next().unwrap_or("");
            let bytes: Option<Vec<u8>> = (0..hex.len())
                .step_by(2)
                .map(|i| hex.get(i..i + 2).and_then(|h| u8::from_str_radix(h, 16).ok()))
                .collect();
            let Some(bytes) = bytes else { return "!bad parms hex".into() };
            if obj < 0x10000 || func < 0x10000 || bytes.len() > 4096 {
                return "!bad args".into();
            }
            run_native(Box::new(move || unsafe { call_ufunction(obj, func, &bytes) }))
        }
        b'G' => xinput_slots(),
        b'Z' | b'N' => lockstep::command(op, &rest),
        b'V' => vhook::command(&rest),
        b'O' => vhook::set_pose(&rest),
        _ => "!unknown opcode".into(),
    }
}

fn dispatch(src: String) -> String {
    if STATE.load(Ordering::Relaxed).is_null() {
        return "!no lua state yet (enter a battle first)".into();
    }
    let (tx, rx) = std::sync::mpsc::channel();
    if let Ok(mut q) = queue().lock() {
        q.push_back(Job { src, reply: tx, native: None });
        PENDING.fetch_add(1, Ordering::Relaxed);
    }
    // Short: while a match runs the pump fires constantly, so a stall means
    // the game is paused or in a menu. Failing fast keeps a recorder responsive
    // instead of blocking for seconds on every tick.
    rx.recv_timeout(std::time::Duration::from_millis(750))
        .unwrap_or_else(|_| "!not pumping (paused, or not in a match)".into())
}

/// Queue native work for the game thread and wait for it. Drained once per
/// frame by the PeekMessageW hook (menus, loading, title - any time), and by
/// the battle Lua pump as a fallback.
fn run_native(f: Box<dyn FnOnce() -> String + Send>) -> String {
    if GAME_TID.load(Ordering::Relaxed) == 0 && STATE.load(Ordering::Relaxed).is_null() {
        return "!no game-thread hook yet".into();
    }
    let (tx, rx) = std::sync::mpsc::channel();
    if let Ok(mut q) = native_queue().lock() {
        q.push_back(Job { src: String::new(), reply: tx, native: Some(f) });
        NATIVE_PENDING.fetch_add(1, Ordering::Relaxed);
    }
    rx.recv_timeout(std::time::Duration::from_millis(3000))
        .unwrap_or_else(|_| "!game thread not pumping".into())
}

// ---------------------------------------------------------------- frame pump
//
// The Lua pump only exists in battle. To run native calls (OpenLevel) from
// the title screen or menus, the main executable's PeekMessageW import is
// hooked: the engine pumps Windows messages once per frame on the game
// thread. Only calls from the process's first thread (the game thread) drain
// the queue - other threads with their own message loops pass straight on.
static ORIG_PEEK: AtomicUsize = AtomicUsize::new(0);
static GAME_TID: AtomicUsize = AtomicUsize::new(0);
static NATIVE_PENDING: AtomicUsize = AtomicUsize::new(0);
static IN_NATIVE: AtomicUsize = AtomicUsize::new(0);
static FRAMES: AtomicUsize = AtomicUsize::new(0);

fn native_queue() -> &'static Mutex<VecDeque<Job>> {
    static Q: OnceLock<Mutex<VecDeque<Job>>> = OnceLock::new();
    Q.get_or_init(|| Mutex::new(VecDeque::new()))
}

/// Game thread only.
unsafe fn drain_native() {
    if NATIVE_PENDING.load(Ordering::Relaxed) == 0 || IN_NATIVE.swap(1, Ordering::Acquire) != 0 {
        return;
    }
    loop {
        let job = native_queue().lock().ok().and_then(|mut q| q.pop_front());
        match job {
            Some(job) => {
                NATIVE_PENDING.fetch_sub(1, Ordering::Relaxed);
                let res = job.native.map(|f| f()).unwrap_or_default();
                let _ = job.reply.send(res);
            }
            None => break,
        }
    }
    IN_NATIVE.store(0, Ordering::Release);
}

type PeekFn = unsafe extern "system" fn(*mut c_void, *mut c_void, u32, u32, u32) -> BOOL;

unsafe extern "system" fn peek_hook(msg: *mut c_void, hwnd: *mut c_void, lo: u32, hi: u32, rm: u32) -> BOOL {
    use windows_sys::Win32::System::Threading::GetCurrentThreadId;
    if GetCurrentThreadId() as usize == GAME_TID.load(Ordering::Relaxed) {
        FRAMES.fetch_add(1, Ordering::Relaxed);
        drain_native();
        lockstep::on_pump(drain_native, log);
    }
    let orig: PeekFn = std::mem::transmute(ORIG_PEEK.load(Ordering::Relaxed));
    loop {
        let r = orig(msg, hwnd, lo, hi, rm);
        if r == 0 || msg.is_null() || !lockstep::keys_muted() {
            return r;
        }
        // MSG.message at +8: keys, chars and the middle button (bindable).
        let m = *((msg as *const u8).add(8) as *const u32);
        if !matches!(m, 0x100..=0x109 | 0x207..=0x209) {
            return r;
        }
        if rm & 1 == 0 {
            orig(msg, hwnd, m, m, 1); // a PM_NOREMOVE peek: take it out of the queue
        }
    }
}

/// The process's first thread (earliest creation time) - UE's game thread.
unsafe fn main_thread_id() -> u32 {
    use windows_sys::Win32::Foundation::FILETIME;
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
    };
    use windows_sys::Win32::System::Threading::{
        GetCurrentProcessId, GetThreadTimes, OpenThread, THREAD_QUERY_LIMITED_INFORMATION,
    };
    let pid = GetCurrentProcessId();
    let snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if snap == INVALID_HANDLE_VALUE {
        return 0;
    }
    let mut te: THREADENTRY32 = std::mem::zeroed();
    te.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;
    let (mut best, mut best_t) = (0u32, u64::MAX);
    let mut ok = Thread32First(snap, &mut te);
    while ok != 0 {
        if te.th32OwnerProcessID == pid {
            let h = OpenThread(THREAD_QUERY_LIMITED_INFORMATION, 0, te.th32ThreadID);
            if !h.is_null() {
                let z = FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
                let (mut c, mut e, mut k, mut u) = (z, z, z, z);
                if GetThreadTimes(h, &mut c, &mut e, &mut k, &mut u) != 0 {
                    let t = ((c.dwHighDateTime as u64) << 32) | c.dwLowDateTime as u64;
                    if t < best_t {
                        best_t = t;
                        best = te.th32ThreadID;
                    }
                }
                CloseHandle(h);
            }
        }
        ok = Thread32Next(snap, &mut te);
    }
    CloseHandle(snap);
    best
}

fn install_frame_pump() {
    unsafe {
        let tid = main_thread_id();
        if tid == 0 {
            log("frame pump: no main thread found");
            return;
        }
        GAME_TID.store(tid as usize, Ordering::Relaxed);
        lockstep::set_game_tid(tid);
        let exe = GetModuleHandleA(std::ptr::null()) as *mut u8;
        match hook_iat(exe, "user32.dll", "PeekMessageW", peek_hook as *const c_void) {
            Some(orig) => {
                ORIG_PEEK.store(orig, Ordering::Relaxed);
                log(&format!("frame pump: hooked PeekMessageW, game thread {tid}"));
            }
            None => {
                GAME_TID.store(0, Ordering::Relaxed);
                log("frame pump: PeekMessageW not in the import table");
            }
        }
    }
}

// AJGAME.exe RVAs (image base 0x140000000). FName ctor from UE4SS's scan.
const RVA_FNAME_CTOR: usize = 0x120f780; // FName::FName(const TCHAR*, EFindName)
const RVA_GMALLOC: usize = 0xa1e3820; // FMalloc* GMalloc
const RVA_COMMON_OPEN_LEVEL: usize = 0x4914a90; // FCommonStatics::OpenLevel
const RVA_PROCESS_EVENT: usize = 0x141d850; // UObject::ProcessEvent (vtable slot 78)

/// Game thread only. The parms buffer is at least the UFunction's ParmsSize
/// (u16 at +0xb6) and 16-byte aligned; the caller's bytes fill its start.
unsafe fn call_ufunction(obj: usize, func: usize, parms: &[u8]) -> String {
    let readable = |a: usize, n: usize| {
        let mut b = vec![0u8; n];
        let mut got = 0usize;
        windows_sys::Win32::System::Diagnostics::Debug::ReadProcessMemory(
            windows_sys::Win32::System::Threading::GetCurrentProcess(),
            a as *const c_void, b.as_mut_ptr() as *mut c_void, n, &mut got) != 0 && got == n
    };
    if !readable(obj, 0x30) || !readable(func, 0xc0) {
        return "!object or function not readable".into();
    }
    let size = (*((func + 0xb6) as *const u16) as usize).max(parms.len());
    let mut buf = vec![0u128; size.div_ceil(16).max(1)];
    std::ptr::copy_nonoverlapping(parms.as_ptr(), buf.as_mut_ptr() as *mut u8, parms.len());
    let base = GetModuleHandleA(std::ptr::null()) as usize;
    type Pe = unsafe extern "system" fn(usize, usize, *mut c_void);
    let pe: Pe = std::mem::transmute(base + RVA_PROCESS_EVENT);
    pe(obj, func, buf.as_mut_ptr() as *mut c_void);
    std::slice::from_raw_parts(buf.as_ptr() as *const u8, size)
        .iter().map(|b| format!("{b:02x}")).collect()
}

#[repr(C)]
struct FStr {
    data: *const u16,
    num: i32,
    max: i32,
}

/// Game thread only.
unsafe fn open_level(ctx: usize, path: &str) -> String {
    let base = GetModuleHandleA(std::ptr::null()) as usize;
    type Ctor = unsafe extern "system" fn(*mut u64, *const u16, i32) -> *mut u64;
    type Open = unsafe extern "system" fn(usize, u64, u8, *const FStr);
    let ctor: Ctor = std::mem::transmute(base + RVA_FNAME_CTOR);
    let open: Open = std::mem::transmute(base + RVA_COMMON_OPEN_LEVEL);
    let w = wide(path);
    let mut name: u64 = 0;
    ctor(&mut name, w.as_ptr(), 1); // FNAME_Add
    if name == 0 {
        return "!fname failed".into();
    }
    let opts = FStr { data: std::ptr::null(), num: 0, max: 0 };
    open(ctx, name, 1, &opts);
    format!("open {path} fname={name:#x}")
}

/// FGenericWindow::GetFullScreenInfo (RVA 0x14ac5c0) is fatal unless the
/// command line has -RenderOffScreen. With -nullrhi the game window is a
/// generic one and UCommonWork::Initialize calls it right at boot; under
/// Proton the -RenderOffScreen test still fails. The function only returns
/// false anyway, so its `jne` past the fatal becomes a `jmp`.
fn patch_headless() {
    const RVA_JNE: usize = 0x14ac5da;
    unsafe {
        let p = (GetModuleHandleA(std::ptr::null()) as usize + RVA_JNE) as *mut u8;
        if *p != 0x75 || *p.add(1) != 0x13 {
            log("headless: GetFullScreenInfo check not where expected, left alone");
            return;
        }
        let mut old: PAGE_PROTECTION_FLAGS = 0;
        VirtualProtect(p as *const c_void, 1, PAGE_EXECUTE_READWRITE, &mut old);
        *p = 0xEB;
        VirtualProtect(p as *const c_void, 1, old, &mut old);
        log("headless: GetFullScreenInfo fatal disabled");
    }
}

const PIPE_INSTANCES: u32 = 4;

/// Several server threads, each owning its own instance of the pipe. With a
/// single instance one client that never disconnects cleanly wedges the server
/// for good, and every later connection gets ERROR_PIPE_BUSY.
fn serve() {
    log(&format!("serving {PIPE_NAME} x{PIPE_INSTANCES}"));
    std::thread::spawn(serve_tcp);
    for _ in 1..PIPE_INSTANCES {
        std::thread::spawn(serve_one);
    }
    serve_one();
}

fn serve_one() {
    let name = wide(PIPE_NAME);
    loop {
        let h = unsafe {
            CreateNamedPipeW(
                name.as_ptr(),
                PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                PIPE_INSTANCES,
                1 << 16,
                1 << 16,
                0,
                std::ptr::null(),
            )
        };
        if h == INVALID_HANDLE_VALUE {
            log("CreateNamedPipeW failed");
            std::thread::sleep(std::time::Duration::from_secs(2));
            continue;
        }
        unsafe {
            ConnectNamedPipe(h, std::ptr::null_mut());
            let mut buf = Vec::new();
            while let Some(msg) = pipe_read_msg(h, &mut buf) {
                let reply = handle(&msg);
                pipe_write_msg(h, &reply);
            }
            DisconnectNamedPipe(h);
            CloseHandle(h);
        }
    }
}

// ------------------------------------------------------- loopback TCP console
//
// Same wire format and opcodes as the pipe, on 127.0.0.1 only. It exists for
// Linux: under Proton the game is a Wine process, and a native Linux Python
// cannot open a Wine named pipe, but Wine's Winsock is a real Linux socket.
// Bound to loopback, so it is no more reachable than the pipe. The port can be
// moved with AJHOOK_PORT (set it in the Steam launch options); 0 disables it.

const TCP_PORT_DEFAULT: u16 = 47380;

fn serve_tcp() {
    let port = std::env::var("AJHOOK_PORT")
        .ok()
        .and_then(|v| v.trim().parse::<u16>().ok())
        .unwrap_or(TCP_PORT_DEFAULT);
    if port == 0 {
        return;
    }
    let listener = loop {
        match std::net::TcpListener::bind(("127.0.0.1", port)) {
            Ok(l) => break l,
            Err(e) => {
                log(&format!("tcp bind 127.0.0.1:{port} failed: {e}"));
                std::thread::sleep(std::time::Duration::from_secs(5));
            }
        }
    };
    log(&format!("serving tcp 127.0.0.1:{port}"));
    for conn in listener.incoming() {
        let Ok(stream) = conn else { continue };
        std::thread::spawn(move || tcp_client(stream));
    }
}

fn tcp_client(mut s: std::net::TcpStream) {
    use std::io::{Read, Write};
    let _ = s.set_nodelay(true);
    let mut buf: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        // Same framing as pipe_read_msg: "<len>\n" then len bytes.
        let msg = loop {
            if let Some(nl) = buf.iter().position(|&b| b == b'\n') {
                let Some(len) = std::str::from_utf8(&buf[..nl])
                    .ok()
                    .and_then(|t| t.trim().parse::<usize>().ok())
                else {
                    return;
                };
                if buf.len() >= nl + 1 + len {
                    let body = buf[nl + 1..nl + 1 + len].to_vec();
                    buf.drain(..nl + 1 + len);
                    break body;
                }
            }
            match s.read(&mut chunk) {
                Ok(0) | Err(_) => return,
                Ok(n) => buf.extend_from_slice(&chunk[..n]),
            }
        };
        let reply = handle(&msg);
        let mut out = format!("{}\n", reply.len()).into_bytes();
        out.extend_from_slice(reply.as_bytes());
        if s.write_all(&out).is_err() {
            return;
        }
    }
}

/// XInput slots the game process itself sees as connected, e.g. "0 1".
/// Python on Windows asks XInput directly; under Proton only the game's own
/// (Wine) XInput knows which player a uinput pad became, so it asks here.
fn xinput_slots() -> String {
    type GetState = unsafe extern "system" fn(u32, *mut [u32; 4]) -> u32;
    static FN: OnceLock<usize> = OnceLock::new();
    let f = *FN.get_or_init(|| unsafe {
        for dll in [c"xinput1_4.dll", c"xinput1_3.dll", c"xinput9_1_0.dll"] {
            let m = windows_sys::Win32::System::LibraryLoader::LoadLibraryA(dll.as_ptr() as *const u8);
            if m.is_null() {
                continue;
            }
            if let Some(p) = GetProcAddress(m, c"XInputGetState".as_ptr() as *const u8) {
                return p as usize;
            }
        }
        0
    });
    if f == 0 {
        return "!no xinput".into();
    }
    // The game's own view, virtual lockstep pads included.
    let f = if lockstep::installed() { lockstep::get_state_hook as *const () as usize } else { f };
    let get: GetState = unsafe { std::mem::transmute(f) };
    let mut st = [0u32; 4];
    (0..4u32)
        .filter(|&i| unsafe { get(i, &mut st) } == 0)
        .map(|i| i.to_string())
        .collect::<Vec<_>>()
        .join(" ")
}

fn init() {
    // panic = "abort": a panic anywhere in here takes the whole game down with
    // 0xc0000409 and no other trace. Record what and where first.
    std::panic::set_hook(Box::new(|info| {
        let loc = info.location().map(|l| format!("{}:{}", l.file(), l.line())).unwrap_or_default();
        let msg = info.payload().downcast_ref::<&str>().map(|s| s.to_string())
            .or_else(|| info.payload().downcast_ref::<String>().cloned())
            .unwrap_or_default();
        log(&format!("PANIC at {loc}: {msg}"));
    }));
    patch_headless();
    // Before the wait: UE polls every XInput slot once at startup and then
    // only the slots it saw connected, so virtual pads must answer that poll.
    log(&lockstep::install(0));
    std::thread::sleep(std::time::Duration::from_secs(5));
    install_frame_pump();

    let lua_mod: HMODULE = unsafe { GetModuleHandleA(c"Lua524.dll".as_ptr() as *const u8) };
    if lua_mod.is_null() {
        log("Lua524.dll not loaded");
        return;
    }

    macro_rules! sym {
        ($n:literal) => {
            match GetProcAddress(lua_mod, concat!($n, "\0").as_ptr()) {
                Some(p) => p as usize,
                None => {
                    log(concat!("missing export ", $n));
                    return;
                }
            }
        };
    }

    let api = lua::Api {
        load_bufferx: unsafe { std::mem::transmute(sym!("luaL_loadbufferx")) },
        pcallk: unsafe { std::mem::transmute(sym!("lua_pcallk")) },
        tolstring: unsafe { std::mem::transmute(sym!("lua_tolstring")) },
        settop: unsafe { std::mem::transmute(sym!("lua_settop")) },
        gettop: unsafe { std::mem::transmute(sym!("lua_gettop")) },
    };
    let _ = API.set(api);

    // The game runs its per-frame action scripts through lua_callk; lua_pcallk
    // fires far less often. Hook both so the pump keeps ticking.
    //
    // Only patching the MAIN executable's import table meant a CPU-controlled
    // fighter's lua_State never produced a single sample: lua_pcall/lua_call
    // (the classic, non-continuation forms) are not even in that module's
    // import table, so whatever drives CPU logic either calls through a
    // DIFFERENT module's own copy of the import table entirely, or doesn't
    // route through these entry points from the main module at all. So every
    // loaded module gets scanned and patched wherever it actually imports
    // one of these by name, not just the main EXE.
    let mut hooked = 0;
    let mods = unsafe { all_modules() };
    log(&format!("scanning {} loaded modules for lua entry points", mods.len()));
    for (base, name) in &mods {
        // The snapshot is only a list of addresses: a module can unload between
        // the snapshot and the header read below. DXCore does exactly that at
        // startup, and reading its freed header crashed the game (AV at the
        // module base, ajhook+0x9e82, dumps from 2026-09-28 and 2026-10-23).
        // Pin the module for the duration of the patch; skip it if it is gone.
        let mut pinned: HMODULE = std::ptr::null_mut();
        let ok = unsafe {
            GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS,
                               *base as *const u16, &mut pinned)
        };
        if ok == 0 || pinned.is_null() || pinned as *mut u8 != *base {
            continue;
        }
        macro_rules! try_hook {
            ($fname:literal, $repl:expr, $store:expr) => {
                if let Some(orig) =
                    unsafe { hook_iat(*base, "lua524.dll", $fname, $repl as *const c_void) }
                {
                    if $store.load(Ordering::Relaxed) == 0 {
                        $store.store(orig, Ordering::Relaxed);
                    }
                    log(&format!("hooked {} in {}", $fname, name));
                    hooked += 1;
                }
            };
        }
        try_hook!("lua_pcallk", pcallk_hook, ORIG_PCALLK);
        try_hook!("lua_callk", callk_hook, ORIG_CALLK);
        try_hook!("lua_pcall", pcall_hook, ORIG_PCALL);
        try_hook!("lua_call", call_hook, ORIG_CALL);
        unsafe { FreeLibrary(pinned) };
    }
    if hooked == 0 {
        log("could not hook any lua entry point in any loaded module (not imported by name?)");
        return;
    }

    serve();
}

#[no_mangle]
pub extern "system" fn DllMain(_h: HMODULE, reason: u32, _r: *mut c_void) -> BOOL {
    if reason == 1 {
        std::thread::spawn(init);
    }
    1
}

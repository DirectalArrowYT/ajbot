//! Virtual-function hooks on game objects, for taking over the battle camera.
//!
//! The battle camera (`CameraBattle`, BattleModule) computes its view natively
//! every frame, so neither moving its actor/component nor writing the camera
//! manager's cached POV from Lua sticks. Instead:
//!
//! 1. `probe`: replace a range of the camera's vtable entries with tiny
//!    pass-through stubs that count calls and record rcx/rdx/r8, then jump to
//!    the original. They touch only r11, so they are safe for any signature.
//!    The slot whose third argument (r8) is the camera manager's view struct
//!    is `CalcCamera(this, float DeltaTime, FMinimalViewInfo& Out)`.
//! 2. `install_view`: point that slot at `view_hook`, which calls the original
//!    and then, while a pose is set (opcode `O`), overwrites the result's
//!    location / rotation / FOV. UE 5.4 FMinimalViewInfo starts with
//!    FVector Location (3 doubles), FRotator Rotation (3 doubles: pitch, yaw,
//!    roll), float FOV.
use std::ffi::c_void;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

use windows_sys::Win32::System::Memory::{
    VirtualAlloc, VirtualProtect, MEM_COMMIT, MEM_RESERVE, PAGE_EXECUTE_READWRITE,
    PAGE_PROTECTION_FLAGS, PAGE_READWRITE,
};

const STUB_SIZE: usize = 48;
const DATA_SIZE: usize = 40; // count, rdx, r8, rcx, orig

struct Probe {
    vtable: usize,
    first: usize,
    n: usize,     // slots actually installed
    alloc: usize, // slots the stub/data blocks were sized for
    mem: usize,   // stubs then data
}

static PROBE: Mutex<Option<Probe>> = Mutex::new(None);
static VIEW_ORIG: AtomicUsize = AtomicUsize::new(0);
static VIEW_SLOT: Mutex<Option<(usize, usize)>> = Mutex::new(None); // (vtable, slot)
static VIEW_CALLS: AtomicUsize = AtomicUsize::new(0);
static POSE: Mutex<Option<[f64; 7]>> = Mutex::new(None);

unsafe fn write_slot(vtable: usize, slot: usize, value: usize) -> usize {
    let p = (vtable + slot * 8) as *mut usize;
    let mut old: PAGE_PROTECTION_FLAGS = 0;
    VirtualProtect(p as *const c_void, 8, PAGE_READWRITE, &mut old);
    let prev = std::ptr::read_volatile(p);
    std::ptr::write_volatile(p, value);
    VirtualProtect(p as *const c_void, 8, old, &mut old);
    prev
}

fn readable(addr: usize) -> bool {
    crate::read_bytes(addr, 1).is_some()
}

fn executable(addr: usize) -> bool {
    use windows_sys::Win32::System::Memory::{VirtualQuery, MEMORY_BASIC_INFORMATION};
    let mut mbi: MEMORY_BASIC_INFORMATION = unsafe { std::mem::zeroed() };
    let got = unsafe {
        VirtualQuery(addr as *const c_void, &mut mbi, std::mem::size_of::<MEMORY_BASIC_INFORMATION>())
    };
    // PAGE_EXECUTE (0x10) | _READ (0x20) | _READWRITE (0x40) | _WRITECOPY (0x80)
    got != 0 && mbi.State == MEM_COMMIT && (mbi.Protect & 0xF0) != 0
}

/// Install counting stubs on vtable[first .. first+n].
pub fn probe(vtable: usize, first: usize, n: usize) -> String {
    let mut g = match PROBE.lock() {
        Ok(g) => g,
        Err(_) => return "!probe lock".into(),
    };
    if g.is_some() {
        return "!already probing; V unprobe first".into();
    }
    if vtable == 0 || n == 0 || n > 512 || !readable(vtable + (first + n) * 8 - 1) {
        return "!bad vtable range".into();
    }
    let mem = unsafe {
        VirtualAlloc(std::ptr::null(), n * (STUB_SIZE + DATA_SIZE),
                     MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE)
    } as usize;
    if mem == 0 {
        return "!VirtualAlloc failed".into();
    }
    let data_base = mem + n * STUB_SIZE;
    let mut installed = 0;
    for i in 0..n {
        let slot = first + i;
        let orig = unsafe { std::ptr::read_volatile((vtable + slot * 8) as *const usize) };
        if orig == 0 || !executable(orig) {
            break; // end of the vtable: what follows is not code pointers
        }
        let data = data_base + i * DATA_SIZE;
        unsafe {
            std::ptr::write_bytes(data as *mut u8, 0, DATA_SIZE);
            *((data + 32) as *mut usize) = orig;
            let s = (mem + i * STUB_SIZE) as *mut u8;
            let mut code: Vec<u8> = Vec::with_capacity(STUB_SIZE);
            code.extend_from_slice(&[0x49, 0xBB]); // mov r11, imm64
            code.extend_from_slice(&(data as u64).to_le_bytes());
            code.extend_from_slice(&[0xF0, 0x49, 0xFF, 0x03]); // lock inc qword [r11]
            code.extend_from_slice(&[0x49, 0x89, 0x53, 0x08]); // mov [r11+8], rdx
            code.extend_from_slice(&[0x4D, 0x89, 0x43, 0x10]); // mov [r11+16], r8
            code.extend_from_slice(&[0x49, 0x89, 0x4B, 0x18]); // mov [r11+24], rcx
            code.extend_from_slice(&[0x41, 0xFF, 0x63, 0x20]); // jmp qword [r11+32]
            std::ptr::copy_nonoverlapping(code.as_ptr(), s, code.len());
            write_slot(vtable, slot, s as usize);
        }
        installed += 1;
    }
    *g = Some(Probe { vtable, first, n: installed, alloc: n, mem });
    format!("probing {installed}/{n} slots from {first}")
}

pub fn stats() -> String {
    let g = match PROBE.lock() {
        Ok(g) => g,
        Err(_) => return "!probe lock".into(),
    };
    let Some(p) = g.as_ref() else { return "!not probing".into() };
    let data_base = p.mem + p.alloc * STUB_SIZE;
    let mut out = String::new();
    for i in 0..p.n {
        let d = data_base + i * DATA_SIZE;
        let (count, rdx, r8, rcx) = unsafe {
            (*(d as *const usize), *((d + 8) as *const usize),
             *((d + 16) as *const usize), *((d + 24) as *const usize))
        };
        if count > 0 {
            out.push_str(&format!("{} {} {:#x} {:#x} {:#x}\n", p.first + i, count, rcx, rdx, r8));
        }
    }
    if out.is_empty() { "none".into() } else { out }
}

pub fn unprobe() -> String {
    let mut g = match PROBE.lock() {
        Ok(g) => g,
        Err(_) => return "!probe lock".into(),
    };
    let Some(p) = g.take() else { return "!not probing".into() };
    let data_base = p.mem + p.alloc * STUB_SIZE;
    let mut restored = 0;
    for i in 0..p.n {
        let orig = unsafe { *((data_base + i * DATA_SIZE + 32) as *const usize) };
        if orig != 0 {
            unsafe { write_slot(p.vtable, p.first + i, orig) };
            restored += 1;
        }
    }
    // The stub memory is deliberately leaked: another thread may still be
    // executing a stub, and freeing it under it would crash the game.
    format!("restored {restored} slots")
}

type ViewFn = unsafe extern "system" fn(*mut c_void, f32, *mut u8);

unsafe extern "system" fn view_hook(this: *mut c_void, dt: f32, out: *mut u8) {
    let orig = VIEW_ORIG.load(Ordering::Relaxed);
    if orig != 0 {
        let f: ViewFn = std::mem::transmute(orig);
        f(this, dt, out);
    }
    VIEW_CALLS.fetch_add(1, Ordering::Relaxed);
    if out.is_null() {
        return;
    }
    if let Ok(g) = POSE.try_lock() {
        if let Some(p) = *g {
            let d = out as *mut f64;
            for k in 0..6 {
                *d.add(k) = p[k];
            }
            *(out.add(48) as *mut f32) = p[6] as f32;
        }
    }
}

static VP_ORIG: AtomicUsize = AtomicUsize::new(0);
static VP_SLOT: Mutex<Option<(usize, usize)>> = Mutex::new(None);
static VP_CALLS: AtomicUsize = AtomicUsize::new(0);

type VpFn = unsafe extern "system" fn(*mut c_void, *mut f64, *mut f64);

/// APlayerController::GetPlayerViewPoint(FVector& Location, FRotator& Rotation):
/// what ULocalPlayer::GetViewPoint actually renders from. The battle
/// controller overrides it to read its own camera, so overriding the camera
/// manager alone never reached the screen.
unsafe extern "system" fn vp_hook(this: *mut c_void, loc: *mut f64, rot: *mut f64) {
    let orig = VP_ORIG.load(Ordering::Relaxed);
    if orig != 0 {
        let f: VpFn = std::mem::transmute(orig);
        f(this, loc, rot);
    }
    VP_CALLS.fetch_add(1, Ordering::Relaxed);
    if loc.is_null() || rot.is_null() {
        return;
    }
    if let Ok(g) = POSE.try_lock() {
        if let Some(p) = *g {
            for k in 0..3 {
                *loc.add(k) = p[k];
                *rot.add(k) = p[3 + k];
            }
        }
    }
}

pub fn install_vp(vtable: usize, slot: usize) -> String {
    if !readable(vtable + slot * 8) {
        return "!bad vtable".into();
    }
    let mut vs = match VP_SLOT.lock() {
        Ok(v) => v,
        Err(_) => return "!vp lock".into(),
    };
    if vs.is_some() {
        return "!viewpoint hook already installed".into();
    }
    let cur = unsafe { std::ptr::read_volatile((vtable + slot * 8) as *const usize) };
    VP_ORIG.store(cur, Ordering::Relaxed);
    unsafe { write_slot(vtable, slot, vp_hook as usize) };
    *vs = Some((vtable, slot));
    format!("viewpoint hook on slot {slot} (orig {cur:#x})")
}

pub fn uninstall_vp() -> String {
    let mut vs = match VP_SLOT.lock() {
        Ok(v) => v,
        Err(_) => return "!vp lock".into(),
    };
    let Some((vt, slot)) = vs.take() else { return "!no viewpoint hook".into() };
    unsafe { write_slot(vt, slot, VP_ORIG.load(Ordering::Relaxed)) };
    "viewpoint hook removed".into()
}

static LP_ORIG: AtomicUsize = AtomicUsize::new(0);
static LP_SLOT: Mutex<Option<(usize, usize)>> = Mutex::new(None);
static LP_CALLS: AtomicUsize = AtomicUsize::new(0);
static LP_WRITES: AtomicUsize = AtomicUsize::new(0);

type LpFn = unsafe extern "system" fn(*mut c_void, *mut u8);

/// ULocalPlayer::GetViewPoint(FMinimalViewInfo& OutViewInfo) const - the
/// renderer's source for the view. Writes only when the original produced
/// something shaped like a view (finite location, FOV 1..179), so a wrong
/// slot guess cannot scribble over unrelated memory.
unsafe extern "system" fn lp_hook(this: *mut c_void, out: *mut u8) {
    let orig = LP_ORIG.load(Ordering::Relaxed);
    if orig != 0 {
        let f: LpFn = std::mem::transmute(orig);
        f(this, out);
    }
    LP_CALLS.fetch_add(1, Ordering::Relaxed);
    if out.is_null() {
        return;
    }
    let d = out as *mut f64;
    let fov = *(out.add(48) as *const f32);
    let sane = (0..6).all(|k| (*d.add(k)).is_finite() && (*d.add(k)).abs() < 1.0e7)
        && fov > 1.0 && fov < 179.0;
    if !sane {
        return;
    }
    if let Ok(g) = POSE.try_lock() {
        if let Some(p) = *g {
            for k in 0..6 {
                *d.add(k) = p[k];
            }
            *(out.add(48) as *mut f32) = p[6] as f32;
            LP_WRITES.fetch_add(1, Ordering::Relaxed);
        }
    }
}

pub fn install_lp(vtable: usize, slot: usize) -> String {
    if !readable(vtable + slot * 8) {
        return "!bad vtable".into();
    }
    let mut vs = match LP_SLOT.lock() {
        Ok(v) => v,
        Err(_) => return "!lp lock".into(),
    };
    if vs.is_some() {
        return "!localplayer hook already installed".into();
    }
    let cur = unsafe { std::ptr::read_volatile((vtable + slot * 8) as *const usize) };
    LP_ORIG.store(cur, Ordering::Relaxed);
    unsafe { write_slot(vtable, slot, lp_hook as usize) };
    *vs = Some((vtable, slot));
    format!("localplayer hook on slot {slot} (orig {cur:#x})")
}

pub fn uninstall_lp() -> String {
    let mut vs = match LP_SLOT.lock() {
        Ok(v) => v,
        Err(_) => return "!lp lock".into(),
    };
    let Some((vt, slot)) = vs.take() else { return "!no localplayer hook".into() };
    unsafe { write_slot(vt, slot, LP_ORIG.load(Ordering::Relaxed)) };
    "localplayer hook removed".into()
}

pub fn install_view(vtable: usize, slot: usize) -> String {
    if !readable(vtable + slot * 8) {
        return "!bad vtable".into();
    }
    let mut vs = match VIEW_SLOT.lock() {
        Ok(v) => v,
        Err(_) => return "!view lock".into(),
    };
    if vs.is_some() {
        return "!view hook already installed".into();
    }
    let cur = unsafe { std::ptr::read_volatile((vtable + slot * 8) as *const usize) };
    VIEW_ORIG.store(cur, Ordering::Relaxed);
    unsafe { write_slot(vtable, slot, view_hook as usize) };
    *vs = Some((vtable, slot));
    format!("view hook on slot {slot} (orig {cur:#x})")
}

pub fn uninstall_view() -> String {
    let mut vs = match VIEW_SLOT.lock() {
        Ok(v) => v,
        Err(_) => return "!view lock".into(),
    };
    let Some((vt, slot)) = vs.take() else { return "!no view hook".into() };
    let orig = VIEW_ORIG.load(Ordering::Relaxed);
    unsafe { write_slot(vt, slot, orig) };
    if let Ok(mut p) = POSE.lock() {
        *p = None;
    }
    "view hook removed".into()
}

fn counters() -> String {
    format!("view {} vp {} lp {} lp_writes {}",
            VIEW_CALLS.load(Ordering::Relaxed), VP_CALLS.load(Ordering::Relaxed),
            LP_CALLS.load(Ordering::Relaxed), LP_WRITES.load(Ordering::Relaxed))
}

/// `O off` or `O x y z pitch yaw roll fov`; `O ?` reports counters.
pub fn set_pose(rest: &str) -> String {
    let t = rest.trim();
    if t == "?" {
        return counters();
    }
    if t == "dump" {
        return gen_dump();
    }
    if let Some(r) = t.strip_prefix("follow") {
        let v: Vec<f64> = r.split_whitespace().filter_map(|x| x.parse().ok()).collect();
        let mut g = match FOLLOW.lock() {
            Ok(g) => g,
            Err(_) => return "!follow lock".into(),
        };
        if v.len() < 3 {
            *g = None;
            return "follow off".into();
        }
        let mut p = [0.0f64; 11];
        let defaults = [420.0, 90.0, 120.0, 100.0, 0.0, 0.0, 50.0, 0.0, 0.0, 0.0, 0.0];
        for k in 0..11 {
            p[k] = *v.get(3 + k).unwrap_or(&defaults[k]);
        }
        *g = Some(Follow { root: v[0] as usize, loc_off: v[1] as usize, rot_off: v[2] as usize, p });
        drop(g);
        return format!("follow on {:?}", follow_pose());
    }
    if let Some(r) = t.strip_prefix("raw") {
        let v: Vec<f64> = r.split_whitespace().filter_map(|x| x.parse().ok()).collect();
        if let Ok(mut g) = RAW.lock() {
            *g = if v.len() >= 2 { Some((v[0] as usize, v[1..].to_vec())) } else { None };
        }
        return format!("raw {} doubles", v.len().saturating_sub(1));
    }
    let mut g = match POSE.lock() {
        Ok(g) => g,
        Err(_) => return "!pose lock".into(),
    };
    if t.is_empty() || t == "off" {
        *g = None;
        if let Ok(mut f) = FOLLOW.lock() {
            *f = None;
        }
        return format!("pose off ({})", counters());
    }
    let v: Vec<f64> = t.split_whitespace().filter_map(|s| s.parse().ok()).collect();
    if v.len() != 7 {
        return "!usage: O x y z pitch yaw roll fov".into();
    }
    *g = Some([v[0], v[1], v[2], v[3], v[4], v[5], v[6]]);
    format!("pose on ({})", counters())
}

/// `V probe <vtable> <first> <n>` / `V stats` / `V unprobe` /
/// `V view <vtable> <slot>` / `V unview`.
pub fn command(rest: &str) -> String {
    let mut it = rest.split_whitespace();
    let num = |s: Option<&str>| -> Option<usize> {
        s.and_then(|t| match t.strip_prefix("0x") {
            Some(h) => usize::from_str_radix(h, 16).ok(),
            None => t.parse().ok(),
        })
    };
    match it.next() {
        Some("probe") => match (num(it.next()), num(it.next()), num(it.next())) {
            (Some(vt), Some(f), Some(n)) => probe(vt, f, n),
            _ => "!usage: V probe <vtable> <first> <n>".into(),
        },
        Some("stats") => stats(),
        Some("unprobe") => unprobe(),
        Some("view") => match (num(it.next()), num(it.next())) {
            (Some(vt), Some(s)) => install_view(vt, s),
            _ => "!usage: V view <vtable> <slot>".into(),
        },
        Some("unview") => uninstall_view(),
        Some("vp") => match (num(it.next()), num(it.next())) {
            (Some(vt), Some(s)) => install_vp(vt, s),
            _ => "!usage: V vp <vtable> <slot>".into(),
        },
        Some("unvp") => uninstall_vp(),
        Some("lp") => match (num(it.next()), num(it.next())) {
            (Some(vt), Some(s)) => install_lp(vt, s),
            _ => "!usage: V lp <vtable> <slot>".into(),
        },
        Some("unlp") => uninstall_lp(),
        Some("gen") => {
            let v: Vec<usize> = it.filter_map(|t| match t.strip_prefix("0x") {
                Some(h) => usize::from_str_radix(h, 16).ok(),
                None => t.parse().ok(),
            }).collect();
            if v.len() != 6 {
                return "!usage: V gen <i> <vtable> <slot> <reg 1-3> <mode 0-2> <guard 0/1>".into();
            }
            install_gen(v[0], v[1], v[2], v[3], v[4], v[5])
        }
        Some("ungen") => match num(it.next()) {
            Some(i) => uninstall_gen(i),
            None => "!usage: V ungen <i>".into(),
        },
        _ => "!usage: V probe|stats|unprobe|view|unview".into(),
    }
}

// ------------------------------------------------------------ generic hooks
//
// Four reusable vtable hooks, each configurable at run time so trying another
// slot / signature does not need a rebuild and a game restart:
//   reg   which argument register holds the output pointer: 1=rdx 2=r8 3=r9
//   mode  0 = FMinimalViewInfo at out (loc, rot doubles; FOV f32 at +48)
//         1 = location/rotation pair: rdx = FVector&, r8 = FRotator&
//         2 = FMinimalViewInfo without touching FOV
//         3 = FSceneViewProjectionData (origin, view rotation matrix, FOV via
//             the projection matrix) - what the renderer actually reads
//         4 = raw doubles from `O raw <offset> v...`
//   guard 1 = only write when the original's output looks like a view
// After every call the 64 bytes at the output pointer are copied for `O dump`.
// The original is called with the four integer registers untouched; float
// arguments ride along in xmm registers, which nothing here touches first.
// Only for functions returning void or an integer.

const NGEN: usize = 4;
static GEN_ORIG: [AtomicUsize; NGEN] = [const { AtomicUsize::new(0) }; NGEN];
static GEN_CFG: [AtomicUsize; NGEN] = [const { AtomicUsize::new(0) }; NGEN]; // reg | mode<<8 | guard<<16
static GEN_CALLS: [AtomicUsize; NGEN] = [const { AtomicUsize::new(0) }; NGEN];
static GEN_WRITES: [AtomicUsize; NGEN] = [const { AtomicUsize::new(0) }; NGEN];
static GEN_SLOT: Mutex<[(usize, usize); NGEN]> = Mutex::new([(0, 0); NGEN]);
static GEN_DUMP: Mutex<[[u8; 64]; NGEN]> = Mutex::new([[0u8; 64]; NGEN]);

// Eight integer-width arguments: the first four in rcx/rdx/r8/r9, the rest
// read from the caller's stack and re-pushed for the original. A wrapper that
// forwarded only four made functions with stack arguments (CalcSceneView-
// InitOptions' StereoViewIndex) read garbage from the wrapper's own frame and
// the game died ~40 s later. Extra trailing values are ignored by the callee.
type GenFn = unsafe extern "system" fn(usize, usize, usize, usize, usize, usize, usize, usize) -> usize;

fn plausible_view(b: &[u8]) -> bool {
    let d = |k: usize| f64::from_le_bytes(b[k * 8..k * 8 + 8].try_into().unwrap());
    let fov = f32::from_le_bytes(b[48..52].try_into().unwrap());
    (0..6).all(|k| d(k).is_finite() && d(k).abs() < 1.0e7) && fov > 1.0 && fov < 179.0
}

fn plausible_vec(b: &[u8]) -> bool {
    (0..3).all(|k| {
        let v = f64::from_le_bytes(b[k * 8..k * 8 + 8].try_into().unwrap());
        v.is_finite() && v.abs() < 1.0e7
    })
}

unsafe fn gen_post(i: usize, _a: usize, b: usize, c: usize, d: usize) {
    GEN_CALLS[i].fetch_add(1, Ordering::Relaxed);
    let cfg = GEN_CFG[i].load(Ordering::Relaxed);
    let (reg, mode, guard) = (cfg & 0xff, (cfg >> 8) & 0xff, (cfg >> 16) & 0xff);
    let out = match reg { 1 => b, 2 => c, 3 => d, _ => 0 };
    if out == 0 {
        return;
    }
    let Some(bytes) = crate::read_bytes(out, 64) else { return };
    if let Ok(mut g) = GEN_DUMP.try_lock() {
        g[i].copy_from_slice(&bytes);
    }
    let pose = match follow_pose() {
        Some(fp) => Some(fp),
        None => match POSE.try_lock() {
            Ok(g) => *g,
            Err(_) => return,
        },
    };
    let Some(p) = pose else { return };
    match mode {
        0 | 2 => {
            if guard != 0 && !plausible_view(&bytes) {
                return;
            }
            let o = out as *mut f64;
            for k in 0..6 {
                *o.add(k) = p[k];
            }
            if mode == 0 {
                *((out + 48) as *mut f32) = p[6] as f32;
            }
        }
        1 => {
            let Some(rb) = crate::read_bytes(c, 24) else { return };
            if guard != 0 && !(plausible_vec(&bytes) && plausible_vec(&rb)) {
                return;
            }
            let lo = b as *mut f64;
            let ro = c as *mut f64;
            for k in 0..3 {
                *lo.add(k) = p[k];
                *ro.add(k) = p[3 + k];
            }
        }
        3 => {
            // FSceneViewProjectionData: FVector ViewOrigin @0, FMatrix
            // ViewRotationMatrix @32 (16-aligned), FMatrix ProjectionMatrix @160.
            let Some(pb) = crate::read_bytes(out, 224) else { return };
            let m00 = f64::from_le_bytes(pb[160..168].try_into().unwrap());
            let m11 = f64::from_le_bytes(pb[200..208].try_into().unwrap());
            if guard != 0 && !(plausible_vec(&bytes) && m00.abs() > 0.05 && m00.abs() < 20.0) {
                return;
            }
            let o = out as *mut f64;
            for k in 0..3 {
                *o.add(k) = p[k];
            }
            let m = view_rotation_matrix(p[3], p[4], p[5]);
            let mo = (out + 32) as *mut f64;
            for k in 0..16 {
                *mo.add(k) = m[k];
            }
            if p[6] > 1.0 && p[6] < 170.0 && m00.abs() > 0.05 {
                let new00 = 1.0 / (p[6].to_radians() / 2.0).tan();
                *((out + 160) as *mut f64) = new00;
                *((out + 200) as *mut f64) = m11 * new00 / m00;
            }
        }
        4 => {
            // raw: write the doubles given by `O raw <offset> v...` at out+offset
            let raw = match RAW.try_lock() {
                Ok(g) => g.clone(),
                Err(_) => return,
            };
            let Some((off, vals)) = raw else { return };
            if guard != 0 && !plausible_vec(&bytes) {
                return;
            }
            let o = (out + off) as *mut f64;
            for (k, v) in vals.iter().enumerate() {
                *o.add(k) = *v;
            }
        }
        _ => return,
    }
    GEN_WRITES[i].fetch_add(1, Ordering::Relaxed);
}

static RAW: Mutex<Option<(usize, Vec<f64>)>> = Mutex::new(None);

/// Orbit a scene component natively, every frame, instead of having Lua push
/// poses 30 times a second (two Lua threads on one state crashed the game).
/// `O follow <root> <loc_off> <rot_off> dist yaw height lookz pitch roll fov offx offy worldyaw`
#[derive(Clone, Copy)]
struct Follow {
    root: usize,
    loc_off: usize,
    rot_off: usize,
    p: [f64; 11], // dist yaw height lookz pitch roll fov offx offy worldyaw(0/1) _
}
static FOLLOW: Mutex<Option<Follow>> = Mutex::new(None);

fn follow_pose() -> Option<[f64; 7]> {
    let f = (*FOLLOW.try_lock().ok()?)?;
    let lb = crate::read_bytes(f.root + f.loc_off, 24)?;
    let rb = crate::read_bytes(f.root + f.rot_off, 24)?;
    let d = |b: &[u8], k: usize| f64::from_le_bytes(b[k * 8..k * 8 + 8].try_into().unwrap());
    let (lx, ly, lz) = (d(&lb, 0), d(&lb, 1), d(&lb, 2));
    let facing = d(&rb, 1);
    if ![lx, ly, lz, facing].iter().all(|v| v.is_finite() && v.abs() < 1.0e7) {
        return None;
    }
    let [dist, yaw, height, lookz, pitch, roll, fov, offx, offy, worldyaw, _] = f.p;
    let base = if worldyaw != 0.0 { 0.0 } else { facing };
    let ang = (base + yaw).to_radians();
    let (cx, cy, cz) = (lx + ang.cos() * dist, ly + ang.sin() * dist, lz + height);
    let (tx, ty, tz) = (lx + offx, ly + offy, lz + lookz);
    let (dx, dy, dz) = (tx - cx, ty - cy, tz - cz);
    let cyaw = dy.atan2(dx).to_degrees();
    let cpitch = dz.atan2((dx * dx + dy * dy).sqrt()).to_degrees() + pitch;
    Some([cx, cy, cz, cpitch, cyaw, roll, fov])
}

/// UE's FInverseRotationMatrix(rot) * swizzle, as ULocalPlayer builds
/// ViewRotationMatrix (row-major, 16 doubles).
fn view_rotation_matrix(pitch: f64, yaw: f64, roll: f64) -> [f64; 16] {
    let (p, y, r) = (pitch.to_radians(), yaw.to_radians(), roll.to_radians());
    let ym = [[y.cos(), -y.sin(), 0.0, 0.0], [y.sin(), y.cos(), 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]];
    let pm = [[p.cos(), 0.0, -p.sin(), 0.0], [0.0, 1.0, 0.0, 0.0], [p.sin(), 0.0, p.cos(), 0.0], [0.0, 0.0, 0.0, 1.0]];
    let rm = [[1.0, 0.0, 0.0, 0.0], [0.0, r.cos(), r.sin(), 0.0], [0.0, -r.sin(), r.cos(), 0.0], [0.0, 0.0, 0.0, 1.0]];
    let sw = [[0.0, 0.0, 1.0, 0.0], [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]];
    let mul = |a: [[f64; 4]; 4], b: [[f64; 4]; 4]| {
        let mut c = [[0.0; 4]; 4];
        for i in 0..4 {
            for j in 0..4 {
                c[i][j] = (0..4).map(|k| a[i][k] * b[k][j]).sum();
            }
        }
        c
    };
    let m = mul(mul(mul(ym, pm), rm), sw);
    let mut out = [0.0; 16];
    for i in 0..4 {
        for j in 0..4 {
            out[i * 4 + j] = m[i][j];
        }
    }
    out
}

macro_rules! gen_fn {
    ($name:ident, $i:expr) => {
        unsafe extern "system" fn $name(a: usize, b: usize, c: usize, d: usize,
                                        e: usize, f5: usize, g: usize, h: usize) -> usize {
            let f: GenFn = std::mem::transmute(GEN_ORIG[$i].load(Ordering::Relaxed));
            let r = f(a, b, c, d, e, f5, g, h);
            gen_post($i, a, b, c, d);
            r
        }
    };
}
gen_fn!(gen0, 0);
gen_fn!(gen1, 1);
gen_fn!(gen2, 2);
gen_fn!(gen3, 3);
const GEN_FNS: [GenFn; NGEN] = [gen0, gen1, gen2, gen3];

pub fn install_gen(i: usize, vtable: usize, slot: usize, reg: usize, mode: usize, guard: usize) -> String {
    if i >= NGEN {
        return "!gen index 0..3".into();
    }
    if !readable(vtable + slot * 8) {
        return "!bad vtable".into();
    }
    let mut slots = match GEN_SLOT.lock() {
        Ok(s) => s,
        Err(_) => return "!gen lock".into(),
    };
    GEN_CFG[i].store(reg | (mode << 8) | (guard << 16), Ordering::Relaxed);
    if slots[i] == (vtable, slot) {
        return format!("gen{i} reconfigured: reg {reg} mode {mode} guard {guard}");
    }
    if slots[i].0 != 0 {
        return format!("!gen{i} busy on slot {}; V ungen {i} first", slots[i].1);
    }
    let cur = unsafe { std::ptr::read_volatile((vtable + slot * 8) as *const usize) };
    GEN_ORIG[i].store(cur, Ordering::Relaxed);
    GEN_CALLS[i].store(0, Ordering::Relaxed);
    GEN_WRITES[i].store(0, Ordering::Relaxed);
    unsafe { write_slot(vtable, slot, GEN_FNS[i] as usize) };
    slots[i] = (vtable, slot);
    format!("gen{i} on slot {slot} (orig {cur:#x}) reg {reg} mode {mode} guard {guard}")
}

pub fn uninstall_gen(i: usize) -> String {
    if i >= NGEN {
        return "!gen index 0..3".into();
    }
    let mut slots = match GEN_SLOT.lock() {
        Ok(s) => s,
        Err(_) => return "!gen lock".into(),
    };
    let (vt, slot) = slots[i];
    if vt == 0 {
        return format!("!gen{i} not installed");
    }
    unsafe { write_slot(vt, slot, GEN_ORIG[i].load(Ordering::Relaxed)) };
    slots[i] = (0, 0);
    format!("gen{i} removed")
}

pub fn gen_dump() -> String {
    let slots = match GEN_SLOT.lock() {
        Ok(s) => *s,
        Err(_) => return "!gen lock".into(),
    };
    let dump = match GEN_DUMP.lock() {
        Ok(d) => *d,
        Err(_) => return "!dump lock".into(),
    };
    let mut out = String::new();
    for i in 0..NGEN {
        if slots[i].0 == 0 {
            continue;
        }
        let b = &dump[i];
        let d: Vec<String> = (0..6)
            .map(|k| format!("{:.2}", f64::from_le_bytes(b[k * 8..k * 8 + 8].try_into().unwrap())))
            .collect();
        let f = f32::from_le_bytes(b[48..52].try_into().unwrap());
        out.push_str(&format!(
            "gen{i} slot {} calls {} writes {} | doubles {} | f32@48 {:.2} | hex {}\n",
            slots[i].1, GEN_CALLS[i].load(Ordering::Relaxed), GEN_WRITES[i].load(Ordering::Relaxed),
            d.join(","), f, b.iter().map(|x| format!("{x:02x}")).collect::<String>()));
    }
    if out.is_empty() { "no gen hooks".into() } else { out }
}

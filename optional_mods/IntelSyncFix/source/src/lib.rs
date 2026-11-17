//! ajfpfix: make an Intel PC compute rcpps/rsqrtps (and ss/VEX forms) exactly like AMD Zen.
//!
//! All's Justice's physics uses the hardware reciprocal / reciprocal-sqrt approximations. Intel and AMD
//! give different low bits for about half of all inputs, so an Intel and an AMD player drift apart after
//! big knockback (seen with Nana's Plus Ultra) and get kicked. AMD vs AMD stays bit-identical.
//! This mod maps a stub blob right after the exe image and rewrites every rcp/rsqrt site into a jmp to a
//! stub that looks the result up in AMD's own tables (dumped from a Zen 5 CPU, exhaustively verified).
//! Install on Intel PCs only; AMD players need nothing.
use std::ffi::c_void;
use std::io::Write;
use windows_sys::Win32::Foundation::{BOOL, HMODULE};
use windows_sys::Win32::System::Diagnostics::Debug::FlushInstructionCache;
use windows_sys::Win32::System::LibraryLoader::{GetModuleFileNameW, GetModuleHandleW};
use windows_sys::Win32::System::Memory::{
    VirtualAlloc, VirtualProtect, MEM_COMMIT, MEM_RESERVE, PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE,
    PAGE_PROTECTION_FLAGS, PAGE_READWRITE,
};
use windows_sys::Win32::System::Threading::GetCurrentProcess;

const BLOB_RVA: usize = 0xb32_0000;
static BLOB: &[u8] = include_bytes!("blob.bin");
static PATCHES: &[u8] = include_bytes!("patches.bin");

fn log(s: &str) {
    unsafe {
        let mut buf = [0u16; 1024];
        let n = GetModuleFileNameW(std::ptr::null_mut(), buf.as_mut_ptr(), buf.len() as u32) as usize;
        let exe = std::path::PathBuf::from(String::from_utf16_lossy(&buf[..n]));
        if let Some(dir) = exe.parent() {
            if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(dir.join("ajfpfix.log")) {
                let _ = writeln!(f, "{s}");
            }
        }
    }
}

fn is_intel() -> bool {
    let r = unsafe { std::arch::x86_64::__cpuid(0) };
    &r.ebx.to_le_bytes() == b"Genu" && &r.edx.to_le_bytes() == b"ineI" && &r.ecx.to_le_bytes() == b"ntel"
}

fn force_file() -> bool {
    unsafe {
        let mut buf = [0u16; 1024];
        let n = GetModuleFileNameW(std::ptr::null_mut(), buf.as_mut_ptr(), buf.len() as u32) as usize;
        let exe = std::path::PathBuf::from(String::from_utf16_lossy(&buf[..n]));
        exe.parent().map_or(false, |d| d.join("ajfpfix.force").exists())
    }
}

struct Rec { rva: usize, orig: &'static [u8], patch: &'static [u8] }

fn records() -> Vec<Rec> {
    let mut v = Vec::new();
    let mut i = 0;
    while i + 5 <= PATCHES.len() {
        let rva = u32::from_le_bytes(PATCHES[i..i + 4].try_into().unwrap()) as usize;
        let n = PATCHES[i + 4] as usize;
        let orig = &PATCHES[i + 5..i + 5 + n];
        let patch = &PATCHES[i + 5 + n..i + 5 + 2 * n];
        v.push(Rec { rva, orig, patch });
        i += 5 + 2 * n;
    }
    v
}

unsafe fn apply() -> Result<String, String> {
    if !is_intel() && std::env::var_os("AJFPFIX_FORCE").is_none() && !force_file() {
        return Ok("not an Intel CPU: nothing to do".into());
    }
    let base = GetModuleHandleW(std::ptr::null()) as usize;
    if base == 0 { return Err("no exe module".into()); }
    let recs = records();
    let at = |r: &Rec| std::slice::from_raw_parts((base + r.rva) as *const u8, r.orig.len());
    let done = recs.iter().filter(|r| at(r) == r.patch).count();
    if done == recs.len() { return Ok("already patched".into()); }
    let bad: Vec<_> = recs.iter().filter(|r| at(r) != r.orig).map(|r| r.rva).collect();
    if !bad.is_empty() {
        return Err(format!("{} sites differ (different game build?), first rva {:#x}", bad.len(), bad[0]));
    }
    let want = base + BLOB_RVA;
    let p = VirtualAlloc(want as *const c_void, BLOB.len(), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE) as usize;
    if p != want {
        return Err(format!("could not map stubs at {want:#x} (got {p:#x})"));
    }
    std::ptr::copy_nonoverlapping(BLOB.as_ptr(), p as *mut u8, BLOB.len());
    let mut old: PAGE_PROTECTION_FLAGS = 0;
    VirtualProtect(p as *const c_void, BLOB.len(), PAGE_EXECUTE_READ, &mut old);
    FlushInstructionCache(GetCurrentProcess(), p as *const c_void, BLOB.len());
    for r in &recs {
        let site = base + r.rva;
        if VirtualProtect(site as *const c_void, r.patch.len(), PAGE_EXECUTE_READWRITE, &mut old) == 0 {
            return Err(format!("VirtualProtect failed at rva {:#x}", r.rva));
        }
        std::ptr::copy_nonoverlapping(r.patch.as_ptr(), site as *mut u8, r.patch.len());
        VirtualProtect(site as *const c_void, r.patch.len(), old, &mut old);
        FlushInstructionCache(GetCurrentProcess(), site as *const c_void, r.patch.len());
    }
    Ok(format!("patched {} rcp/rsqrt sites to AMD tables, stubs at {want:#x}", recs.len()))
}

#[no_mangle]
pub extern "system" fn DllMain(_h: HMODULE, reason: u32, _r: *mut c_void) -> BOOL {
    if reason == 1 {
        let r = unsafe { apply() };
        log(&match r { Ok(m) => format!("ajfpfix: {m}"), Err(e) => format!("ajfpfix: NOT applied: {e}") });
    }
    1
}

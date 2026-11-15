//! Minimal Lua 5.2 C API bindings, resolved at runtime from the game's Lua524.dll.
use std::ffi::c_void;
use std::os::raw::{c_char, c_int};

pub type State = *mut c_void;

pub const OK: c_int = 0;
pub const MULTRET: c_int = -1;
pub const GLOBALSINDEX: c_int = -1001000; // 5.2 uses registry + _ENV; see get_global

pub type LoadBufferX = unsafe extern "C" fn(State, *const c_char, usize, *const c_char, *const c_char) -> c_int;
pub type PcallK = unsafe extern "C" fn(State, c_int, c_int, c_int, c_int, *const c_void) -> c_int;
/// 5.2: void lua_callk(L, nargs, nresults, ctx, k) - note: no errfunc, returns void.
pub type CallK = unsafe extern "C" fn(State, c_int, c_int, c_int, *const c_void);
/// The classic, non-continuation forms lua_pcallk/lua_callk exist alongside -
/// int lua_pcall(L, nargs, nresults, msgh); void lua_call(L, nargs, nresults).
/// Hooked too: a caller that never needs to yield across the call (no
/// mid-call suspend/resume) has no reason to use the K variants, and
/// whatever drives CPU-controlled fighters may be exactly that - simpler,
/// synchronous update logic with no continuation to save.
pub type Pcall = unsafe extern "C" fn(State, c_int, c_int, c_int) -> c_int;
pub type Call = unsafe extern "C" fn(State, c_int, c_int);
pub type ToLString = unsafe extern "C" fn(State, c_int, *mut usize) -> *const c_char;
pub type SetTop = unsafe extern "C" fn(State, c_int);
pub type GetTop = unsafe extern "C" fn(State) -> c_int;

pub struct Api {
    pub load_bufferx: LoadBufferX,
    pub pcallk: PcallK,
    pub tolstring: ToLString,
    pub settop: SetTop,
    pub gettop: GetTop,
}

unsafe impl Send for Api {}
unsafe impl Sync for Api {}

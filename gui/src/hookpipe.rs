//! Minimal named-pipe client for talking directly to ajhook, mirroring the
//! wire protocol `ajbot/hook.py` implements in Python (see its module
//! docstring): `\\.\pipe\ajhook`, messages are `<opcode><payload>`, both
//! directions length-prefixed as ASCII decimal + '\n' + that many raw bytes.
//!
//! This exists so the GUI can drive the native recorder's start/stop/status
//! opcodes (W/X/T) directly, without spawning a long-running Python process
//! per tick - see `record_control.rs`. It intentionally does NOT know the
//! resident sampler's Lua source (the `R`/`I` setup opcodes): that text
//! lives in `ajbot/sampler.py`'s `RESIDENT` string, hundreds of carefully
//! live-verified lines, and duplicating it here would be a real
//! maintenance/sync risk for no benefit - a tiny one-shot Python helper
//! (`native_record_setup.py`) handles that one-time setup call instead.
//!
//! On Linux the game runs under Proton and its named pipe lives inside Wine,
//! out of reach of a native process, so there the same messages go over
//! ajhook's loopback TCP listener instead (127.0.0.1, port AJHOOK_PORT or
//! 47380). Everything above the transport is shared.

use std::io;

#[cfg(windows)]
pub use pipe::HookPipe;
#[cfg(not(windows))]
pub use tcp::HookPipe;

/// Frame and send one `<opcode><payload>` message, then read the framed reply.
fn exchange(
    mut write: impl FnMut(&[u8]) -> io::Result<()>,
    mut read: impl FnMut(&mut [u8]) -> io::Result<usize>,
    opcode: u8,
    payload: &str,
) -> io::Result<String> {
    let mut body = vec![opcode];
    body.extend_from_slice(payload.as_bytes());
    let mut out = format!("{}\n", body.len()).into_bytes();
    out.extend_from_slice(&body);
    write(&out)?;

    let mut buf: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 4096];
    let reply = loop {
        if let Some(nl) = buf.iter().position(|&b| b == b'\n') {
            let len: usize = std::str::from_utf8(&buf[..nl])
                .ok()
                .and_then(|s| s.trim().parse().ok())
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "bad frame length"))?;
            if buf.len() >= nl + 1 + len {
                break String::from_utf8_lossy(&buf[nl + 1..nl + 1 + len]).into_owned();
            }
        }
        let got = read(&mut chunk)?;
        if got == 0 {
            return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "pipe closed"));
        }
        buf.extend_from_slice(&chunk[..got]);
    };
    if let Some(err) = reply.strip_prefix('!') {
        return Err(io::Error::new(io::ErrorKind::Other, err.to_string()));
    }
    Ok(reply)
}

impl HookPipe {
    /// Start native recording to `path` (see ajhook's 'W' opcode - a
    /// resident sampler must already be installed, which is what
    /// `native_record_setup.py` does before this is ever called).
    pub fn start_recording(&self, path: &str) -> io::Result<String> {
        self.send(b'W', &game_path(path))
    }

    pub fn stop_recording(&self) -> io::Result<String> {
        self.send(b'X', "")
    }

    /// Raw "recording=<0|1> ticks=<n> path=<path>" status line.
    pub fn recording_status(&self) -> io::Result<String> {
        self.send(b'T', "")
    }
}

/// A path the DLL can open. Under Proton the DLL is a Wine process, where the
/// Linux root is drive Z:. Same mapping as `ajbot/hook.py`'s wine_path().
fn game_path(path: &str) -> String {
    if cfg!(not(windows)) && path.starts_with('/') {
        format!("Z:{}", path.replace('/', "\\"))
    } else {
        path.to_string()
    }
}

#[cfg(not(windows))]
mod tcp {
    use std::io::{self, Read, Write};
    use std::net::TcpStream;
    use std::time::Duration;

    pub struct HookPipe {
        stream: TcpStream,
    }

    impl HookPipe {
        /// One attempt, like the pipe version: the GUI polls on its own timer.
        pub fn connect() -> io::Result<Self> {
            let port = std::env::var("AJHOOK_PORT")
                .ok()
                .and_then(|v| v.trim().parse::<u16>().ok())
                .unwrap_or(47380);
            let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
            let stream = TcpStream::connect_timeout(&addr, Duration::from_millis(500))?;
            stream.set_nodelay(true)?;
            stream.set_read_timeout(Some(Duration::from_secs(5)))?;
            Ok(HookPipe { stream })
        }

        pub(super) fn send(&self, opcode: u8, payload: &str) -> io::Result<String> {
            super::exchange(
                |b| (&self.stream).write_all(b),
                |b| (&self.stream).read(b),
                opcode,
                payload,
            )
        }
    }
}

#[cfg(windows)]
mod pipe {
use std::ffi::c_void;
use std::io;
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};

const PIPE_NAME: &str = r"\\.\pipe\ajhook";

// windows-sys is already a dependency of ajhook but NOT of this gui crate
// (see gui/Cargo.toml) - rather than pull in a whole new crate dependency
// for four Win32 calls, this binds them directly via `extern "system"`,
// matching the exact signatures windows-sys itself would generate.
mod win32 {
    use std::ffi::c_void;

    pub type Handle = *mut c_void;
    pub const INVALID_HANDLE_VALUE: Handle = -1isize as Handle;
    pub const GENERIC_READ: u32 = 0x8000_0000;
    pub const GENERIC_WRITE: u32 = 0x4000_0000;
    pub const OPEN_EXISTING: u32 = 3;

    #[link(name = "kernel32")]
    extern "system" {
        pub fn CreateFileW(
            lpFileName: *const u16,
            dwDesiredAccess: u32,
            dwShareMode: u32,
            lpSecurityAttributes: *const c_void,
            dwCreationDisposition: u32,
            dwFlagsAndAttributes: u32,
            hTemplateFile: Handle,
        ) -> Handle;
        pub fn ReadFile(
            hFile: Handle,
            lpBuffer: *mut u8,
            nNumberOfBytesToRead: u32,
            lpNumberOfBytesRead: *mut u32,
            lpOverlapped: *mut c_void,
        ) -> i32;
        pub fn WriteFile(
            hFile: Handle,
            lpBuffer: *const u8,
            nNumberOfBytesToWrite: u32,
            lpNumberOfBytesWritten: *mut u32,
            lpOverlapped: *mut c_void,
        ) -> i32;
    }
}

pub struct HookPipe {
    handle: OwnedHandle,
}

fn wide(s: &str) -> Vec<u16> {
    std::ffi::OsStr::new(s)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

impl HookPipe {
    /// Connects to the running ajhook DLL's named pipe. Fails immediately
    /// (no retry loop) if the game isn't running with ajhook.asi loaded -
    /// same "not there yet" failure `ajbot/hook.py`'s Hook.open() retries
    /// on for a few seconds, but the GUI already polls on its own timer so
    /// a single attempt per call is the right shape here.
    pub fn connect() -> io::Result<Self> {
        let name = wide(PIPE_NAME);
        let h = unsafe {
            win32::CreateFileW(
                name.as_ptr(),
                win32::GENERIC_READ | win32::GENERIC_WRITE,
                0,
                std::ptr::null(),
                win32::OPEN_EXISTING,
                0,
                std::ptr::null_mut(),
            )
        };
        if h == win32::INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        // Safety: h is a valid, freshly-opened HANDLE we own exclusively.
        let handle = unsafe { OwnedHandle::from_raw_handle(h as *mut c_void as _) };
        Ok(HookPipe { handle })
    }

    fn raw(&self) -> win32::Handle {
        self.handle.as_raw_handle() as win32::Handle
    }

    pub(super) fn send(&self, opcode: u8, payload: &str) -> io::Result<String> {
        super::exchange(
            |out| {
                let mut written: u32 = 0;
                let ok = unsafe {
                    win32::WriteFile(
                        self.raw(),
                        out.as_ptr(),
                        out.len() as u32,
                        &mut written,
                        std::ptr::null_mut(),
                    )
                };
                if ok == 0 {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            },
            |chunk| {
                let mut got: u32 = 0;
                let ok = unsafe {
                    win32::ReadFile(
                        self.raw(),
                        chunk.as_mut_ptr(),
                        chunk.len() as u32,
                        &mut got,
                        std::ptr::null_mut(),
                    )
                };
                if ok == 0 {
                    return Ok(0);
                }
                Ok(got as usize)
            },
            opcode,
            payload,
        )
    }
}
}

/// Parsed form of the 'T' opcode's reply.
#[derive(Debug, Clone, Default)]
pub struct RecordingStatus {
    pub recording: bool,
    pub ticks: u64,
    pub path: String,
}

pub fn parse_status(s: &str) -> RecordingStatus {
    let mut out = RecordingStatus::default();
    for part in s.split_whitespace() {
        if let Some(v) = part.strip_prefix("recording=") {
            out.recording = v == "1";
        } else if let Some(v) = part.strip_prefix("ticks=") {
            out.ticks = v.parse().unwrap_or(0);
        } else if let Some(v) = part.strip_prefix("path=") {
            out.path = v.to_string();
        }
    }
    out
}

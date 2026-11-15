//! Controls a native (in-DLL) recording session for Mode::Record, replacing
//! the old "spawn record.py and parse its stdout for the whole session"
//! flow. The only process spawned here is `native_record_setup.py`, which
//! runs for well under a second (install the resident sampler, send the
//! 'W' start opcode, exit) - after that, the DLL keeps recording on its own
//! and this struct just polls status over the pipe (see `hookpipe.rs`).

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

use crate::hookpipe::{parse_status, HookPipe, RecordingStatus};

const POLL_PERIOD: Duration = Duration::from_millis(500);

pub enum RecorderState {
    Idle,
    /// Waiting for native_record_setup.py to finish (should be near-instant).
    Starting,
    Recording(RecordingStatus),
    /// Something went wrong (setup script failed, or the pipe stopped
    /// answering) - held until the user acknowledges it by trying again.
    Error(String),
}

pub struct NativeRecorder {
    pub state: RecorderState,
    last_poll: Instant,
    path: PathBuf,
}

impl NativeRecorder {
    pub fn new() -> Self {
        NativeRecorder { state: RecorderState::Idle, last_poll: Instant::now(), path: PathBuf::new() }
    }

    /// Kicks off `native_record_setup.py <path> --every N` and waits for it
    /// to exit (it's a one-shot call, not a long-running process - see the
    /// module doc). `python` is whatever interpreter Settings already uses
    /// for play.py/play2.py; `dir` is the ajbot project directory.
    pub fn start(&mut self, python: &str, dir: &Path, out_path: &Path, every_pumps: u32) {
        self.path = out_path.to_path_buf();
        self.state = RecorderState::Starting;

        let mut cmd = Command::new(python);
        cmd.arg("-u")
            .arg("bot/native_record_setup.py")
            .arg(out_path)
            .arg("--every")
            .arg(every_pumps.to_string())
            .current_dir(dir);
        #[cfg(windows)]
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW

        match cmd.output() {
            Ok(out) => {
                let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
                if out.status.success() && !text.starts_with('!') {
                    self.state = RecorderState::Recording(RecordingStatus {
                        recording: true,
                        ticks: 0,
                        path: out_path.display().to_string(),
                    });
                } else {
                    let err = String::from_utf8_lossy(&out.stderr);
                    let msg = if !text.is_empty() { text } else { err.trim().to_string() };
                    self.state = RecorderState::Error(if msg.is_empty() {
                        "native_record_setup.py failed with no output".into()
                    } else {
                        msg
                    });
                }
            }
            Err(e) => {
                self.state = RecorderState::Error(format!("could not run native_record_setup.py: {e}"));
            }
        }
        self.last_poll = Instant::now();
    }

    /// Sends the 'X' stop opcode directly over the pipe - no process to
    /// spawn or kill, the DLL just stops writing and closes the file.
    pub fn stop(&mut self) {
        match HookPipe::connect().and_then(|p| p.stop_recording()) {
            Ok(_) => self.state = RecorderState::Idle,
            Err(e) => self.state = RecorderState::Error(format!("could not stop recording: {e}")),
        }
    }

    /// Call once per frame; polls the 'T' status opcode on `POLL_PERIOD`,
    /// updating `self.state` in place. Cheap to call every frame - it only
    /// actually touches the pipe every half second.
    pub fn tick(&mut self) {
        if !matches!(self.state, RecorderState::Recording(_)) {
            return;
        }
        if self.last_poll.elapsed() < POLL_PERIOD {
            return;
        }
        self.last_poll = Instant::now();
        match HookPipe::connect().and_then(|p| p.recording_status()) {
            Ok(raw) => {
                let status = parse_status(&raw);
                if status.recording {
                    self.state = RecorderState::Recording(status);
                } else {
                    // The DLL itself stopped (e.g. hook reset) without us
                    // asking - surface that rather than silently going quiet.
                    self.state = RecorderState::Error(
                        "recording stopped unexpectedly (hook no longer reports recording=1)".into(),
                    );
                }
            }
            Err(e) => {
                self.state = RecorderState::Error(format!("lost the hook pipe: {e}"));
            }
        }
    }

    pub fn is_recording(&self) -> bool {
        matches!(self.state, RecorderState::Recording(_))
    }
}

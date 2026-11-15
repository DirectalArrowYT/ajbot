//! Launching and stopping the Python side (bot/play.py, bot/play2.py, ...).
//!
//! The scripts are long-running and print a live log. They are started with
//! `python -u` so output arrives line by line, and stopped through a stop
//! file: the scripts check for it every tick and shut down cleanly, which
//! releases the virtual pads and writes the last match's statistics. Killing
//! the process outright would skip both, so that is only the fallback when a
//! script ignores the stop file for too long.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

const MAX_LINES: usize = 4000;
const KILL_AFTER: Duration = Duration::from_secs(12);

pub type Log = Arc<Mutex<VecDeque<String>>>;

/// The newest "[rec] ..." heartbeat from the script, and when its tick count
/// last went up - the GUI's proof that data is actually being written.
#[derive(Clone)]
pub struct RecStatus {
    pub line: String,
    pub at: Instant,
    pub ticks: Option<u64>,
    pub last_growth: Instant,
}

pub type Rec = Arc<Mutex<Option<RecStatus>>>;

/// How a script is asked to stop: a stop file it watches, here or on the
/// machine it runs on over ssh.
enum Stop {
    File(PathBuf),
    Remote { host: String, file: String },
    /// One-shot tools (setup fixes, sync, pull): stopping just ends them.
    Kill,
}

pub struct Runner {
    child: Child,
    pub log: Log,
    pub rec: Rec,
    pub label: String,
    pub started: Instant,
    stop: Stop,
    stop_requested: Option<Instant>,
}

/// Quote one word for a POSIX shell.
fn sh_quote(s: &str) -> String {
    if !s.is_empty() && s.chars().all(|c| c.is_ascii_alphanumeric() || "-_./=:,@+".contains(c)) {
        return s.to_string();
    }
    format!("'{}'", s.replace('\'', "'\\''"))
}

fn base64(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for c in data.chunks(3) {
        let n = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
        for i in 0..4 {
            if i <= c.len() {
                out.push(T[(n >> (18 - 6 * i) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

impl Runner {
    pub fn start(python: &str, dir: &Path, script: &str, args: &[String], label: &str)
        -> Result<Runner, String>
    {
        let stop_file = dir.join("stats").join(".gui_stop");
        let _ = std::fs::create_dir_all(dir.join("stats"));
        let _ = std::fs::remove_file(&stop_file);

        let mut cmd = Command::new(python);
        cmd.arg("-u")
            .arg(script)
            .args(args)
            .arg("--stop-file")
            .arg(&stop_file)
            .current_dir(dir)
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW

        let shown = format!("> {python} -u {script} {}", args.join(" "));
        Self::spawn(cmd, python, shown, label, Stop::File(stop_file))
    }

    /// Run any program to completion (setup fixes, pip, sync/pull scripts),
    /// with no stop file: Stop kills it.
    pub fn start_plain(program: &str, dir: &Path, args: &[String], label: &str)
        -> Result<Runner, String>
    {
        let mut cmd = Command::new(program);
        cmd.args(args)
            .current_dir(dir)
            .env("PYTHONIOENCODING", "utf-8")
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
        let shown = format!("> {program} {}", args.join(" "));
        Self::spawn(cmd, program, shown, label, Stop::Kill)
    }

    /// Run a script on another machine over ssh (the box's headless game
    /// copies), with `env` set for it. The command travels base64-encoded so
    /// neither the remote login shell (fish on the box) nor quoting in the
    /// arguments gets in the way; stopping touches a stop file over there.
    pub fn start_remote(host: &str, dir: &str, python: &str, script: &str, args: &[String],
                        env: &[(&str, &str)], label: &str) -> Result<Runner, String>
    {
        let tag = script.rsplit('/').next().unwrap_or(script);   // bot/practice.py -> practice.py
        let stop = format!("{}/stats/.gui_stop_{tag}", dir.trim_end_matches('/'));
        let mut line = format!("cd {} && mkdir -p stats && rm -f {} && exec env PYTHONIOENCODING=utf-8",
                               sh_quote(dir), sh_quote(&stop));
        for (k, v) in env {
            line += &format!(" {k}={}", sh_quote(v));
        }
        line += &format!(" {} -u {}", sh_quote(python), sh_quote(script));
        for a in args {
            line += " ";
            line += &sh_quote(a);
        }
        line += &format!(" --stop-file {}", sh_quote(&stop));
        let mut cmd = Command::new("ssh");
        cmd.args(["-o", "BatchMode=yes", host])
            .arg(format!("echo {} | base64 -d | bash", base64(line.as_bytes())))
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        cmd.creation_flags(0x0800_0000);
        let shown = format!("> [{host}] {python} -u {script} {}", args.join(" "));
        Self::spawn(cmd, "ssh", shown, label, Stop::Remote { host: host.into(), file: stop })
    }

    fn spawn(mut cmd: Command, program: &str, shown: String, label: &str, stop: Stop)
        -> Result<Runner, String>
    {
        let mut child = cmd
            .spawn()
            .map_err(|e| format!("could not start `{program}`: {e}"))?;

        let log: Log = Arc::new(Mutex::new(VecDeque::new()));
        {
            let mut l = log.lock().unwrap();
            l.push_back(shown);
        }
        let rec: Rec = Arc::new(Mutex::new(None));
        if let Some(out) = child.stdout.take() {
            pump(out, log.clone(), rec.clone());
        }
        if let Some(err) = child.stderr.take() {
            pump(err, log.clone(), rec.clone());
        }
        Ok(Runner {
            child,
            log,
            rec,
            label: label.to_string(),
            started: Instant::now(),
            stop,
            stop_requested: None,
        })
    }

    /// Ask the script to stop cleanly (it finishes the current tick, releases
    /// the pads and writes its last stats line).
    pub fn request_stop(&mut self) {
        if self.stop_requested.is_none() {
            match &self.stop {
                Stop::File(f) => {
                    let _ = std::fs::write(f, b"stop");
                }
                Stop::Remote { host, file } => {
                    let _ = Command::new("ssh")
                        .args(["-o", "BatchMode=yes", host.as_str(), &format!("touch {}", sh_quote(file))])
                        .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
                        .spawn();
                }
                Stop::Kill => {
                    let _ = self.child.kill();
                }
            }
            self.stop_requested = Some(Instant::now());
            self.push("[gui] stop requested - waiting for the script to finish".into());
        }
    }

    pub fn stopping(&self) -> bool {
        self.stop_requested.is_some()
    }

    /// Poll: Some(exit description) once the process has ended.
    pub fn poll(&mut self) -> Option<String> {
        if let Some(t) = self.stop_requested {
            if t.elapsed() > KILL_AFTER {
                let _ = self.child.kill();
                self.push("[gui] script did not stop in time - killed".into());
            }
        }
        match self.child.try_wait() {
            Ok(Some(status)) => {
                if let Stop::File(f) = &self.stop {
                    let _ = std::fs::remove_file(f);
                }
                Some(format!("exited ({status})"))
            }
            Ok(None) => None,
            Err(e) => Some(format!("lost track of the process: {e}")),
        }
    }

    pub fn kill(&mut self) {
        let _ = self.child.kill();
    }

    pub fn push(&self, line: String) {
        let mut l = self.log.lock().unwrap();
        l.push_back(line);
        while l.len() > MAX_LINES {
            l.pop_front();
        }
    }
}

fn parse_ticks(line: &str) -> Option<u64> {
    // "... | 1234 ticks (+38 in 2s) | ..."
    let i = line.find(" ticks")?;
    line[..i].rsplit(|c: char| !c.is_ascii_digit()).next()?.parse().ok()
}

fn pump<R: std::io::Read + Send + 'static>(stream: R, log: Log, rec: Rec) {
    thread::spawn(move || {
        let reader = BufReader::new(stream);
        for line in reader.split(b'\n') {
            let Ok(bytes) = line else { break };
            let text = String::from_utf8_lossy(&bytes);
            // Windows line endings are \r\n, so strip the trailing \r first.
            // Any \r left is a countdown redrawn in place; keep its last frame.
            let text = text.trim_end_matches('\r');
            let text = text.rsplit('\r').next().unwrap_or("").to_string();
            if let Some(body) = text.strip_prefix("[rec]") {
                // Heartbeats drive the recording indicator instead of flooding
                // the console every two seconds.
                let now = Instant::now();
                let ticks = parse_ticks(body);
                let mut r = rec.lock().unwrap();
                let grew = match (&*r, ticks) {
                    (Some(prev), Some(t)) => prev.ticks.map(|p| t > p).unwrap_or(true),
                    (None, Some(_)) => true,
                    _ => false,
                };
                let last_growth = match &*r {
                    Some(prev) if !grew => prev.last_growth,
                    _ => now,
                };
                *r = Some(RecStatus { line: body.trim().to_string(), at: now, ticks, last_growth });
                continue;
            }
            let mut l = log.lock().unwrap();
            l.push_back(text);
            while l.len() > MAX_LINES {
                l.pop_front();
            }
        }
    });
}

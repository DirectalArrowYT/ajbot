//! The Setup page: an install checklist with a Fix button per problem.
//!
//! The checks and fixes live in tools/doctor.py (`--json` reports, `--fix ID`
//! repairs); this page only shows them. Python itself is checked here, since
//! without it doctor.py cannot run at all.

use crate::theme::{self, BAD, GOOD, MUTED, ORANGE, WARN};
use crate::{App, Job};
use eframe::egui::{self, RichText};
use serde::Deserialize;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::Instant;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[derive(Deserialize, Clone)]
pub struct Check {
    pub id: String,
    pub group: String,
    pub label: String,
    pub ok: Option<bool>,
    #[serde(default)]
    pub detail: String,
    pub fix: Option<String>,
    pub fix_label: Option<String>,
}

#[derive(Deserialize)]
struct Report {
    checks: Vec<Check>,
}

#[derive(Default)]
pub struct SetupState {
    pub busy: bool,
    pub checked: Option<Instant>,
    /// None = not looked yet; Some(Err) = python could not be run.
    pub python: Option<Result<String, String>>,
    pub checks: Vec<Check>,
    pub error: Option<String>,
}

impl SetupState {
    /// Anything that needs the user's attention (drives the sidebar dot).
    pub fn problems(&self) -> usize {
        let py = matches!(self.python, Some(Err(_))) as usize;
        py + self.checks.iter().filter(|c| c.ok == Some(false)).count()
    }
}

pub type Shared = Arc<Mutex<SetupState>>;

/// No console window for a background command (it would pop up over the GUI).
pub fn quiet(cmd: &mut Command) -> &mut Command {
    #[cfg(windows)]
    cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    cmd
}

/// Re-run every check in the background.
pub fn refresh(state: &Shared, python: String, dir: PathBuf, ctx: egui::Context) {
    {
        let mut s = state.lock().unwrap();
        if s.busy {
            return;
        }
        s.busy = true;
    }
    let state = state.clone();
    std::thread::spawn(move || {
        let py = quiet(Command::new(&python).arg("--version")).output();
        let py = match py {
            Ok(o) if o.status.success() => {
                let v = String::from_utf8_lossy(&o.stdout).to_string() + &String::from_utf8_lossy(&o.stderr);
                Ok(v.trim().to_string())
            }
            Ok(o) => Err(String::from_utf8_lossy(&o.stderr).trim().chars().take(200).collect()),
            Err(e) => Err(e.to_string()),
        };
        let mut checks = Vec::new();
        let mut error = None;
        if py.is_ok() {
            let out = quiet(Command::new(&python)
                .args(["tools/doctor.py", "--json"])
                .current_dir(&dir)
                .env("PYTHONIOENCODING", "utf-8"))
                .output();
            match out {
                Ok(o) => {
                    let text = String::from_utf8_lossy(&o.stdout);
                    match text.lines().rev().find(|l| l.starts_with('{')).map(serde_json::from_str::<Report>) {
                        Some(Ok(r)) => checks = r.checks,
                        _ => {
                            let err = String::from_utf8_lossy(&o.stderr);
                            error = Some(format!("doctor.py failed: {}",
                                err.lines().last().unwrap_or("no output")));
                        }
                    }
                }
                Err(e) => error = Some(format!("could not run doctor.py: {e}")),
            }
        }
        let mut s = state.lock().unwrap();
        s.python = Some(py);
        s.checks = checks;
        s.error = error;
        s.busy = false;
        s.checked = Some(Instant::now());
        ctx.request_repaint();
    });
}

/// Python installs this machine might have that are not on PATH yet (a fresh
/// winget install only reaches PATH for programs started after it).
pub fn find_python(dir: &Path) -> Option<String> {
    let mut c: Vec<PathBuf> = vec![dir.join("python").join("python.exe")];
    if let Ok(la) = std::env::var("LOCALAPPDATA") {
        let base = Path::new(&la).join("Programs").join("Python");
        if let Ok(rd) = std::fs::read_dir(&base) {
            let mut v: Vec<PathBuf> = rd.flatten().map(|e| e.path().join("python.exe")).collect();
            v.sort();
            v.reverse(); // newest version first
            c.extend(v);
        }
    }
    for pf in ["ProgramFiles", "ProgramW6432"] {
        if let Ok(p) = std::env::var(pf) {
            for v in ["Python314", "Python313", "Python312"] {
                c.push(Path::new(&p).join(v).join("python.exe"));
            }
        }
    }
    c.into_iter().find(|p| p.is_file()).map(|p| p.display().to_string())
}

/// Open a visible console running `line` (installers that show progress or ask).
fn console(line: &str) {
    #[cfg(windows)]
    {
        let _ = Command::new("cmd").args(["/k", line]).creation_flags(0x0000_0010).spawn(); // CREATE_NEW_CONSOLE
    }
    #[cfg(not(windows))]
    {
        let _ = line;
    }
}

impl App {
    pub(crate) fn setup_refresh(&mut self, ctx: &egui::Context) {
        refresh(&self.setup, self.settings.python.clone(), self.dir(), ctx.clone());
    }

    fn fix_job(&self, id: &str, extra: &[String]) -> Job {
        let mut args = vec!["-u".to_string(), "tools/doctor.py".into(), "--fix".into(), id.into()];
        args.extend_from_slice(extra);
        Job { program: self.settings.python.clone(), args, label: format!("fix {id}"), recheck: true, env: vec![] }
    }

    pub(crate) fn ui_setup(&mut self, ui: &mut egui::Ui) {
        theme::page_title(ui, "Setup",
            "Everything the bot needs on this PC. Fix buttons do the work; Check again re-tests.");
        let busy_job = self.runner.is_some();
        let (python, checks, error, busy, checked) = {
            let s = self.setup.lock().unwrap();
            (s.python.clone(), s.checks.clone(), s.error.clone(), s.busy, s.checked)
        };
        ui.horizontal(|ui| {
            if ui.add_enabled(!busy, egui::Button::new("⟳  Check again")).clicked() {
                self.setup_refresh(ui.ctx());
            }
            if busy {
                ui.spinner();
                ui.label(RichText::new("checking…").color(MUTED));
            } else if let Some(t) = checked {
                ui.label(RichText::new(format!("checked {}s ago", t.elapsed().as_secs())).color(MUTED));
            }
            let bad = checks.iter().filter(|c| c.ok == Some(false)).count()
                + matches!(python, Some(Err(_))) as usize;
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if python.is_none() {
                    // still looking
                } else if bad == 0 && error.is_none() {
                    theme::pill(ui, "ready to play", GOOD);
                } else {
                    theme::pill(ui, &format!("{bad} to fix"), WARN);
                }
            });
        });
        ui.add_space(8.0);

        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            // ---- Python
            theme::titled_card(ui, "Python", |ui| {
                match &python {
                    None => {
                        ui.horizontal(|ui| { ui.spinner(); ui.label("looking for Python…"); });
                    }
                    Some(Ok(v)) => {
                        ui.horizontal(|ui| row(ui, Some(true), v, &self.settings.python));
                    }
                    Some(Err(e)) => {
                        ui.horizontal(|ui| row(ui, Some(false), "Python not found", e));
                        ui.label(RichText::new(
                            "The bot's brain runs on Python 3.12 or newer. Install it, then press Look again.")
                            .color(MUTED));
                        ui.horizontal(|ui| {
                            if theme::primary_button(ui, "Install Python 3.12").clicked() {
                                console("winget install -e --id Python.Python.3.12 --scope user");
                            }
                            if ui.button("Download page").clicked() {
                                open_url("https://www.python.org/downloads/windows/");
                            }
                            if ui.button("Look again").clicked() {
                                if let Some(p) = find_python(&self.dir()) {
                                    self.settings.python = p;
                                    self.save_settings();
                                }
                                self.setup_refresh(ui.ctx());
                            }
                        });
                    }
                }
                for c in checks.iter().filter(|c| c.group == "Python" && c.id != "python") {
                    self.check_row(ui, c, busy_job);
                }
            });
            if let Some(e) = &error {
                ui.add_space(8.0);
                theme::card(ui, |ui| { ui.colored_label(BAD, e); });
            }

            let mut groups: Vec<&str> = Vec::new();
            for c in &checks {
                if c.group != "Python" && !groups.contains(&c.group.as_str()) {
                    groups.push(&c.group);
                }
            }
            for g in groups {
                ui.add_space(8.0);
                let title = match g {
                    "Controller" => "Virtual controller",
                    "Game" => "Game files",
                    "Bot" => "Trained bot",
                    o => o,
                };
                theme::titled_card(ui, title, |ui| {
                    for c in checks.iter().filter(|c| c.group == g) {
                        self.check_row(ui, c, busy_job);
                        if c.id == "game" && c.ok == Some(false) {
                            self.game_folder_picker(ui, busy_job);
                        }
                    }
                    if g == "Game" {
                        ui.add_space(2.0);
                        ui.label(RichText::new(
                            "Game files go in HeroGame/Binaries/Win64. Close the game before installing.")
                            .color(MUTED).small());
                    }
                });
            }

            ui.add_space(8.0);
            theme::titled_card(ui, "Updates", |ui| {
                ui.label(RichText::new(
                    "Pulls the newest code (git), GUI and game files from GitHub releases.")
                    .color(MUTED));
                ui.horizontal(|ui| {
                    let py = self.settings.python.clone();
                    if ui.add_enabled(!busy_job, egui::Button::new("Check for updates")).clicked() {
                        self.run_jobs(vec![Job { program: py.clone(),
                            args: vec!["-u".into(), "tools/update.py".into(), "--check".into()],
                            label: "update check".into(), recheck: false, env: vec![] }]);
                    }
                    if ui.add_enabled(!busy_job, egui::Button::new("Update now")).clicked() {
                        self.run_jobs(vec![Job { program: py,
                            args: vec!["-u".into(), "tools/update.py".into()],
                            label: "update".into(), recheck: true, env: vec![] }]);
                    }
                    if ui.button("Releases page").clicked() {
                        open_url("https://github.com/DirectalArrowYT/ajbot/releases");
                    }
                });
            });
        });
    }

    fn check_row(&mut self, ui: &mut egui::Ui, c: &Check, busy_job: bool) {
        ui.horizontal(|ui| {
            row(ui, c.ok, &c.label, &c.detail);
            if c.ok == Some(false) {
                if let (Some(fix), Some(lbl)) = (&c.fix, &c.fix_label) {
                    if fix == "game" {
                        return; // the folder picker below handles it
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.add_enabled(!busy_job, egui::Button::new(
                            RichText::new(lbl).strong().color(egui::Color32::BLACK)).fill(ORANGE)).clicked()
                        {
                            let job = self.fix_job(fix, &[]);
                            self.run_jobs(vec![job]);
                        }
                    });
                }
            }
        });
    }

    fn game_folder_picker(&mut self, ui: &mut egui::Ui, busy_job: bool) {
        ui.indent("gamedir", |ui| {
            ui.label(RichText::new(
                "Paste the game's folder (Steam: right-click the game > Manage > Browse local files).")
                .color(MUTED));
            ui.horizontal(|ui| {
                ui.add(egui::TextEdit::singleline(&mut self.game_dir_input)
                    .hint_text("…/steamapps/common/MY HERO ACADEMIA All's Justice")
                    .desired_width(420.0));
                let ok = Path::new(self.game_dir_input.trim()).join("HeroGame").is_dir();
                if ui.add_enabled(ok && !busy_job, egui::Button::new("Use this folder")).clicked() {
                    let job = self.fix_job("game", &["--path".into(), self.game_dir_input.trim().into()]);
                    self.run_jobs(vec![job]);
                }
                if !self.game_dir_input.trim().is_empty() && !ok {
                    ui.colored_label(BAD, "no HeroGame folder inside");
                }
            });
        });
    }
}

/// ✔ / ✖ / – then the label and a muted detail.
fn row(ui: &mut egui::Ui, ok: Option<bool>, label: &str, detail: &str) {
    let (mark, col) = match ok {
        Some(true) => ("✔", GOOD),
        Some(false) => ("✖", BAD),
        None => ("–", MUTED),
    };
    ui.label(RichText::new(mark).color(col).strong());
    ui.label(label);
    if !detail.is_empty() {
        ui.label(RichText::new(detail).color(MUTED).small());
    }
}

pub fn open_url(url: &str) {
    #[cfg(windows)]
    {
        // Straight to the URL handler: `cmd /c start` would cut the URL at
        // its first & (query strings, like the prefilled combo form).
        let _ = Command::new("rundll32").args(["url.dll,FileProtocolHandler", url])
            .creation_flags(0x0800_0000).spawn();
    }
    #[cfg(not(windows))]
    {
        let _ = Command::new("xdg-open").arg(url).spawn();
    }
}

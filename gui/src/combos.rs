//! The Combos page: a library of saved combos for the picked character, an
//! editor with an explicit Save, and practice / watch controls.
//!
//! Saved combos live in models/demo_routes.json (written by ajbot.notation
//! --save, by practice.py after it practices, and deleted with ajbot.demo
//! delete). Practicing still saves on its own, with what it learned.

use crate::runner::sh_quote;
use crate::theme::{self, BAD, GOOD, MUTED, ORANGE, WARN};
use crate::{display, App, SavedRoute};
use eframe::egui::{self, RichText};
use std::time::Instant;

const REPO_ISSUES: &str = "https://github.com/DirectalArrowYT/ajbot/issues/new";
const REPO_TREE: &str = "https://github.com/DirectalArrowYT/ajbot/blob/main";
const TIERS: [&str; 3] = ["authored", "potential", "hypothetical"];

fn tier_color(t: &str) -> egui::Color32 {
    match t {
        "authored" => GOOD,
        "potential" => WARN,
        _ => egui::Color32::from_rgb(0xb4, 0x96, 0xf0),
    }
}

fn cap(t: &str) -> &'static str {
    match t {
        "authored" => "Authored",
        "potential" => "Potential",
        "hypothetical" => "Hypothetical",
        _ => "?",
    }
}

/// Percent-encoding for a query string value.
fn url_enc(s: &str) -> String {
    s.bytes().map(|b| match b {
        b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => (b as char).to_string(),
        _ => format!("%{b:02X}"),
    }).collect()
}

/// Where the editor stands against the saved file.
enum SaveState {
    Empty,
    New,
    Saved,
    Changed,
}

impl App {
    /// The name the editor's combo is (or would be) saved under: the Name box,
    /// else "<char>: <notation>" like ajbot.notation's default.
    fn combo_save_name(&self) -> String {
        let s = &self.settings;
        let n = s.combo_name.trim();
        if n.is_empty() {
            format!("{}: {}", s.combo_char, s.combo_text.trim())
        } else {
            n.to_string()
        }
    }

    fn combo_save_state(&self, routes: &[SavedRoute]) -> SaveState {
        let s = &self.settings;
        if s.combo_text.trim().is_empty() {
            return SaveState::Empty;
        }
        let name = self.combo_save_name();
        match routes.iter().find(|r| r.name == name) {
            None => SaveState::New,
            Some(r) if r.char_ == s.combo_char
                && r.notation.as_deref().map(str::trim) == Some(s.combo_text.trim()) => SaveState::Saved,
            Some(_) => SaveState::Changed,
        }
    }

    /// Run a short python command in the background, then re-read the saved
    /// combos and show `done` (or the error) under the library.
    fn combo_cmd(&mut self, ctx: &egui::Context, args: Vec<String>, done: String,
                 box_line: Option<String>) {
        {
            let mut sv = self.saved.lock().unwrap();
            if sv.busy {
                return;
            }
            sv.busy = true;
        }
        let shared = self.saved.clone();
        let python = self.settings.python.clone();
        let dir = self.dir();
        let host = self.settings.box_host.clone();
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let mut cmd = std::process::Command::new(&python);
            cmd.args(&args).current_dir(&dir).env("PYTHONIOENCODING", "utf-8")
                .stdin(std::process::Stdio::null());
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                cmd.creation_flags(0x0800_0000);
            }
            let out = cmd.output();
            let mut err = match out {
                Err(e) => Some(format!("could not run {python}: {e}")),
                Ok(o) if !o.status.success() => {
                    let e = String::from_utf8_lossy(&o.stderr);
                    Some(e.lines().filter(|l| !l.trim().is_empty()).last()
                         .unwrap_or("failed").to_string())
                }
                Ok(_) => None,
            };
            // The box keeps its own copy: delete there too, or the next
            // refresh from the box would bring the combo back.
            if let (None, Some(line)) = (&err, box_line) {
                let o = crate::setup::quiet(&mut std::process::Command::new("ssh"))
                    .args(["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", &host, &line])
                    .stdin(std::process::Stdio::null()).output();
                if !matches!(&o, Ok(o) if o.status.success()) {
                    err = Some(format!("{done} here, but not on the box (ssh failed)"));
                }
            }
            let mut sv = shared.lock().unwrap();
            sv.busy = false;
            sv.loaded = false;                 // re-read on the next frame
            sv.flash = Some(match err {
                None => (done, true),
                Some(e) => (e, false),
            });
            ctx.request_repaint();
        });
    }

    fn save_combo(&mut self, ctx: &egui::Context) {
        let s = &self.settings;
        let name = self.combo_save_name();
        let args = vec!["-m".into(), "ajbot.notation".into(), s.combo_text.trim().to_string(),
                        "--char".into(), s.combo_char.clone(), "--save".into(), name.clone()];
        self.saved_pick = Some(name.clone());
        self.save_settings();
        self.combo_cmd(ctx, args, format!("saved '{name}'"), None);
    }

    fn delete_combo(&mut self, ctx: &egui::Context, name: &str) {
        let s = &self.settings;
        let box_line = (s.combo_on_box && !s.box_host.is_empty()).then(|| format!(
            "cd {} && {} -m ajbot.demo delete {}",
            sh_quote(&s.box_dir), sh_quote(&s.box_python), sh_quote(name)));
        let args = vec!["-m".into(), "ajbot.demo".into(), "delete".into(), name.to_string()];
        if self.saved_pick.as_deref() == Some(name) {
            self.saved_pick = None;
        }
        self.combo_cmd(ctx, args, format!("deleted '{name}'"), box_line);
    }

    fn load_combo(&mut self, r: &SavedRoute) {
        self.settings.combo_char = r.char_.clone();
        // A default-named combo keeps an empty Name box (so editing the
        // notation doesn't pin the old text as its name).
        let default = format!("{}: {}", r.char_, r.notation.as_deref().unwrap_or("").trim());
        self.settings.combo_name = if r.name == default { String::new() } else { r.name.clone() };
        if let Some(n) = &r.notation {
            self.settings.combo_text = n.clone();
            self.preview_edit = Some(Instant::now());
        }
        self.saved_pick = Some(r.name.clone());
        self.save_settings();
    }

    /// Left card: the saved combos of the picked character (or everyone's).
    fn ui_combo_library(&mut self, ui: &mut egui::Ui, running: bool) {
        if !self.saved.lock().unwrap().loaded {
            self.refresh_saved(ui.ctx(), false);
        }
        let routes = self.saved.lock().unwrap().routes.clone();
        let char_ = self.settings.combo_char.clone();
        theme::card(ui, |ui| {
            ui.horizontal(|ui| {
                ui.label(RichText::new("Saved combos").strong().size(15.0).color(ORANGE));
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    let tip = if self.settings.combo_on_box {
                        "Re-read the saved combos, after copying in the box's"
                    } else {
                        "Re-read the saved combos (models/demo_routes.json)"
                    };
                    if ui.small_button("↻").on_hover_text(tip).clicked() {
                        let from_box = self.settings.combo_on_box;
                        self.refresh_saved(ui.ctx(), from_box);
                    }
                    ui.checkbox(&mut self.combo_all_chars, "all characters");
                });
            });
            let shown: Vec<&SavedRoute> = routes.iter()
                .filter(|r| self.combo_all_chars || r.char_ == char_).collect();
            let count = if self.combo_all_chars {
                format!("{} saved", shown.len())
            } else {
                format!("{} saved for {}", shown.len(), display(&char_))
            };
            ui.label(RichText::new(count).color(MUTED).small());
            ui.add_space(4.0);
            let mut load: Option<SavedRoute> = None;
            let mut watch: Option<SavedRoute> = None;
            let mut train: Option<SavedRoute> = None;
            let mut delete: Option<String> = None;
            egui::ScrollArea::vertical().id_salt("combo_library").max_height(380.0)
                .auto_shrink([false, true]).show(ui, |ui| {
                if shown.is_empty() {
                    ui.label(RichText::new(
                        "Nothing saved yet. Write a combo on the right and press Save.").color(MUTED));
                }
                for r in &shown {
                    let picked = self.saved_pick.as_deref() == Some(&r.name);
                    let frame = egui::Frame::new()
                        .fill(if picked { theme::WIDGET } else { theme::STRIPE })
                        .corner_radius(egui::CornerRadius::same(6))
                        .inner_margin(egui::Margin::symmetric(8, 5));
                    let resp = frame.show(ui, |ui| {
                        ui.set_width(ui.available_width());
                        // Buttons first (right to left), the title truncated in what is left.
                        let w = ui.available_width();
                        ui.allocate_ui_with_layout(egui::vec2(w, 24.0),
                            egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if self.combo_confirm_delete.as_deref() == Some(&r.name) {
                                if ui.button(RichText::new("Delete").color(BAD)).clicked() {
                                    delete = Some(r.name.clone());
                                }
                                if ui.button("Keep").clicked() {
                                    self.combo_confirm_delete = None;
                                }
                                ui.label(RichText::new("delete it?").color(BAD));
                            } else {
                                if ui.small_button("🗑").on_hover_text("Delete this saved combo").clicked() {
                                    self.combo_confirm_delete = Some(r.name.clone());
                                }
                                if ui.add_enabled(!running, egui::Button::new("▶ Watch").small())
                                    .on_hover_text(
                                        "Play it with the timing practice learned - nothing is tuned \
                                         or saved. Times and where it runs are set under Practice.")
                                    .clicked()
                                {
                                    watch = Some((*r).clone());
                                }
                                if ui.add_enabled(!running && !self.settings.combo_on_box,
                                                  egui::Button::new("🎮").small())
                                    .on_hover_text(
                                        "Train me: the game stops before every input and goes on when you press it on \
                                         your controller or keyboard (this PC's game)")
                                    .clicked()
                                {
                                    train = Some((*r).clone());
                                }
                                if r.attempts > 0 {
                                    let rate = r.landed as f32 / r.attempts as f32;
                                    let c = if rate >= 0.8 { GOOD } else if rate >= 0.3 { WARN } else { BAD };
                                    theme::pill(ui, &format!("{}/{} landed", r.landed, r.attempts), c);
                                } else if r.learned {
                                    theme::pill(ui, "timing learned", GOOD);
                                } else {
                                    theme::pill(ui, "not practiced", MUTED);
                                }
                            }
                            ui.with_layout(egui::Layout::left_to_right(egui::Align::Center), |ui| {
                                let title = if self.combo_all_chars {
                                    format!("{} - {}", display(&r.char_), r.name)
                                } else {
                                    r.name.clone()
                                };
                                ui.add(egui::Label::new(RichText::new(title).strong()).truncate());
                            });
                        });
                        let mut sub = format!("{} steps", r.steps);
                        if let Some(n) = &r.notation {
                            if *n != r.name && !r.name.ends_with(n.as_str()) {
                                sub += &format!(" · {n}");
                            }
                        }
                        ui.add(egui::Label::new(RichText::new(sub).color(MUTED).small().monospace()).wrap());
                    }).response;
                    let resp = resp.interact(egui::Sense::click())
                        .on_hover_cursor(egui::CursorIcon::PointingHand)
                        .on_hover_text("Click to open it in the editor");
                    if resp.clicked() {
                        load = Some((*r).clone());
                    }
                    ui.add_space(3.0);
                }
            });
            if let Some(r) = load {
                self.load_combo(&r);
            }
            if let Some(r) = watch {
                self.load_combo(&r);
                self.watch_saved(&r);
            }
            if let Some(r) = train {
                self.load_combo(&r);
                self.start_trainer(Some(&r));
            }
            if let Some(n) = delete {
                self.combo_confirm_delete = None;
                self.delete_combo(ui.ctx(), &n);
            }
            let sv = self.saved.lock().unwrap();
            if let Some(e) = &sv.error {
                ui.colored_label(BAD, e);
            }
            if let Some(n) = &sv.note {
                ui.label(RichText::new(n).color(MUTED).small());
            }
        });
    }

    /// combos/<char>.json for the picked character, re-read when it changes.
    fn library_combos(&mut self) -> Vec<serde_json::Value> {
        let char_ = self.settings.combo_char.clone();
        let path = self.dir().join("combos").join(format!("{char_}.json"));
        let mtime = std::fs::metadata(&path).and_then(|m| m.modified()).ok();
        let fresh = matches!(&self.library, Some((c, t, _)) if *c == char_ && *t == mtime);
        if !fresh {
            let list = std::fs::read_to_string(&path).ok()
                .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
                .and_then(|v| v["combos"].as_array().cloned())
                .unwrap_or_default();
            self.library = Some((char_, mtime, list));
        }
        self.library.as_ref().map(|l| l.2.clone()).unwrap_or_default()
    }

    /// The GitHub "Combo" issue form, prefilled.
    fn combo_form_url(&self, kind: &str, notation: &str, evidence: &str) -> String {
        let mut url = format!("{REPO_ISSUES}?template=combo.yml&labels=combo&character={}&notation={}",
                              url_enc(&self.settings.combo_char), url_enc(notation));
        url += &format!("&title={}", url_enc(&format!("[Combo] {}: {}", self.settings.combo_char, notation)));
        if !kind.is_empty() {
            url += &format!("&kind={}", url_enc(kind));
        }
        if !evidence.is_empty() {
            url += &format!("&evidence={}", url_enc(evidence));
        }
        url
    }

    /// Left card, under the saved combos: the shared combo library.
    fn ui_combo_community(&mut self, ui: &mut egui::Ui) {
        let combos = self.library_combos();
        let saved = self.saved.lock().unwrap().routes.clone();
        let char_ = self.settings.combo_char.clone();
        theme::card(ui, |ui| {
            ui.horizontal(|ui| {
                ui.label(RichText::new("Combo library").strong().size(15.0).color(ORANGE));
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui.small_button("Open on GitHub").on_hover_text(
                        "This character's page of the shared library (combos/ in the repo)").clicked()
                    {
                        crate::setup::open_url(&format!("{REPO_TREE}/combos/{char_}.md"));
                    }
                });
            });
            ui.label(RichText::new(
                "Shared combos everyone can add to. Authored: works every time. Potential: works, \
                 with a catch. Hypothetical: not proven yet (the game's CPU combos, routes the bot \
                 found) - practice one and report how it went.").color(MUTED).small());
            ui.add_space(4.0);
            ui.horizontal(|ui| {
                let n = |t: &str| combos.iter().filter(|c| c["tier"] == t).count();
                ui.selectable_value(&mut self.library_tier, String::new(), format!("All {}", combos.len()));
                for t in TIERS {
                    ui.selectable_value(&mut self.library_tier, t.to_string(),
                                        RichText::new(format!("{} {}", cap(t), n(t))).color(tier_color(t)));
                }
            });
            ui.add_space(4.0);
            let mut load: Option<serde_json::Value> = None;
            let mut report: Option<String> = None;
            egui::ScrollArea::vertical().id_salt("combo_community").max_height(320.0)
                .auto_shrink([false, true]).show(ui, |ui| {
                let shown: Vec<&serde_json::Value> = combos.iter()
                    .filter(|c| self.library_tier.is_empty() || c["tier"] == self.library_tier.as_str())
                    .collect();
                if shown.is_empty() {
                    ui.label(RichText::new(if combos.is_empty() {
                        format!("No combos for {} yet. Suggest one below.", display(&char_))
                    } else {
                        "None in this tier yet.".to_string()
                    }).color(MUTED));
                }
                for c in shown {
                    let tier = c["tier"].as_str().unwrap_or("");
                    let text = c["notation"].as_str().unwrap_or("").to_string();
                    let picked = self.settings.combo_text.trim() == text;
                    let frame = egui::Frame::new()
                        .fill(if picked { theme::WIDGET } else { theme::STRIPE })
                        .corner_radius(egui::CornerRadius::same(6))
                        .inner_margin(egui::Margin::symmetric(8, 5));
                    let resp = frame.show(ui, |ui| {
                        ui.set_width(ui.available_width());
                        let w = ui.available_width();
                        ui.allocate_ui_with_layout(egui::vec2(w, 22.0),
                            egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if tier == "hypothetical" && ui.small_button("Report").on_hover_text(
                                "Tell the library how it went (opens a prefilled GitHub form)").clicked()
                            {
                                report = Some(text.clone());
                            }
                            theme::pill(ui, cap(tier), tier_color(tier));
                            ui.with_layout(egui::Layout::left_to_right(egui::Align::Center), |ui| {
                                let title = c["name"].as_str().filter(|n| !n.is_empty())
                                    .map(String::from).unwrap_or_else(|| text.clone());
                                ui.add(egui::Label::new(RichText::new(title).strong()).truncate());
                            });
                        });
                        let mut sub: Vec<String> = vec![];
                        if c["name"].as_str().is_some_and(|n| !n.is_empty()) {
                            sub.push(text.clone());
                        }
                        for k in ["conditions", "downsides"] {
                            if let Some(v) = c[k].as_str() {
                                sub.push(v.to_string());
                            }
                        }
                        if c["requires"]["awake"].as_i64().unwrap_or(0) > 0 {
                            sub.push("needs Rising".into());
                        }
                        if let Some(s) = c["source"].as_str() {
                            sub.push(match s {
                                "game-cpu" => "from the game's CPU".into(),
                                "ml" => "found by the bot".into(),
                                other => other.to_string(),
                            });
                        }
                        if !sub.is_empty() {
                            ui.add(egui::Label::new(RichText::new(sub.join(" · "))
                                .color(MUTED).small()).wrap());
                        }
                    }).response;
                    let resp = resp.interact(egui::Sense::click())
                        .on_hover_cursor(egui::CursorIcon::PointingHand)
                        .on_hover_text("Click to open it in the editor, then Practice or Save it");
                    if resp.clicked() {
                        load = Some(c.clone());
                    }
                    ui.add_space(3.0);
                }
            });
            if let Some(c) = load {
                self.settings.combo_text = c["notation"].as_str().unwrap_or("").to_string();
                self.settings.combo_name = c["name"].as_str().unwrap_or("").to_string();
                self.saved_pick = None;
                self.preview_edit = Some(Instant::now());
                self.save_settings();
            }
            if let Some(text) = report {
                // The bot's own practice of it, if this PC has some.
                let ev = saved.iter()
                    .find(|r| r.char_ == char_ && r.notation.as_deref().map(str::trim) == Some(text.as_str())
                          && r.attempts > 0)
                    .map(|r| format!("Bot practice (AJBot): landed {} of {} attempts.", r.landed, r.attempts))
                    .unwrap_or_default();
                crate::setup::open_url(&self.combo_form_url(
                    "Test result for a combo already listed", &text, &ev));
            }
            ui.add_space(4.0);
            let text = self.settings.combo_text.trim().to_string();
            let listed = combos.iter().any(|c| c["notation"].as_str().map(str::trim) == Some(text.as_str()));
            if ui.add_enabled(!text.is_empty() && !listed, egui::Button::new("Suggest the editor's combo"))
                .on_hover_text("Opens a prefilled GitHub form to add it to the library")
                .on_disabled_hover_text(if listed { "Already in the library" } else { "Write a combo first" })
                .clicked()
            {
                let ev = saved.iter()
                    .find(|r| r.char_ == char_ && r.notation.as_deref().map(str::trim) == Some(text.as_str())
                          && r.attempts > 0)
                    .map(|r| format!("Bot practice (AJBot): landed {} of {} attempts.", r.landed, r.attempts))
                    .unwrap_or_default();
                crate::setup::open_url(&self.combo_form_url("A new combo", &text, &ev));
            }
        });
    }

    /// Right card: character, name, notation, Save / New.
    fn ui_combo_editor(&mut self, ui: &mut egui::Ui) {
        let routes = self.saved.lock().unwrap().routes.clone();
        theme::titled_card(ui, "Combo", |ui| {
            egui::Grid::new("combo_form").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                ui.label("Character");
                egui::ComboBox::from_id_salt("combo_char")
                    .selected_text(display(&self.settings.combo_char))
                    .height(400.0)
                    .show_ui(ui, |ui| {
                        for n in self.roster.clone() {
                            if ui.selectable_value(&mut self.settings.combo_char, n.clone(), display(&n))
                                .changed()
                            {
                                self.saved_pick = None;
                                self.combo_confirm_delete = None;
                            }
                        }
                    });
                ui.end_row();
                ui.label("Name").on_hover_text(
                    "What it is saved as. Empty = the combo text itself. A new name saves a copy.");
                ui.add(egui::TextEdit::singleline(&mut self.settings.combo_name)
                    .hint_text("(the combo text)").desired_width(f32::INFINITY));
                ui.end_row();
                ui.label("Combo");
                let r = ui.add(egui::TextEdit::multiline(&mut self.settings.combo_text)
                    .desired_rows(3)
                    .desired_width(f32::INFINITY)
                    .font(egui::TextStyle::Monospace)
                    .hint_text("AAA > Q1 > Dodge Cancel > AA > Q1 > Jump > Q1 > Q2"));
                if r.changed() {
                    self.preview_edit = Some(Instant::now());
                    self.saved.lock().unwrap().flash = None;
                }
                ui.end_row();
            });
            ui.add_space(6.0);
            let state = self.combo_save_state(&routes);
            let parse_ok = {
                let p = self.preview.lock().unwrap();
                p.error.is_none() && !p.busy && p.key.0 == self.settings.combo_text
                    && !p.steps.is_empty()
            };
            let busy = self.saved.lock().unwrap().busy;
            ui.horizontal(|ui| {
                let can_save = parse_ok && !busy && !matches!(state, SaveState::Empty | SaveState::Saved);
                let label = if matches!(state, SaveState::Changed) { "💾 Save changes" } else { "💾 Save" };
                let b = ui.add_enabled(can_save, egui::Button::new(
                    RichText::new(label).strong().color(egui::Color32::BLACK)).fill(ORANGE));
                let b = if !parse_ok && !self.settings.combo_text.trim().is_empty() {
                    b.on_disabled_hover_text("Fix the combo first (see What the bot will press)")
                } else {
                    b
                };
                if b.clicked() {
                    self.save_combo(ui.ctx());
                }
                if ui.button("New").on_hover_text("Clear the editor for a new combo").clicked() {
                    self.settings.combo_text.clear();
                    self.settings.combo_name.clear();
                    self.saved_pick = None;
                    self.preview_edit = Some(Instant::now());
                }
                match state {
                    SaveState::Empty => {}
                    SaveState::New => { theme::pill(ui, "not saved", WARN); }
                    SaveState::Saved => { theme::pill(ui, "saved", GOOD); }
                    SaveState::Changed => { theme::pill(ui, "unsaved changes", WARN); }
                }
            });
            let flash = self.saved.lock().unwrap().flash.clone();
            if let Some((t, ok)) = flash {
                ui.label(RichText::new(t).color(if ok { GOOD } else { BAD }).small());
            }
        });
    }

    fn ui_combo_practice(&mut self, ui: &mut egui::Ui, running: bool) {
        theme::titled_card(ui, "Practice", |ui| {
            ui.label(RichText::new(
                "The bot walks up to a standing dummy on the gym and repeats the combo, tuning the \
                 timing of each step until it lands it. What it learns is saved with the combo.")
                .color(MUTED));
            ui.add_space(4.0);
            egui::Grid::new("combo_practice").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                ui.label("Run on");
                ui.horizontal(|ui| {
                    ui.selectable_value(&mut self.settings.combo_on_box, false, "This PC's game (watch it)");
                    ui.selectable_value(&mut self.settings.combo_on_box, true, "Box (headless, fast)");
                });
                ui.end_row();
                ui.label("Tag partners").on_hover_text(
                    "Who is on the team besides the combo's character. Tags in the combo \
                     (t.Todoroki.Q1) put their target on the team first.");
                ui.text_edit_singleline(&mut self.settings.combo_partners);
                ui.end_row();
                ui.label("Attempts");
                ui.horizontal(|ui| {
                    ui.add(egui::DragValue::new(&mut self.settings.combo_attempts).range(5..=2000));
                    ui.label("at most, then");
                    ui.add(egui::DragValue::new(&mut self.settings.combo_confirm).range(1..=100));
                    ui.label("repeats to confirm it");
                });
                ui.end_row();
                ui.label("Watch");
                ui.horizontal(|ui| {
                    ui.add(egui::DragValue::new(&mut self.settings.combo_watch).range(1..=100));
                    ui.label("times");
                });
                ui.end_row();
            });
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                let has_text = !self.settings.combo_text.trim().is_empty();
                if ui.add_enabled(!running && has_text, egui::Button::new(
                    RichText::new("▶ Practice").strong().color(egui::Color32::BLACK)).fill(ORANGE))
                    .clicked()
                {
                    self.saved_pick = Some(self.combo_save_name());
                    self.start_practice();
                }
                let routes = self.saved.lock().unwrap().routes.clone();
                let saved = routes.iter().find(|r| r.name == self.combo_save_name()).cloned();
                if ui.add_enabled(!running && saved.is_some(), egui::Button::new("▶ Watch"))
                    .on_hover_text("Play the saved combo with its learned timing; nothing is tuned")
                    .on_disabled_hover_text("Save the combo first")
                    .clicked()
                {
                    if let Some(r) = &saved {
                        self.watch_saved(r);
                    }
                }
                let local = !self.settings.combo_on_box;
                if ui.add_enabled(!running && local && (has_text || saved.is_some()),
                                  egui::Button::new("🎮 Train me"))
                    .on_hover_text(
                        "Learn it yourself on this PC's game: the game stops before every input, \
                         the input shows on the game window, and it carries on when you press it \
                         on your controller or keyboard (the game's own key setup) - at your own \
                         pace. Back on the pad or Backspace ends it. Uses the saved combo's \
                         learned timing when there is one.")
                    .on_disabled_hover_text(if local {
                        "Type or pick a combo first"
                    } else {
                        "The trainer runs on this PC's game - pick \"This PC's game\" above"
                    })
                    .clicked()
                {
                    // The saved copy only while the editor still holds its combo.
                    let text = self.settings.combo_text.trim().to_string();
                    let same = saved.as_ref().filter(|r| text.is_empty()
                        || r.notation.as_deref().map(str::trim) == Some(text.as_str()));
                    self.saved_pick = Some(self.combo_save_name());
                    self.start_trainer(same);
                }
                if let Some(r) = self.runner.as_mut() {
                    if r.label.starts_with("Combo ") {
                        let stopping = r.stopping();
                        if ui.add_enabled(!stopping, egui::Button::new("■ Stop")).clicked() {
                            r.request_stop();
                        }
                    } else {
                        ui.label(RichText::new("something else is running - stop it on the Play page first")
                            .color(MUTED));
                    }
                }
            });
            if !self.status.is_empty() {
                ui.label(RichText::new(&self.status).color(MUTED).small());
            }
        });
    }

    fn ui_combo_steps(&mut self, ui: &mut egui::Ui) {
        theme::titled_card(ui, "What the bot will press", |ui| {
            let p = self.preview.lock().unwrap();
            if let Some(e) = &p.error {
                ui.colored_label(BAD, e);
            } else if p.busy || p.key.0 != self.settings.combo_text {
                ui.label(RichText::new("reading...").color(MUTED));
            } else if p.steps.is_empty() {
                ui.label(RichText::new("Write a combo above.").color(MUTED));
            }
            egui::ScrollArea::vertical().id_salt("combo_steps").max_height(320.0)
                .auto_shrink([false, true]).show(ui, |ui| {
                egui::Grid::new("combo_steps_grid").striped(true).num_columns(3).show(ui, |ui| {
                    for (k, st) in p.steps.iter().enumerate() {
                        ui.label(RichText::new(format!("{:2}", k + 1)).monospace().color(MUTED));
                        let mut t = st.label.clone();
                        if let Some(tag) = &st.tag {
                            t = format!("Tag in {tag}");
                        }
                        if let Some(c) = st.charge {
                            t += &format!("  (charge {c}f)");
                        }
                        ui.label(RichText::new(t).monospace().color(ORANGE));
                        let mut src = st.src.clone();
                        if let Some(n) = &st.note {
                            src += &format!("  - {n}");
                        }
                        ui.label(RichText::new(src).color(MUTED));
                        ui.end_row();
                    }
                });
            });
        });
    }

    fn ui_combo_legend(&mut self, ui: &mut egui::Ui) {
        theme::titled_card(ui, "Notation", |ui| {
            egui::Grid::new("combo_legend").num_columns(2).spacing([10.0, 2.0]).show(ui, |ui| {
                for (c, m) in [
                    ("A", "attack (jab) - AAA = three"),
                    ("CA", "counter attack (yellow)"),
                    ("GC", "guard-cancel counter"),
                    ("UB", "unblockable (red)"),
                    ("Q1 / Q2", "quirk 1 / quirk 2"),
                    ("TQ1 / Q3", "tilt quirk 1"),
                    ("TQ2 / Q4", "tilt quirk 2"),
                    ("PU", "Plus Ultra (PU2 = the second)"),
                    ("j.X", "jump, then X"),
                    ("d.X  bd.X", "dash / back dash, then X"),
                    ("r.X", "roll, then X"),
                    ("t.NAME.X", "tag in NAME, then X"),
                    ("[X]  [X]:40", "charge X (hold 40 frames)"),
                    ("( ... )", "a note, not pressed"),
                    ("Jump, Dash, Dodge Cancel,", "Guard, Just Guard, Escape,"),
                    ("Rising, Extreme Action", "written out also work"),
                ] {
                    ui.label(RichText::new(c).monospace().color(ORANGE));
                    ui.label(m);
                    ui.end_row();
                }
            });
            ui.add_space(4.0);
            ui.label(RichText::new(
                "Steps go between > or -. Timing is not written: each gap starts from how a \
                 real player times that pair (else a guess) and practice tunes it. \
                 Plus Ultra needs gauge; the dummy stands still.").color(MUTED).small());
        });
    }

    pub(crate) fn ui_combos(&mut self, ui: &mut egui::Ui) {
        let running = self.runner.is_some() || self.native_recorder.is_some();
        theme::page_title(ui, "Combos",
            "Write a combo the way players do, save it, and let the bot practice it on a dummy.");
        self.refresh_preview(ui.ctx());
        egui::ScrollArea::vertical().id_salt("combos_page").auto_shrink([false, false]).show(ui, |ui| {
            ui.columns(2, |cols| {
                self.ui_combo_library(&mut cols[0], running);
                cols[0].add_space(8.0);
                self.ui_combo_community(&mut cols[0]);
                self.ui_combo_editor(&mut cols[1]);
                cols[1].add_space(8.0);
                self.ui_combo_practice(&mut cols[1], running);
            });
            ui.add_space(8.0);
            ui.columns(2, |cols| {
                self.ui_combo_steps(&mut cols[0]);
                self.ui_combo_legend(&mut cols[1]);
            });
        });
    }
}

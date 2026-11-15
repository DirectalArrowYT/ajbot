//! The Combos page: a library of saved combos for the picked character, an
//! editor with an explicit Save, and practice / watch controls.
//!
//! Saved combos live in models/demo_routes.json (written by ajbot.notation
//! --save, by practice.py after it practises, and deleted with ajbot.demo
//! delete). Practising still saves on its own, with what it learned.

use crate::runner::sh_quote;
use crate::theme::{self, BAD, GOOD, MUTED, ORANGE, WARN};
use crate::{display, App, SavedRoute};
use eframe::egui::{self, RichText};
use std::time::Instant;

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
                let o = std::process::Command::new("ssh")
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
                                         or saved. Times and where it runs are set under Practise.")
                                    .clicked()
                                {
                                    watch = Some((*r).clone());
                                }
                                if r.attempts > 0 {
                                    let rate = r.landed as f32 / r.attempts as f32;
                                    let c = if rate >= 0.8 { GOOD } else if rate >= 0.3 { WARN } else { BAD };
                                    theme::pill(ui, &format!("{}/{} landed", r.landed, r.attempts), c);
                                } else if r.learned {
                                    theme::pill(ui, "timing learned", GOOD);
                                } else {
                                    theme::pill(ui, "not practised", MUTED);
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
        theme::titled_card(ui, "Practise", |ui| {
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
                    RichText::new("▶ Practise").strong().color(egui::Color32::BLACK)).fill(ORANGE))
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
            "Write a combo the way players do, save it, and let the bot practise it on a dummy.");
        self.refresh_preview(ui.ctx());
        egui::ScrollArea::vertical().id_salt("combos_page").auto_shrink([false, false]).show(ui, |ui| {
            ui.columns(2, |cols| {
                self.ui_combo_library(&mut cols[0], running);
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

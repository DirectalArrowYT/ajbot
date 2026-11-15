//! The Train page. "This PC" retrains the model here with training/retrain.bat.
//! "Server" (only when ajbot_local.json lists one) pushes recordings to a
//! training server, fetches its league champion, or retrains over there.

use crate::theme::{self, MUTED};
use crate::{App, Job};
use eframe::egui::{self, RichText};
use std::path::Path;

/// Server names in ajbot_local.json ("servers": {name: {...}}).
pub fn servers(dir: &Path) -> Vec<String> {
    let path = std::env::var("AJBOT_LOCAL")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| dir.join("ajbot_local.json"));
    let Ok(text) = std::fs::read_to_string(path) else { return vec![] };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) else { return vec![] };
    let mut names: Vec<String> = v.get("servers").and_then(|s| s.as_object())
        .map(|o| o.keys().cloned().collect()).unwrap_or_default();
    names.sort();
    names
}

impl App {
    fn py_job(&self, label: &str, args: &[&str]) -> Job {
        let mut a = vec!["-u".to_string()];
        a.extend(args.iter().map(|s| s.to_string()));
        Job { program: self.settings.python.clone(), args: a, label: label.into(), recheck: false, env: vec![] }
    }

    pub(crate) fn ui_train(&mut self, ui: &mut egui::Ui) {
        theme::page_title(ui, "Train",
            "Teach the bot from your recordings: retrain its model from everything recorded so far.");
        if self.servers.is_empty() {
            self.train_on_server = false;
        } else {
            ui.horizontal(|ui| {
                ui.selectable_value(&mut self.train_on_server, false, RichText::new("This PC").strong());
                ui.selectable_value(&mut self.train_on_server, true, RichText::new("Server").strong());
            });
            ui.add_space(6.0);
        }
        if self.train_on_server {
            self.ui_train_server(ui);
        } else {
            self.ui_train_local(ui);
        }
    }

    fn ui_train_local(&mut self, ui: &mut egui::Ui) {
        let busy = self.runner.is_some();
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            theme::titled_card(ui, "Retrain model", |ui| {
                ui.label(RichText::new(
                    "Runs training\\retrain.bat: digests new recordings (runs\\ and botruns\\), mines \
                     combos and fits a new models\\policy.pkl. The old model moves to models\\prev\\, \
                     so a worse retrain can be put back. Uses most of the CPU and memory for a few \
                     minutes; best with the game closed.")
                    .color(MUTED));
                ui.horizontal(|ui| {
                    ui.label("train.py flags");
                    ui.add(egui::TextEdit::singleline(&mut self.local_train_args)
                        .hint_text("empty = quick fit")
                        .desired_width(260.0));
                    if ui.small_button("careful fit").on_hover_text(
                        "--cherrypick: compares every candidate on a held-out match first (slow)")
                        .clicked()
                    {
                        self.local_train_args = "--cherrypick".into();
                    }
                });
                ui.add_space(4.0);
                if ui.add_enabled(!busy, egui::Button::new(
                    RichText::new("Retrain model").strong().color(egui::Color32::BLACK))
                    .fill(theme::ORANGE)).clicked()
                {
                    let mut args = vec!["/c".to_string(), "training\\retrain.bat".into()];
                    args.extend(self.local_train_args.split_whitespace().map(String::from));
                    let env = vec![("PYTHON".to_string(), self.settings.python.clone()),
                                   ("AJBOT_NO_PAUSE".to_string(), "1".to_string())];
                    self.run_jobs(vec![Job { program: "cmd".into(), args, label: "retrain".into(),
                                             recheck: false, env }]);
                }
            });
        });
    }

    fn ui_train_server(&mut self, ui: &mut egui::Ui) {
        let busy = self.runner.is_some();
        let names = self.servers.clone();
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            ui.label(RichText::new(
                "The league trains on the server around the clock. Feed it recordings and fetch its champion.")
                .color(MUTED));
            ui.add_space(6.0);
            theme::titled_card(ui, "League champion", |ui| {
                ui.label(RichText::new(
                    "Pushes your new recordings to the server, then installs the current champion \
                     into models/ if it is newer (the old model is kept, pickable as Bot B).")
                    .color(MUTED));
                ui.horizontal(|ui| {
                    if ui.add_enabled(!busy, egui::Button::new(
                        RichText::new("Sync + get champion").strong().color(egui::Color32::BLACK))
                        .fill(theme::ORANGE)).clicked()
                    {
                        let jobs = vec![self.py_job("sync", &["training/remote_retrain.py", "--sync"]),
                                        self.py_job("champion", &["training/pull_champion.py"])];
                        self.run_jobs(jobs);
                    }
                    if ui.add_enabled(!busy, egui::Button::new("What does the server have?")).clicked() {
                        let j = self.py_job("champion check", &["training/pull_champion.py", "--check"]);
                        self.run_jobs(vec![j]);
                    }
                    if ui.add_enabled(!busy, egui::Button::new("Reinstall champion")).clicked() {
                        let j = self.py_job("champion", &["training/pull_champion.py", "--force"]);
                        self.run_jobs(vec![j]);
                    }
                });
            });
            ui.add_space(8.0);
            theme::titled_card(ui, "Recordings", |ui| {
                ui.label(RichText::new(
                    "Copies finished recordings to the server (checked, then removed here).")
                    .color(MUTED));
                if ui.add_enabled(!busy, egui::Button::new("Push recordings now")).clicked() {
                    let j = self.py_job("sync", &["training/remote_retrain.py", "--sync"]);
                    self.run_jobs(vec![j]);
                }
            });
            ui.add_space(8.0);
            theme::titled_card(ui, "One-off retrain on the server", |ui| {
                ui.label(RichText::new(
                    "Trains one policy there from every recording and pulls it back. The league \
                     already does this each generation; use it to try different train.py flags.")
                    .color(MUTED));
                ui.horizontal(|ui| {
                    ui.label("server");
                    egui::ComboBox::from_id_salt("train_host")
                        .selected_text(&self.train_host)
                        .show_ui(ui, |ui| {
                            for n in &names {
                                ui.selectable_value(&mut self.train_host, n.clone(), n);
                            }
                        });
                    ui.label("train.py flags");
                    ui.add(egui::TextEdit::singleline(&mut self.train_args).desired_width(260.0));
                });
                ui.horizontal(|ui| {
                    if ui.add_enabled(!busy, egui::Button::new("Retrain on server")).clicked() {
                        let (h, a) = (self.train_host.clone(), self.train_args.clone());
                        let j = self.py_job("retrain", &["training/remote_retrain.py", "--host", &h, "--args", &a]);
                        self.run_jobs(vec![j]);
                    }
                    if ui.add_enabled(!busy, egui::Button::new("Only fetch its latest policy")).clicked() {
                        let h = self.train_host.clone();
                        let j = self.py_job("pull", &["training/remote_retrain.py", "--host", &h, "--pull"]);
                        self.run_jobs(vec![j]);
                    }
                });
            });
            ui.add_space(8.0);
            ui.label(RichText::new(format!(
                "Servers come from ajbot_local.json: {}", names.join(", "))).color(MUTED).small());
        });
    }
}

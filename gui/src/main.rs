//! AJBot front end.
//!
//!   Play        pick a team from the roster (with the game's own icons) or
//!               use whatever is already selected in-game, choose Bot vs CPU,
//!               Bot vs Bot or Record me, and start/stop with a live log.
//!   Statistics  wins and losses per character, team and opponent, time on
//!               point (how much data each character has), just guards,
//!               counters, change combos... read from stats/matches.jsonl,
//!               which the Python side appends to after every match.
//!   Combos      combo tester: type a combo the way players write it
//!               (AAA > Q1 > Dodge Cancel > ...), see it parsed, and have the
//!               bot practise it until it lands (practice.py), on the box's
//!               headless game copy or on this PC's game.
//!   Settings    where AJBot lives and which Python to run.
//!
//! This is a front end only: the bot itself still runs as the Python scripts
//! (bot/play.py, bot/play2.py, ...), launched as child processes.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod charts;
mod hookpipe;
mod record_control;
mod runner;
mod setup;
mod stats;
mod theme;
mod train;

use charts::{bar_row, win_rate_color};
use eframe::egui;
use runner::Runner;
use serde::{Deserialize, Serialize};
use stats::{Filter, Stats};
use std::collections::{HashMap, VecDeque};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use theme::{ORANGE, WIDGET};

const ICON_W: f32 = 100.0;
const ICON_H: f32 = 60.0;
const LOW_DATA_MIN: f64 = 10.0; // minutes on point below which a character "needs data"

// ------------------------------------------------------------------ settings

#[derive(Serialize, Deserialize, Clone)]
#[serde(default)]
struct Settings {
    ajbot_dir: String,
    python: String,
    model: String,
    hz: f32,
    p1: bool,
    tag: bool,
    defense: bool,
    learn: bool,
    resources: bool,
    escape: bool,
    record: bool,
    pick_start: String,
    /// Bot vs Bot: endless random matches with automatic rematch.
    auto_loop: bool,
    /// Cursor on returning to character select: "none" = still on Random.
    auto_again_left: bool,
    /// One-shot: what the control-mode screen shows now ("" = trust memory).
    control_now: String,
    /// Online lobby picking: "best" (most confident), "counter" (vs the team
    /// last fought, re-picked every match) or "manual".
    online_pick: String,
    /// Bot vs Bot: policy for bot B ("" = same as bot A).
    model_b: String,
    /// Bot vs Bot with model_b, endless: swap sides every N matches (0 = never).
    swap_every: u32,
    // Combo tester
    combo_char: String,
    combo_text: String,
    combo_name: String,
    combo_on_box: bool,
    combo_attempts: u32,
    combo_confirm: u32,
    combo_partners: String,
    /// How many times "Watch" plays a saved combo.
    combo_watch: u32,
    box_host: String,
    box_dir: String,
    box_python: String,
    box_instance: u32,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            ajbot_dir: String::new(),
            python: "python".into(),
            model: "models/policy.pkl".into(),
            hz: 15.0,
            p1: true,
            tag: true,
            defense: true,
            learn: true,
            resources: true,
            escape: true,
            record: true,
            pick_start: "DekuFinal".into(),
            auto_loop: false,
            auto_again_left: false,
            control_now: String::new(),
            online_pick: "best".into(),
            model_b: String::new(),
            swap_every: 2,
            combo_char: "Bakugo".into(),
            combo_text: "AAA > Q1 > Dodge Cancel > AA > Q1 > Jump > Q1 > Q2 (Jump Followup)".into(),
            combo_name: String::new(),
            // This PC's game by default: not everyone using the GUI has the box.
            combo_on_box: false,
            combo_attempts: 200,
            combo_confirm: 10,
            combo_partners: "Todoroki, Uraraka".into(),
            combo_watch: 5,
            box_host: String::new(),
            box_dir: String::new(),
            box_python: String::new(),
            box_instance: 4,
        }
    }
}

/// "models/prev/policy-20261110-033008.pkl" -> "policy-20261110-033008".
/// A model folder's policy.pkl is named after its folder:
/// "models/prev-local-20261111/policy.pkl" -> "prev-local-20261111",
/// "models/policy.pkl" -> "policy".
fn stem(p: &str) -> String {
    let path = Path::new(p);
    let s = path.file_stem().map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| p.to_string());
    if s == "policy" {
        if let Some(d) = path.parent().and_then(|d| d.file_name()) {
            let d = d.to_string_lossy();
            if d != "models" {
                return d.into_owned();
            }
        }
    }
    s
}

/// Policy files a bot can load: models/*.pkl (policy only), every
/// models/prev/*.pkl, and every model folder models/<name>/policy.pkl (a
/// folder carries its own data files - combo book, move and threat stats),
/// newest first, as paths relative to the AJBot folder.
fn list_models(dir: &Path) -> Vec<String> {
    let mut out: Vec<(std::time::SystemTime, String)> = vec![];
    if let Ok(rd) = std::fs::read_dir(dir.join("models")) {
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            let pkl = e.path().join("policy.pkl");
            if name == "prev" || !pkl.is_file() {
                continue;
            }
            let t = pkl.metadata().and_then(|m| m.modified())
                .unwrap_or(std::time::UNIX_EPOCH);
            out.push((t, format!("models/{name}/policy.pkl")));
        }
    }
    for sub in ["models", "models/prev"] {
        let Ok(rd) = std::fs::read_dir(dir.join(sub)) else { continue };
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            if !name.ends_with(".pkl") || !name.starts_with("policy") {
                continue;
            }
            let t = e.metadata().and_then(|m| m.modified())
                .unwrap_or(std::time::UNIX_EPOCH);
            out.push((t, format!("{sub}/{name}")));
        }
    }
    out.sort_by(|a, b| b.0.cmp(&a.0));
    out.into_iter().map(|(_, p)| p).collect()
}

fn settings_path() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.join("ajbot_gui_settings.json")))
        .unwrap_or_else(|| PathBuf::from("ajbot_gui_settings.json"))
}

/// The AJBot folder: the one holding play.py. Looked for next to the exe and
/// in its parents (the exe may sit in gui/target/release), then the cwd.
fn find_ajbot_dir() -> String {
    let mut cands = vec![];
    if let Ok(exe) = std::env::current_exe() {
        let mut d = exe.parent().map(Path::to_path_buf);
        while let Some(dir) = d {
            cands.push(dir.clone());
            d = dir.parent().map(Path::to_path_buf);
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        cands.push(cwd);
    }
    for c in cands {
        if c.join("bot").join("play.py").exists() && c.join("ajbot").exists() {
            return c.display().to_string();
        }
    }
    String::new()
}

// -------------------------------------------------------------------- roster

/// Roster names in character-select order, parsed from ajbot/roster.py so the
/// GUI never drifts from what the picker automation expects.
fn load_roster(dir: &Path) -> Vec<String> {
    let text = std::fs::read_to_string(dir.join("ajbot").join("roster.py")).unwrap_or_default();
    let mut out = vec![];
    let mut inside = false;
    for line in text.lines() {
        let l = line.trim();
        if l.starts_with("DISPLAY_ORDER") {
            inside = true;
            continue;
        }
        if inside {
            if l.starts_with(']') {
                break;
            }
            if let Some(start) = l.find('"') {
                if let Some(end) = l[start + 1..].find('"') {
                    out.push(l[start + 1..start + 1 + end].to_string());
                }
            }
        }
    }
    if out.is_empty() {
        // Fall back to whatever icons exist.
        if let Ok(rd) = std::fs::read_dir(dir.join("assets").join("icons")) {
            for e in rd.flatten() {
                let p = e.path();
                if p.extension().map(|x| x == "png").unwrap_or(false) {
                    if let Some(s) = p.file_stem() {
                        out.push(s.to_string_lossy().to_string());
                    }
                }
            }
            out.sort();
        }
    }
    out
}

/// Friendlier label for roster ids the game names generically.
fn display(name: &str) -> String {
    match name {
        "DLC1" => "Shinso".into(),
        "DLC2" => "Aoyama".into(),
        "DLC3" => "Present Mic".into(),
        "DLC4" => "Midnight".into(),
        "DLC5" => "Kurogiri".into(),
        "HighendNomu1" => "High-End".into(),
        "DekuHalfNaked" => "Deku (Rising)".into(),
        "DekuOverlay" => "Deku (Overlay)".into(),
        "ArmorAllMight" => "Armored All Might".into(),
        "AwakeAFO" => "AFO (Rewind)".into(),
        "AllForOneShigaraki" => "AFO (Chaos)".into(),
        other => other.to_string(),
    }
}

fn load_icon(ctx: &egui::Context, path: &Path) -> Option<egui::TextureHandle> {
    let img = image::open(path).ok()?.to_rgba8();
    let size = [img.width() as usize, img.height() as usize];
    let color = egui::ColorImage::from_rgba_unmultiplied(size, img.as_raw());
    Some(ctx.load_texture(path.display().to_string(), color, egui::TextureOptions::LINEAR))
}

// ----------------------------------------------------------------------- app

#[derive(PartialEq, Clone, Copy)]
enum Tab {
    Setup,
    Play,
    Combos,
    Stats,
    Train,
    Settings,
}

/// One step of a one-shot job list (setup fixes, sync then pull, updates):
/// run to completion, then the next step starts.
pub(crate) struct Job {
    pub program: String,
    pub args: Vec<String>,
    pub label: String,
    /// Re-run the Setup checks once the list finishes.
    pub recheck: bool,
}

/// The notation preview: ajbot.notation's parse of the text box, done by the
/// Python side (one parser for the GUI and practice.py) in a thread.
#[derive(Default)]
struct Preview {
    /// (text, character) the result is for.
    key: (String, String),
    steps: Vec<PreviewStep>,
    error: Option<String>,
    busy: bool,
}

#[derive(Clone)]
struct PreviewStep {
    label: String,
    src: String,
    tag: Option<String>,
    note: Option<String>,
    charge: Option<i64>,
}

/// Saved combos: the routes in this folder's models/demo_routes.json (what
/// practice.py saves, with the timing it learned).
#[derive(Default)]
struct Saved {
    routes: Vec<SavedRoute>,
    error: Option<String>,
    busy: bool,
    loaded: bool,
    note: Option<String>,
}

#[derive(Clone)]
struct SavedRoute {
    name: String,
    char_: String,
    notation: Option<String>,
    steps: usize,
    attempts: i64,
    landed: i64,
    learned: bool,
    /// The whole route, handed to practice.py --route-json.
    json: String,
}

fn read_routes(text: &str) -> Result<Vec<serde_json::Value>, String> {
    let v: serde_json::Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
    Ok(v.get("routes").and_then(|r| r.as_array()).cloned().unwrap_or_default())
}

fn route_updated(r: &serde_json::Value) -> i64 {
    r.pointer("/learned/updated").and_then(|x| x.as_i64()).unwrap_or(0)
}

/// Merge the box's saved combos into this folder's file: new names are
/// added, and a name both have keeps whichever was practised last.
fn merge_box_routes(local: &Path, host: &str, box_dir: &str) -> Result<String, String> {
    let remote = format!("{}/models/demo_routes.json", box_dir.trim_end_matches('/'));
    let out = std::process::Command::new("ssh")
        .args(["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "cat", &remote])
        .stdin(std::process::Stdio::null())
        .output()
        .map_err(|e| format!("ssh: {e}"))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        return Err(format!("box: {}", err.lines().last().unwrap_or("ssh failed")));
    }
    let theirs = read_routes(&String::from_utf8_lossy(&out.stdout))?;
    let mut data: serde_json::Value = std::fs::read_to_string(local).ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_else(|| serde_json::json!({"routes": []}));
    let mut mine = data.get("routes").and_then(|r| r.as_array()).cloned().unwrap_or_default();
    let (mut added, mut newer) = (0, 0);
    for r in theirs {
        let name = r.get("name").and_then(|n| n.as_str()).unwrap_or("").to_string();
        match mine.iter().position(|m| m.get("name").and_then(|n| n.as_str()) == Some(&name)) {
            None => { mine.push(r); added += 1; }
            Some(i) if route_updated(&r) > route_updated(&mine[i]) => { mine[i] = r; newer += 1; }
            _ => {}
        }
    }
    if added + newer > 0 {
        data["routes"] = serde_json::Value::Array(mine);
        if let Some(d) = local.parent() {
            let _ = std::fs::create_dir_all(d);
        }
        let tmp = local.with_extension("gui.tmp");
        std::fs::write(&tmp, serde_json::to_string_pretty(&data).unwrap_or_default())
            .map_err(|e| e.to_string())?;
        std::fs::rename(&tmp, local).map_err(|e| e.to_string())?;
    }
    Ok(format!("from the box: {added} new, {newer} updated"))
}

#[derive(PartialEq, Clone, Copy)]
enum Mode {
    BotVsCpu,
    HumanVsBot,
    OnlineLobby,
    BotVsBot,
    Record,
}

#[derive(PartialEq, Clone, Copy)]
enum TeamSource {
    InGame,
    Pick,
    /// The bot picks a random team itself, a new one every match.
    Random,
}

#[derive(PartialEq, Clone, Copy)]
enum CharSort {
    Name,
    Matches,
    WinRate,
    Data,
    Dealt,
}

struct App {
    settings: Settings,
    tab: Tab,
    mode: Mode,
    source: TeamSource,
    teams: [Vec<String>; 2],
    editing: usize,
    search: String,
    roster: Vec<String>,
    icons: HashMap<String, egui::TextureHandle>,
    icons_loaded: bool,
    runner: Option<Runner>,
    native_recorder: Option<record_control::NativeRecorder>,
    last_log: Option<runner::Log>,
    status: String,
    stats: Stats,
    last_refresh: Instant,
    filter: Filter,
    // Recomputing the summary is an O(matches) pass over the whole history
    // with fresh HashMaps and string allocations - fine once, not on every
    // repaint a scroll or hover triggers. Cached until the file or the
    // filter actually changes.
    stats_cache: Option<(Option<std::time::SystemTime>, Filter, stats::Summary)>,
    char_sort: CharSort,
    sort_desc: bool,
    autoscroll: bool,
    show_console: bool,
    preview: std::sync::Arc<std::sync::Mutex<Preview>>,
    preview_edit: Option<Instant>,
    saved: std::sync::Arc<std::sync::Mutex<Saved>>,
    saved_pick: Option<String>,
    setup: setup::Shared,
    game_dir_input: String,
    jobs: VecDeque<Job>,
    jobs_recheck: bool,
    /// Training servers from ajbot_local.json (Train page shown when any).
    servers: Vec<String>,
    train_host: String,
    train_args: String,
}

impl App {
    fn new(cc: &eframe::CreationContext<'_>) -> Self {
        theme::apply(&cc.egui_ctx);
        let first_run = !settings_path().exists();
        let mut settings: Settings = std::fs::read_to_string(settings_path())
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or_default();
        if settings.ajbot_dir.is_empty() || !Path::new(&settings.ajbot_dir).join("bot").join("play.py").exists() {
            settings.ajbot_dir = find_ajbot_dir();
        }
        // A bundled interpreter (<AJBot>/python/python.exe, as in a copy made
        // for someone without Python set up) wins over a bare "python" from
        // PATH, and over a saved path that no longer exists (folder moved).
        let bundled = Path::new(&settings.ajbot_dir).join("python").join("python.exe");
        let stale = settings.python.contains(['\\', '/']) && !Path::new(&settings.python).exists();
        if bundled.is_file()
            && (settings.python.trim().is_empty() || settings.python == "python" || stale)
        {
            settings.python = bundled.display().to_string();
        }
        let roster = load_roster(Path::new(&settings.ajbot_dir));
        let servers = train::servers(Path::new(&settings.ajbot_dir));
        // What a GUI self-update left behind (it renames the running exe).
        if let Ok(exe) = std::env::current_exe() {
            let _ = std::fs::remove_file(exe.with_extension("exe.old"));
        }
        let mut app = App {
            settings,
            tab: if first_run { Tab::Setup } else { Tab::Play },
            mode: Mode::BotVsCpu,
            source: TeamSource::InGame,
            teams: [vec![], vec![]],
            editing: 0,
            search: String::new(),
            roster,
            icons: HashMap::new(),
            icons_loaded: false,
            runner: None,
            native_recorder: None,
            last_log: None,
            status: String::new(),
            stats: Stats::default(),
            last_refresh: Instant::now() - Duration::from_secs(60),
            filter: Filter { bot: true, selfplay: true, human: false, backfill: true },
            stats_cache: None,
            char_sort: CharSort::Matches,
            sort_desc: true,
            autoscroll: true,
            show_console: true,
            preview: Default::default(),
            preview_edit: Some(Instant::now()),
            saved: Default::default(),
            saved_pick: None,
            setup: Default::default(),
            game_dir_input: String::new(),
            jobs: VecDeque::new(),
            jobs_recheck: false,
            train_host: servers.first().cloned().unwrap_or_default(),
            servers,
            train_args: "--cherrypick --quick".into(),
        };
        // AJBOT_GUI_PAGE=play|combos|stats|train|settings|setup opens that page.
        if let Ok(pg) = std::env::var("AJBOT_GUI_PAGE") {
            app.tab = match pg.to_lowercase().as_str() {
                "play" => Tab::Play,
                "combos" => Tab::Combos,
                "stats" => Tab::Stats,
                "train" => Tab::Train,
                "settings" => Tab::Settings,
                _ => Tab::Setup,
            };
        }
        if app.settings.ajbot_dir.is_empty() {
            app.status = "AJBot folder not found - set it in Settings".into();
            app.tab = Tab::Settings;
        } else {
            app.setup_refresh(&cc.egui_ctx);
        }
        app
    }

    fn dir(&self) -> PathBuf {
        PathBuf::from(&self.settings.ajbot_dir)
    }

    /// Run a list of one-shot jobs one after another (see Job).
    pub(crate) fn run_jobs(&mut self, jobs: Vec<Job>) {
        if self.runner.is_some() || self.native_recorder.is_some() {
            self.status = "something is already running - stop it first".into();
            return;
        }
        self.jobs = jobs.into();
        self.next_job(None);
    }

    /// Start the next queued job; its console continues the previous log.
    fn next_job(&mut self, prev: Option<runner::Log>) -> bool {
        let Some(j) = self.jobs.pop_front() else { return false };
        self.jobs_recheck |= j.recheck;
        match Runner::start_plain(&j.program, &self.dir(), &j.args, &j.label) {
            Ok(r) => {
                if let Some(prev) = prev {
                    let old: Vec<String> = prev.lock().unwrap().iter().cloned().collect();
                    let mut l = r.log.lock().unwrap();
                    for line in old.into_iter().rev() {
                        l.push_front(line);
                    }
                }
                self.status = format!("{} running", j.label);
                self.show_console = true;
                self.runner = Some(r);
                true
            }
            Err(e) => {
                self.status = e;
                self.jobs.clear();
                false
            }
        }
    }

    fn save_settings(&self) {
        if let Ok(t) = serde_json::to_string_pretty(&self.settings) {
            let _ = std::fs::write(settings_path(), t);
        }
    }

    fn ensure_icons(&mut self, ctx: &egui::Context) {
        if self.icons_loaded {
            return;
        }
        self.icons_loaded = true;
        let dir = self.dir().join("assets").join("icons");
        for name in &self.roster {
            if let Some(t) = load_icon(ctx, &dir.join(format!("{name}.png"))) {
                self.icons.insert(name.clone(), t);
            }
        }
    }

    // ------------------------------------------------------------- launch
    fn build_command(&self) -> Result<(String, Vec<String>, String), String> {
        let s = &self.settings;
        let mut a: Vec<String> = vec![];
        let flag = |a: &mut Vec<String>, on: bool, f: &str| {
            if !on {
                a.push(f.to_string());
            }
        };
        match self.mode {
            // Record mode is handled natively in start() - see
            // record_control::NativeRecorder - and never reaches here.
            Mode::Record => Err("Record mode is handled natively".into()),
            Mode::BotVsCpu | Mode::HumanVsBot | Mode::OnlineLobby => {
                let online = self.mode == Mode::OnlineLobby;
                let vs_human = self.mode == Mode::HumanVsBot;
                if online {
                    a.push("--online".into());
                    match s.online_pick.as_str() {
                        "best" => a.extend(["--pick-best".into(), "--pick-start".into(), s.pick_start.clone()]),
                        "counter" => a.extend(["--pick-counter".into(), "--pick-start".into(), s.pick_start.clone()]),
                        _ => {}
                    }
                }
                a.push("--model".into());
                a.push(s.model.clone());
                a.push("--hz".into());
                a.push(format!("{}", s.hz));
                // Against a human, whoever plugs in first claims Player 1 - the
                // bot's virtual pad should not fight over that slot, so --p1
                // is only ever sent for the solo vs-CPU flow.
                if s.p1 && !vs_human {
                    a.push("--p1".into());
                }
                flag(&mut a, s.tag, "--no-tag");
                flag(&mut a, s.defense, "--no-defense");
                if s.defense && !s.learn {
                    a.push("--no-learn".into());
                }
                flag(&mut a, s.resources, "--no-resources");
                flag(&mut a, s.escape, "--no-escape");
                flag(&mut a, s.record, "--no-record");
                let base = if online { "Online lobby (Player 1)" }
                           else if vs_human { "Human vs Bot" } else { "Bot vs CPU" };
                let mut label = base.to_string();
                if self.source == TeamSource::Pick && !online {
                    if self.teams[0].len() != 3 {
                        return Err("pick 3 characters for the bot's team".into());
                    }
                    a.push("--pick".into());
                    a.push(self.teams[0].join(","));
                    a.push("--pick-start".into());
                    a.push(s.pick_start.clone());
                    label = format!("{base}: {}", self.teams[0].join(" / "));
                } else if self.source == TeamSource::Random && !online {
                    a.extend(["--pick-random".into(), "--pick-start".into(), s.pick_start.clone()]);
                    label = format!("{base}: random teams");
                }
                Ok(("bot/play.py".into(), a, label))
            }
            Mode::BotVsBot => {
                a.push("--model".into());
                a.push(s.model.clone());
                let duel = !s.model_b.is_empty() && s.model_b != s.model;
                let vs = if duel {
                    a.extend(["--model-b".into(), s.model_b.clone()]);
                    if s.auto_loop && s.swap_every > 0 {
                        a.extend(["--swap-every".into(), s.swap_every.to_string()]);
                    }
                    // "keep learning" in a duel: model A (the one being
                    // tested) learns into its own tables, B stays frozen.
                    if s.defense && s.learn {
                        a.extend(["--learner".into(), "A".into()]);
                    }
                    format!(" [{}{} vs {}]", stem(&s.model),
                            if s.defense && s.learn { " (learning)" } else { "" },
                            stem(&s.model_b))
                } else {
                    String::new()
                };
                flag(&mut a, s.tag, "--no-tag");
                flag(&mut a, s.defense, "--no-defense");
                if s.defense && !s.learn {
                    a.push("--no-learn".into());
                }
                flag(&mut a, s.resources, "--no-resources");
                flag(&mut a, s.escape, "--no-escape");
                flag(&mut a, s.record, "--no-record");
                let mut label = "Bot vs Bot".to_string();
                if s.auto_loop {
                    a.push("--auto".into());
                    if s.auto_again_left {
                        a.extend(["--auto-cursor".into(), "reset".into()]);
                    }
                    if self.source == TeamSource::Pick {
                        // Same matchup every match: both teams fixed.
                        if self.teams[0].len() != 3 || self.teams[1].len() != 3 {
                            return Err("pick 3 characters for each team".into());
                        }
                        a.extend(["--a".into(), self.teams[0].join(","),
                                  "--b".into(), self.teams[1].join(",")]);
                        return Ok(("bot/play2.py".into(), a, format!(
                            "Bot vs Bot: endless {} vs {}{vs}",
                            self.teams[0].join("/"), self.teams[1].join("/"))));
                    }
                    return Ok(("bot/play2.py".into(), a,
                               format!("Bot vs Bot: endless random matches{vs}")));
                }
                if self.source == TeamSource::Pick {
                    if self.teams[0].len() != 3 || self.teams[1].len() != 3 {
                        return Err("pick 3 characters for each team".into());
                    }
                    a.extend(["--a".into(), self.teams[0].join(","),
                              "--b".into(), self.teams[1].join(","),
                              "--pick".into(), "--pick-start".into(), s.pick_start.clone()]);
                    label = format!("Bot vs Bot: {} vs {}",
                                    self.teams[0].join("/"), self.teams[1].join("/"));
                }
                Ok(("bot/play2.py".into(), a, label + &vs))
            }
        }
    }

    fn start(&mut self) {
        if self.mode == Mode::Record {
            let ts = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0);
            let out_path = self.dir().join("runs").join(format!("{ts}.jsonl"));
            let mut rec = record_control::NativeRecorder::new();
            rec.start(&self.settings.python, &self.dir(), &out_path, 2);
            self.status = match &rec.state {
                record_control::RecorderState::Error(e) => e.clone(),
                _ => "recording natively".into(),
            };
            self.native_recorder = Some(rec);
            return;
        }
        match self.build_command() {
            Err(e) => self.status = e,
            Ok((script, mut args, label)) => {
                if !self.settings.control_now.is_empty() && script != "bot/record.py" {
                    args.push("--control-now".into());
                    args.push(self.settings.control_now.clone());
                    // One-shot: after this run the bot's own memory is right.
                    self.settings.control_now.clear();
                    self.save_settings();
                }
                match Runner::start(&self.settings.python, &self.dir(), &script, &args, &label) {
                    Ok(r) => {
                        self.status = format!("running: {label}");
                        self.runner = Some(r);
                    }
                    Err(e) => self.status = e,
                }
            }
        }
    }

    // --------------------------------------------------------------- tabs
    fn ui_play(&mut self, ui: &mut egui::Ui) {
        let running = self.runner.is_some() || self.native_recorder.is_some();
        ui.horizontal_wrapped(|ui| {
            ui.add_enabled_ui(!running, |ui| {
                ui.label("Mode:");
                ui.selectable_value(&mut self.mode, Mode::BotVsCpu, "Bot vs CPU");
                ui.selectable_value(&mut self.mode, Mode::HumanVsBot, "Human vs Bot");
                ui.selectable_value(&mut self.mode, Mode::OnlineLobby, "Online lobby (bot = P1)");
                ui.selectable_value(&mut self.mode, Mode::BotVsBot, "Bot vs Bot");
                ui.selectable_value(&mut self.mode, Mode::Record, "Record me");
                if self.mode != Mode::Record && self.mode != Mode::OnlineLobby {
                    ui.separator();
                    ui.label("Teams:");
                    ui.selectable_value(&mut self.source, TeamSource::InGame, "Already selected in-game");
                    ui.selectable_value(&mut self.source, TeamSource::Pick, "Pick for me");
                    if self.mode == Mode::HumanVsBot || self.mode == Mode::BotVsCpu {
                        ui.selectable_value(&mut self.source, TeamSource::Random, "Random")
                            .on_hover_text("The bot picks three random characters at character \
                                            select, and a new random team every time you go \
                                            back to character select.");
                    } else if self.source == TeamSource::Random {
                        self.source = TeamSource::InGame;
                    }
                }
            });
        });

        if self.mode != Mode::Record {
            ui.add_enabled_ui(!running, |ui| {
                let ajdir = self.dir();
                ui.horizontal_wrapped(|ui| {
                    let s = &mut self.settings;
                    ui.checkbox(&mut s.tag, "Tagging");
                    ui.checkbox(&mut s.defense, "Defence (JG / counter)");
                    ui.add_enabled(s.defense, egui::Checkbox::new(&mut s.learn, "keep learning"));
                    ui.checkbox(&mut s.resources, "Rising / Plus Ultra");
                    ui.checkbox(&mut s.escape, "Emergency dodge");
                    ui.checkbox(&mut s.record, "Record for training");
                    if self.mode == Mode::BotVsBot {
                        ui.checkbox(&mut s.auto_loop, egui::RichText::new(
                            "Endless matches (auto rematch)").color(ORANGE))
                            .on_hover_text("Start on the 2-player character select. Both bots pick                                 random teams (favouring characters with little data); after each                                 match Player 1 goes Continue -> Character Select and they pick                                 again. Leave it running.");
                        if s.auto_loop {
                            ui.checkbox(&mut s.auto_again_left, "cursor resets to the first fighter between matches")
                                .on_hover_text("The bots pick random teams themselves (so they know                                     each character's control-mode setting). If the cursor returns to                                     the first fighter after a match instead of staying on the last                                     pick, tick this - the loop also detects it and switches itself.");
                        }
                    }
                    if self.mode == Mode::BotVsBot {
                        ui.end_row();
                        ui.label("Bot B plays:")
                            .on_hover_text("Pick an older policy for bot B to test whether the                                             newest model got better. Old models are kept in                                             models/prev each time training replaces policy.pkl.");
                        let shown = if s.model_b.is_empty() || s.model_b == s.model {
                            "same model as A".to_string()
                        } else {
                            s.model_b.clone()
                        };
                        egui::ComboBox::from_id_salt("model_b")
                            .selected_text(shown)
                            .width(320.0)
                            .show_ui(ui, |ui| {
                                ui.selectable_value(&mut s.model_b, String::new(), "same model as A");
                                for m in &list_models(&ajdir) {
                                    if *m != s.model {
                                        ui.selectable_value(&mut s.model_b, m.clone(), m.as_str());
                                    }
                                }
                            });
                        if !s.model_b.is_empty() && s.model_b != s.model && s.auto_loop {
                            ui.add(egui::DragValue::new(&mut s.swap_every).range(0..=50))
                                .on_hover_text("Swap which side each model plays every N matches                                                 (same teams), so Player 1 / Player 2 advantage                                                 cancels out. 0 = never swap.");
                            ui.label("swap sides every N matches");
                        }
                    }
                    if self.mode == Mode::BotVsCpu {
                        ui.checkbox(&mut s.p1, "Require Player 1");
                        ui.add(egui::Slider::new(&mut s.hz, 5.0..=30.0).text("decisions/s"));
                    }
                    if self.mode == Mode::OnlineLobby {
                        ui.horizontal(|ui| {
                            ui.label("Team:");
                            ui.radio_value(&mut s.online_pick, "best".to_string(), "Most confident")
                                .on_hover_text("The 3 characters with the most convincing record in \
                                                Statistics (a long winning record beats a short \
                                                lucky one).");
                            ui.radio_value(&mut s.online_pick, "counter".to_string(), "Counter last opponent")
                                .on_hover_text("The 3 characters with the best record against the \
                                                team it just fought, re-picked at character select \
                                                before every match. First match: counters the last \
                                                online opponent, or most confident if none.");
                            ui.radio_value(&mut s.online_pick, "manual".to_string(), "I pick");
                        });
                    }
                    if self.mode == Mode::HumanVsBot || self.mode == Mode::OnlineLobby {
                        ui.add(egui::Slider::new(&mut s.hz, 5.0..=30.0).text("decisions/s"));
                    }
                });
            });
        }

        ui.separator();
        let auto = self.mode == Mode::BotVsBot && self.settings.auto_loop;
        let picking = self.mode != Mode::Record && self.mode != Mode::OnlineLobby
            && self.source == TeamSource::Pick;
        if picking {
            self.ui_team_slots(ui, running);
            if auto {
                ui.label(egui::RichText::new("Endless: these two teams every match. Open the                           2-player vs character select, then Start.").color(ORANGE));
            }
            ui.add_space(4.0);
        } else if self.mode == Mode::OnlineLobby {
            if self.settings.online_pick != "manual" {
                let later = if self.settings.online_pick == "counter" {
                    "After each match it works out a counter to the team it just fought \
                     and picks that when character select comes back."
                } else {
                    "After each match it picks its most confident team again when character \
                     select comes back."
                };
                ui.label(format!("Disconnect your own controller first so the bot's pad becomes \
                          Player 1. Get into the lobby's character select with the cursor on the \
                          start fighter, then Start - the bot picks, waits for the opponent, and \
                          plays. {later}"));
            } else {
                ui.label("Disconnect your own controller first so the bot's pad becomes Player 1, \
                          then start this. Nothing is pressed until a match loads - pick the \
                          team in the lobby yourself; the bot plays match after match.");
            }
        } else if auto {
            ui.label("Endless mode: open the 2-player vs character select, then Start.                       Both bots pick random teams (Teams: Pick for me = the same teams every match) and rematch forever - every match is recorded                       and added to Statistics. Stop whenever you like.");
        } else if self.source == TeamSource::Random && self.mode != Mode::Record {
            ui.label("Open character select with the bot's cursor on the start fighter, then \
                      Start - the bot picks a random team and plays. After each match, go \
                      Continue -> Character Select and it picks a new random team (a rematch \
                      keeps the same team).");
        } else if self.mode == Mode::HumanVsBot {
            ui.label("Plug in your controller first so you claim Player 1, then start this - the \
                      bot takes whichever pad slot is left. Plays whatever teams are on screen; \
                      start it at character select, the loading screen or mid-battle, and it keeps \
                      going match after match, including real wins/losses (not just training mode).");
        } else if self.mode != Mode::Record {
            ui.label("The bot plays whatever teams are on screen - start it at character select, \
                      the loading screen or mid-battle. It keeps going match after match.");
        } else {
            ui.label("Records your own play for training and adds each match to the statistics.");
        }

        ui.horizontal(|ui| {
            if running {
                let stopping = self.runner.as_ref().map(|r| r.stopping()).unwrap_or(false);
                if ui.add_enabled(!stopping, egui::Button::new("  Stop  ")).clicked() {
                    if let Some(r) = self.runner.as_mut() {
                        r.request_stop();
                    }
                    if let Some(rec) = self.native_recorder.as_mut() {
                        rec.stop();
                        self.native_recorder = None;
                        self.status = "recording stopped".into();
                    }
                }
                if stopping && ui.button("Force kill").clicked() {
                    if let Some(r) = self.runner.as_mut() {
                        r.kill();
                    }
                }
                if let Some(r) = &self.runner {
                    let secs = r.started.elapsed().as_secs();
                    ui.label(format!("{}  ({}:{:02})", r.label, secs / 60, secs % 60));
                }
                if let Some(rec) = &self.native_recorder {
                    if let record_control::RecorderState::Recording(status) = &rec.state {
                        ui.label(format!("Recording your play  ({} ticks)", status.ticks));
                    } else {
                        ui.label("Recording your play  (starting...)");
                    }
                }
            } else if theme::primary_button(ui, "  ▶  Start  ").clicked() {
                self.save_settings();
                self.start();
            }
            ui.label(&self.status);
        });
        ui.separator();

        if picking {
            let h = (ui.available_height() * 0.55).max(180.0);
            egui::ScrollArea::vertical().id_salt("roster").max_height(h).show(ui, |ui| {
                self.ui_roster_grid(ui, running);
            });
            ui.separator();
        }
    }

    fn ui_team_slots(&mut self, ui: &mut egui::Ui, running: bool) {
        let n_teams = if self.mode == Mode::BotVsBot { 2 } else { 1 };
        for t in 0..n_teams {
            ui.horizontal(|ui| {
                let label = if n_teams == 1 { "Bot's team".to_string() }
                            else if t == 0 { "Bot A (Player 1)".into() } else { "Bot B (Player 2)".into() };
                let sel = self.editing == t;
                if ui.selectable_label(sel, format!("{label}:")).clicked() {
                    self.editing = t;
                }
                let mut remove = None;
                for (i, name) in self.teams[t].iter().enumerate() {
                    ui.vertical(|ui| {
                        if let Some(tex) = self.icons.get(name) {
                            let r = ui.add(egui::Image::new(tex)
                                .fit_to_exact_size(egui::vec2(ICON_W * 0.8, ICON_H * 0.8))
                                .sense(egui::Sense::click()));
                            if r.clicked() && !running {
                                remove = Some(i);
                            }
                            r.on_hover_text("click to remove");
                        }
                        ui.small(display(name));
                    });
                }
                for _ in self.teams[t].len()..3 {
                    ui.add_sized([ICON_W * 0.8, ICON_H * 0.8], egui::Label::new("  (empty)  "));
                }
                if let Some(i) = remove {
                    self.teams[t].remove(i);
                }
                if !running && !self.teams[t].is_empty() && ui.small_button("clear").clicked() {
                    self.teams[t].clear();
                }
            });
        }
        ui.horizontal(|ui| {
            ui.label("Search:");
            ui.text_edit_singleline(&mut self.search);
            ui.small("Click a character to add it to the highlighted team. \
                      Badge = matches played / win rate; red = needs more data.");
        });
    }

    fn ui_roster_grid(&mut self, ui: &mut egui::Ui, running: bool) {
        let data = stats::data_time(&self.stats.matches);
        let cols = ((ui.available_width() / (ICON_W + 8.0)).floor() as usize).max(4);
        let q = self.search.to_lowercase();
        let names: Vec<String> = self.roster.iter()
            .filter(|n| q.is_empty() || n.to_lowercase().contains(&q))
            .cloned().collect();
        egui::Grid::new("roster_grid").spacing([6.0, 6.0]).show(ui, |ui| {
            for (i, name) in names.iter().enumerate() {
                let (secs, w, l) = data.get(name).cloned().unwrap_or_default();
                let taken_in = self.teams[self.editing].contains(name).then_some(self.editing);
                let in_other = self.teams.iter().enumerate().any(|(k, t)| k != self.editing && t.contains(name));
                ui.vertical(|ui| {
                    let resp = if let Some(tex) = self.icons.get(name) {
                        let mut img = egui::Image::new(tex)
                            .fit_to_exact_size(egui::vec2(ICON_W, ICON_H))
                            .sense(egui::Sense::click());
                        if taken_in.is_some() {
                            img = img.tint(egui::Color32::from_gray(90));
                        } else if in_other {
                            img = img.tint(egui::Color32::from_rgb(190, 200, 235));
                        }
                        ui.add(img)
                    } else {
                        ui.add_sized([ICON_W, ICON_H], egui::Button::new(name.as_str()))
                    };
                    let mins = secs / 60.0;
                    let badge = if w + l > 0 {
                        format!("{} · {}%", w + l, (100.0 * w as f64 / (w + l) as f64).round())
                    } else {
                        "no matches".into()
                    };
                    let color = if mins < LOW_DATA_MIN { egui::Color32::from_rgb(220, 90, 80) }
                                else { ui.visuals().weak_text_color() };
                    ui.label(egui::RichText::new(display(name)).small().strong());
                    ui.label(egui::RichText::new(format!("{badge} · {mins:.0}m")).small().color(color));
                    let resp = resp.on_hover_text(format!(
                        "{name}\n{w} wins, {l} losses\n{mins:.1} min on point (all modes)"));
                    if resp.clicked() && !running {
                        let t = self.editing;
                        if let Some(other) = taken_in {
                            self.teams[other].retain(|n| n != name);
                        } else if self.teams[t].len() < 3 {
                            self.teams[t].push(name.clone());
                            if self.teams[t].len() == 3 && self.mode == Mode::BotVsBot && t == 0 {
                                self.editing = 1;
                            }
                        }
                    }
                });
                if (i + 1) % cols == 0 {
                    ui.end_row();
                }
            }
        });
    }

    /// (text, colour) for the recording indicator, from the newest "[rec]"
    /// heartbeat. Red whenever data should be flowing and is not.
    fn rec_indicator(&self) -> (String, egui::Color32) {
        let green = egui::Color32::from_rgb(90, 200, 110);
        let red = egui::Color32::from_rgb(235, 80, 70);
        let amber = egui::Color32::from_rgb(235, 170, 60);
        let grey = egui::Color32::from_gray(150);
        if let Some(rec) = &self.native_recorder {
            return match &rec.state {
                record_control::RecorderState::Idle => ("not running".into(), grey),
                record_control::RecorderState::Starting => ("starting native recording...".into(), grey),
                record_control::RecorderState::Recording(s) => {
                    (format!("● REC (native)  {} ticks -> {}", s.ticks, s.path), green)
                }
                record_control::RecorderState::Error(e) => (format!("REC error: {e}"), red),
            };
        }
        let Some(r) = &self.runner else {
            return ("not running".into(), grey);
        };
        let status = r.rec.lock().unwrap().clone();
        let Some(st) = status else {
            let secs = r.started.elapsed().as_secs();
            return if secs > 20 {
                (format!("no recorder heartbeat after {secs}s - is the game running with the hook?"), red)
            } else {
                ("starting...".into(), grey)
            };
        };
        let silent = st.at.elapsed().as_secs();
        if silent >= 6 {
            return (format!("REC? recorder silent for {silent}s - script busy or stuck"), red);
        }
        if st.line.starts_with("LIVE") {
            let flat = st.last_growth.elapsed().as_secs();
            if flat >= 5 {
                return (format!("REC STALLED - no new data for {flat}s | {}", st.line), red);
            }
            return (format!("● REC  {}", st.line), green);
        }
        if st.line.starts_with("OFF") {
            return (format!("not recording | {}", st.line), grey);
        }
        (format!("○ {}", st.line), amber)
    }

    fn ui_console(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.strong(egui::RichText::new("Console").color(ORANGE));
            let (txt, col) = self.rec_indicator();
            ui.label(egui::RichText::new(txt).color(col).strong().monospace());
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.small_button("hide").clicked() {
                    self.show_console = false;
                }
                if let Some(r) = self.runner.as_mut() {
                    if !r.stopping() && ui.small_button("■ stop").clicked() {
                        r.request_stop();
                    }
                }
                ui.checkbox(&mut self.autoscroll, "follow");
            });
        });
        ui.separator();
        self.ui_log(ui);
    }

    fn ui_log(&mut self, ui: &mut egui::Ui) {
        let lines: Vec<String> = match (&self.runner, &self.native_recorder, &self.last_log) {
            (Some(r), _, _) => r.log.lock().unwrap().iter().cloned().collect(),
            (None, Some(_), _) => {
                // Native recording has no subprocess stdout to show - the
                // status line above the log already carries everything
                // useful (tick count, path, errors).
                vec!["(recording natively - see status above)".into()]
            }
            (None, None, Some(l)) => l.lock().unwrap().iter().cloned().collect(),
            (None, None, None) => vec!["(not running)".into()],
        };
        let mut sa = egui::ScrollArea::vertical().id_salt("log").auto_shrink([false, false]);
        if self.autoscroll {
            sa = sa.stick_to_bottom(true);
        }
        sa.show(ui, |ui| {
            for l in lines.iter().rev().take(1500).collect::<Vec<_>>().into_iter().rev() {
                let mut t = egui::RichText::new(l).monospace().size(12.0);
                if l.contains("MATCH WIN") {
                    t = t.color(egui::Color32::from_rgb(90, 200, 110)).strong();
                } else if l.contains("MATCH LOSS") {
                    t = t.color(egui::Color32::from_rgb(230, 100, 90)).strong();
                } else if l.contains("!!") || l.contains("Traceback") || l.contains("Error") {
                    t = t.color(egui::Color32::from_rgb(230, 170, 60));
                } else if l.contains("=== match") {
                    t = t.strong();
                }
                ui.label(t);
            }
        });
    }

    fn ui_stats(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.label("Include:");
            ui.checkbox(&mut self.filter.bot, "Bot vs CPU");
            ui.checkbox(&mut self.filter.selfplay, "Bot vs Bot");
            ui.checkbox(&mut self.filter.human, "My own play");
            ui.checkbox(&mut self.filter.backfill, "Imported old recordings");
            if ui.button("Reload").clicked() {
                self.stats.loaded_mtime = None;
                self.stats.refresh(&self.dir());
            }
        });
        if let Some(e) = &self.stats.error {
            ui.colored_label(egui::Color32::from_rgb(230, 170, 60), e);
            return;
        }
        let fresh = match &self.stats_cache {
            Some((mtime, filter, _)) => *mtime == self.stats.loaded_mtime && *filter == self.filter,
            None => false,
        };
        if !fresh {
            let summary = stats::summarize(&self.stats.matches, &self.filter);
            self.stats_cache = Some((self.stats.loaded_mtime, self.filter.clone(), summary));
        }
        let s = self.stats_cache.as_ref().unwrap().2.clone();
        let decided = s.wins + s.losses;
        let wr = if decided > 0 { 100.0 * s.wins as f64 / decided as f64 } else { 0.0 };
        ui.separator();
        ui.horizontal_wrapped(|ui| {
            ui.heading(format!("{} - {}", s.wins, s.losses));
            ui.label(format!("win rate {wr:.0}%   ·   {} sessions ({} undecided: training / stopped / abandoned)   ·   {:.1} h played",
                             s.total, s.other, s.play_s / 3600.0));
        });
        let per = |x: f64| if decided > 0 { x / decided.max(1) as f64 } else { 0.0 };
        let ev = |k: &str| s.events.get(k).cloned().unwrap_or(0.0);
        ui.label(format!(
            "per match: dealt {:.2} · taken {:.2} · KOs {:.1}-{:.1} · just guards {:.1} · counters {:.1} · \
             change combos {:.1} · changes {:.1} · emergency dodges {:.1} · guard breaks {:.1}",
            per(s.dealt), per(s.taken), per(s.kos_dealt as f64), per(s.kos_taken as f64),
            per(ev("just_guards")), per(ev("counters")), per(ev("change_combos")),
            per(ev("changes")), per(ev("escapes")), per(ev("guard_breaks"))));
        if s.combo_matches > 0 {
            let avg = if s.combos > 0.0 { s.combo_hits / s.combos } else { 0.0 };
            ui.label(egui::RichText::new(format!(
                "combos (game rule: hits never stopped): {:.1} per match · average {:.1} hits ·                  best {} hits · 2-fighter chains {:.0} · 3-fighter chains {:.0} · Plus Ultra finishers {:.0}",
                s.combos / s.combo_matches as f64, avg, s.combo_best as i64,
                s.chains_2, s.chains_3, s.pu_finishers)).color(ORANGE));
        }
        ui.separator();

        egui::ScrollArea::vertical().id_salt("stats").auto_shrink([false, false]).show(ui, |ui| {
            egui::CollapsingHeader::new("Characters (the bot's side)").default_open(true).show(ui, |ui| {
                self.ui_char_chart(ui, &s.chars);
                ui.add_space(6.0);
                self.ui_char_table(ui, s.chars.clone());
            });
            egui::CollapsingHeader::new("Best and worst teams").default_open(true).show(ui, |ui| {
                self.ui_team_chart(ui, &s.teams);
                ui.add_space(6.0);
                egui::Grid::new("teams").striped(true).show(ui, |ui| {
                    ui.strong("Team");
                    ui.strong("W");
                    ui.strong("L");
                    ui.strong("Win %");
                    ui.end_row();
                    let mut teams = s.teams.clone();
                    teams.sort_by(|a, b| {
                        let ra = a.wins as f64 / (a.wins + a.losses).max(1) as f64;
                        let rb = b.wins as f64 / (b.wins + b.losses).max(1) as f64;
                        rb.partial_cmp(&ra).unwrap().then((b.wins + b.losses).cmp(&(a.wins + a.losses)))
                    });
                    for t in teams.iter().take(25) {
                        ui.label(&t.team);
                        ui.label(t.wins.to_string());
                        ui.label(t.losses.to_string());
                        ui.label(format!("{:.0}%", 100.0 * t.wins as f64 / (t.wins + t.losses).max(1) as f64));
                        ui.end_row();
                    }
                });
            });
            egui::CollapsingHeader::new("Against each opponent").default_open(false).show(ui, |ui| {
                let mut opps = s.opponents.clone();
                opps.sort_by(|a, b| b.matches.cmp(&a.matches));
                egui::Grid::new("opps").striped(true).show(ui, |ui| {
                    ui.strong("");
                    ui.strong("Opponent");
                    ui.strong("Matches");
                    ui.strong("Bot won");
                    ui.strong("Bot lost");
                    ui.strong("Win %");
                    ui.end_row();
                    for o in &opps {
                        self.small_icon(ui, &o.name);
                        ui.label(display(&o.name));
                        ui.label(o.matches.to_string());
                        ui.label(o.wins.to_string());
                        ui.label(o.losses.to_string());
                        ui.label(o.win_rate().map(|r| format!("{:.0}%", r * 100.0)).unwrap_or("-".into()));
                        ui.end_row();
                    }
                });
            });
            egui::CollapsingHeader::new("Recent matches").default_open(true).show(ui, |ui| {
                egui::Grid::new("recent").striped(true).show(ui, |ui| {
                    for h in ["Result", "Mode", "Length", "Team", "Opponents", "Dealt", "Taken", "KOs", "JG", "Counters", "Change combos", "Best combo", "3-chains"] {
                        ui.strong(h);
                    }
                    ui.end_row();
                    let recent: Vec<_> = self.stats.matches.iter().rev()
                        .filter(|m| self.filter.keep(m)).take(60).cloned().collect();
                    for m in recent {
                        let (txt, col) = match m.result.as_str() {
                            "win" => ("WIN", egui::Color32::from_rgb(90, 200, 110)),
                            "loss" => ("LOSS", egui::Color32::from_rgb(230, 100, 90)),
                            other => (other, ui.visuals().weak_text_color()),
                        };
                        ui.colored_label(col, txt).on_hover_text(&m.reason);
                        let mode = match (m.mode.as_str(), &m.side) {
                            ("selfplay", Some(s)) => format!("bot {s}"),
                            (x, _) => x.to_string(),
                        };
                        ui.label(mode);
                        let d = m.duration_s as u64;
                        ui.label(format!("{}:{:02}", d / 60, d % 60));
                        ui.label(m.team.iter().map(|n| display(n)).collect::<Vec<_>>().join(" / "));
                        ui.label(m.opponents.iter().map(|n| display(n)).collect::<Vec<_>>().join(" / "));
                        ui.label(format!("{:.2}", m.dealt));
                        ui.label(format!("{:.2}", m.taken));
                        ui.label(format!("{}-{}", m.kos_dealt, m.kos_taken));
                        let e = |k: &str| m.events.get(k).cloned().unwrap_or(0.0);
                        ui.label(format!("{}", e("just_guards")));
                        ui.label(format!("{}", e("counters")));
                        ui.label(format!("{}", e("change_combos")));
                        if m.combos > 0.0 {
                            ui.label(format!("{} hits", m.combo_max as i64));
                            ui.label(format!("{}", m.chains_3 as i64));
                        } else {
                            ui.label("-");
                            ui.label("-");
                        }
                        ui.end_row();
                    }
                });
            });
        });
    }

    fn small_icon(&self, ui: &mut egui::Ui, name: &str) {
        if let Some(tex) = self.icons.get(name) {
            ui.add(egui::Image::new(tex).fit_to_exact_size(egui::vec2(40.0, 24.0)));
        } else {
            ui.label("");
        }
    }

    /// Win-rate bar per character, most-played first, capped so the chart
    /// stays readable once the roster has real history.
    fn ui_char_chart(&self, ui: &mut egui::Ui, chars: &[stats::CharStat]) {
        let mut cs: Vec<_> = chars.iter().filter(|c| c.matches > 0).collect();
        if cs.is_empty() {
            ui.label(egui::RichText::new("no decided matches yet").weak());
            return;
        }
        cs.sort_by(|a, b| b.matches.cmp(&a.matches));
        let bar_w = (ui.available_width() - 220.0).max(120.0);
        for c in cs.iter().take(18) {
            let rate = c.win_rate().unwrap_or(0.0);
            let label = format!("{} ({})", display(&c.name), c.matches);
            let value = format!("{:.0}%  {}-{}", rate * 100.0, c.wins, c.losses);
            bar_row(ui, 170.0, bar_w, &label, rate as f32, win_rate_color(rate), WIDGET, &value);
        }
        if cs.len() > 18 {
            ui.label(egui::RichText::new(format!("+ {} more in the table below", cs.len() - 18)).weak().small());
        }
    }

    /// Best and worst teams by win rate, each needing at least 2 decided
    /// matches so one lucky or unlucky match does not dominate the chart.
    fn ui_team_chart(&self, ui: &mut egui::Ui, teams: &[stats::TeamStat]) {
        let rated: Vec<(&stats::TeamStat, f64)> = teams.iter()
            .filter(|t| t.wins + t.losses >= 2)
            .map(|t| (t, t.wins as f64 / (t.wins + t.losses) as f64))
            .collect();
        if rated.is_empty() {
            ui.label(egui::RichText::new(
                "not enough decided matches per team yet (need 2+) - see every team below").weak());
            return;
        }
        let bar_w = (ui.available_width() - 260.0).max(120.0);
        let mut best = rated.clone();
        best.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap().then((b.0.wins + b.0.losses).cmp(&(a.0.wins + a.0.losses))));
        let mut worst = rated;
        worst.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap().then((b.0.wins + b.0.losses).cmp(&(a.0.wins + a.0.losses))));

        ui.strong("Best");
        for (t, rate) in best.iter().take(5) {
            let value = format!("{:.0}%  {}-{}", rate * 100.0, t.wins, t.losses);
            bar_row(ui, 210.0, bar_w, &t.team, *rate as f32, win_rate_color(*rate), WIDGET, &value);
        }
        ui.add_space(4.0);
        ui.strong("Needs work");
        for (t, rate) in worst.iter().take(5) {
            let value = format!("{:.0}%  {}-{}", rate * 100.0, t.wins, t.losses);
            bar_row(ui, 210.0, bar_w, &t.team, *rate as f32, win_rate_color(*rate), WIDGET, &value);
        }
    }

    fn ui_char_table(&mut self, ui: &mut egui::Ui, mut chars: Vec<stats::CharStat>) {
        let key = self.char_sort;
        chars.sort_by(|a, b| {
            let o = match key {
                CharSort::Name => b.name.cmp(&a.name),
                CharSort::Matches => a.matches.cmp(&b.matches),
                CharSort::WinRate => a.win_rate().unwrap_or(-1.0).partial_cmp(&b.win_rate().unwrap_or(-1.0)).unwrap(),
                CharSort::Data => a.data_s.partial_cmp(&b.data_s).unwrap(),
                CharSort::Dealt => (a.dealt / a.matches.max(1) as f64)
                    .partial_cmp(&(b.dealt / b.matches.max(1) as f64)).unwrap(),
            };
            if self.sort_desc { o.reverse() } else { o }
        });
        let mut sort = self.char_sort;
        let mut desc = self.sort_desc;
        let mut header = |ui: &mut egui::Ui, label: &str, k: CharSort| {
            let arrow = if sort == k { if desc { " v" } else { " ^" } } else { "" };
            if ui.button(format!("{label}{arrow}")).clicked() {
                if sort == k {
                    desc = !desc;
                } else {
                    sort = k;
                    desc = true;
                }
            }
        };
        egui::Grid::new("chars").striped(true).show(ui, |ui| {
            ui.label("");
            header(ui, "Character", CharSort::Name);
            header(ui, "Matches", CharSort::Matches);
            ui.strong("W");
            ui.strong("L");
            header(ui, "Win %", CharSort::WinRate);
            header(ui, "Data (min on point)", CharSort::Data);
            header(ui, "Dealt / match", CharSort::Dealt);
            ui.strong("Taken / match");
            ui.strong("KO'd");
            ui.end_row();
            for c in &chars {
                self.small_icon(ui, &c.name);
                ui.label(display(&c.name));
                ui.label(c.matches.to_string());
                ui.label(c.wins.to_string());
                ui.label(c.losses.to_string());
                ui.label(c.win_rate().map(|r| format!("{:.0}%", r * 100.0)).unwrap_or("-".into()));
                let mins = c.data_s / 60.0;
                if mins < LOW_DATA_MIN {
                    ui.colored_label(egui::Color32::from_rgb(220, 90, 80), format!("{mins:.1}  needs data"));
                } else {
                    ui.label(format!("{mins:.1}"));
                }
                let n = c.matches.max(1) as f64;
                ui.label(format!("{:.2}", c.dealt / n));
                ui.label(format!("{:.2}", c.taken / n));
                ui.label(c.kod.to_string());
                ui.end_row();
            }
        });
        self.char_sort = sort;
        self.sort_desc = desc;
    }

    // ------------------------------------------------------------- combos
    /// Ask the Python parser for the current text, unless that is already
    /// parsed or being parsed.
    fn refresh_preview(&mut self, ctx: &egui::Context) {
        let key = (self.settings.combo_text.clone(), self.settings.combo_char.clone());
        {
            let p = self.preview.lock().unwrap();
            if p.key == key || p.busy {
                return;
            }
        }
        if let Some(t) = self.preview_edit {
            if t.elapsed() < Duration::from_millis(350) {
                ctx.request_repaint_after(Duration::from_millis(120));
                return;
            }
        }
        self.preview.lock().unwrap().busy = true;
        let shared = self.preview.clone();
        let python = self.settings.python.clone();
        let dir = self.dir();
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let out = std::process::Command::new(&python)
                .args(["-m", "ajbot.notation", &key.0, "--char", &key.1, "--json"])
                .current_dir(&dir)
                .stdin(std::process::Stdio::null())
                .output();
            let mut p = shared.lock().unwrap();
            p.busy = false;
            p.key = key;
            p.steps.clear();
            p.error = None;
            match out {
                Err(e) => p.error = Some(format!("could not run {python}: {e}")),
                Ok(o) => {
                    let text = String::from_utf8_lossy(&o.stdout);
                    match serde_json::from_str::<serde_json::Value>(text.trim()) {
                        Err(_) => {
                            let err = String::from_utf8_lossy(&o.stderr);
                            p.error = Some(err.lines().last().unwrap_or("no answer from the parser").to_string());
                        }
                        Ok(v) => {
                            if let Some(e) = v.get("error").and_then(|e| e.as_str()) {
                                p.error = Some(e.to_string());
                            }
                            for st in v.get("steps").and_then(|s| s.as_array()).into_iter().flatten() {
                                let s = |k: &str| st.get(k).and_then(|x| x.as_str()).map(String::from);
                                p.steps.push(PreviewStep {
                                    label: s("label").unwrap_or_default(),
                                    src: s("src").unwrap_or_default(),
                                    tag: s("tag"),
                                    note: s("note"),
                                    charge: st.get("hold_frames").and_then(|x| x.as_i64()),
                                });
                            }
                        }
                    }
                }
            }
            ctx.request_repaint();
        });
    }

    fn start_practice(&mut self) {
        let s = &self.settings;
        let text = s.combo_text.trim().to_string();
        if text.is_empty() {
            self.status = "type a combo first".into();
            return;
        }
        let mut args: Vec<String> = vec![
            "--char".into(), s.combo_char.clone(), "--notation".into(), text.clone(),
            "--max-attempts".into(), s.combo_attempts.to_string(),
            "--confirm".into(), s.combo_confirm.to_string(),
        ];
        if !s.combo_name.trim().is_empty() {
            args.extend(["--name".into(), s.combo_name.trim().to_string()]);
        }
        // Partner names -> menu ids is the Python side's job; pass names.
        let partners: Vec<String> = s.combo_partners.split(',').map(|x| x.trim().to_string())
            .filter(|x| !x.is_empty()).collect();
        if !partners.is_empty() {
            args.extend(["--partner-names".into(), partners.join(",")]);
        }
        let label = format!("Combo practice: {} - {}", s.combo_char, text);
        let r = if s.combo_on_box {
            args.extend(["--launch".into(), "--instance".into(), s.box_instance.to_string()]);
            Runner::start_remote(&s.box_host, &s.box_dir, &s.box_python, "bot/practice.py", &args,
                                 &[], &label)
        } else {
            args.extend(["--instance".into(), "0".into(), "--speed".into(), "1".into()]);
            Runner::start(&s.python, &self.dir(), "bot/practice.py", &args, &label)
        };
        match r {
            Ok(r) => {
                self.status = format!("running: {label}");
                self.runner = Some(r);
                self.show_console = true;
            }
            Err(e) => self.status = e,
        }
        self.save_settings();
    }

    /// Re-read this folder's saved combos in a thread; with `from_box`, first
    /// merge in the box's (only offered when combos run on the box).
    fn refresh_saved(&mut self, ctx: &egui::Context, from_box: bool) {
        {
            let mut sv = self.saved.lock().unwrap();
            if sv.busy {
                return;
            }
            sv.busy = true;
        }
        let shared = self.saved.clone();
        let file = self.dir().join("models").join("demo_routes.json");
        let (host, box_dir) = (self.settings.box_host.clone(), self.settings.box_dir.clone());
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let note = if from_box {
                Some(merge_box_routes(&file, &host, &box_dir).unwrap_or_else(|e| e))
            } else {
                None
            };
            let read = match std::fs::read_to_string(&file) {
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(vec![]),
                Err(e) => Err(e.to_string()),
                Ok(t) => read_routes(&t),
            };
            let mut sv = shared.lock().unwrap();
            sv.busy = false;
            sv.loaded = true;
            sv.note = note;
            sv.error = None;
            sv.routes.clear();
            match read {
                Err(e) => sv.error = Some(format!("models/demo_routes.json: {e}")),
                Ok(rs) => {
                    for r in rs {
                        let st = |k: &str| r.get(k).and_then(|x| x.as_str()).map(String::from);
                        let li = |k: &str| r.pointer(&format!("/learned/{k}")).and_then(|x| x.as_i64());
                        sv.routes.push(SavedRoute {
                            name: st("name").unwrap_or_default(),
                            char_: st("char").unwrap_or_default(),
                            notation: st("notation"),
                            steps: r.get("steps").and_then(|x| x.as_array()).map_or(0, |a| a.len()),
                            attempts: li("attempts").unwrap_or(0),
                            landed: li("landed").unwrap_or(0),
                            learned: r.pointer("/learned/adj").is_some(),
                            json: r.to_string(),
                        });
                    }
                    sv.routes.sort_by(|a, b| (&a.char_, &a.name).cmp(&(&b.char_, &b.name)));
                }
            }
            ctx.request_repaint();
        });
    }

    /// Play a saved combo with its learned timing, nothing tuned or saved
    /// (practice.py --watch). On this PC's game you see it happen.
    fn watch_saved(&mut self, r: &SavedRoute) {
        let s = &self.settings;
        let mut args: Vec<String> = vec![
            "--route-json".into(), r.json.clone(),
            "--watch".into(), s.combo_watch.to_string(),
        ];
        let partners: Vec<String> = s.combo_partners.split(',').map(|x| x.trim().to_string())
            .filter(|x| !x.is_empty()).collect();
        if !partners.is_empty() {
            args.extend(["--partner-names".into(), partners.join(",")]);
        }
        let label = format!("Combo watch: {} - {}", display(&r.char_), r.name);
        let res = if s.combo_on_box {
            args.extend(["--launch".into(), "--instance".into(), s.box_instance.to_string()]);
            Runner::start_remote(&s.box_host, &s.box_dir, &s.box_python, "bot/practice.py", &args,
                                 &[], &label)
        } else {
            args.extend(["--instance".into(), "0".into(), "--speed".into(), "1".into()]);
            Runner::start(&s.python, &self.dir(), "bot/practice.py", &args, &label)
        };
        match res {
            Ok(run) => {
                self.status = format!("running: {label}");
                self.runner = Some(run);
                self.show_console = true;
            }
            Err(e) => self.status = e,
        }
        self.save_settings();
    }

    fn ui_saved_combos(&mut self, ui: &mut egui::Ui, running: bool) {
        if !self.saved.lock().unwrap().loaded {
            self.refresh_saved(ui.ctx(), false);
        }
        let routes = self.saved.lock().unwrap().routes.clone();
        ui.strong("Saved combos");
        ui.horizontal(|ui| {
            let shown = self.saved_pick.as_deref()
                .and_then(|n| routes.iter().find(|r| r.name == n))
                .map(|r| format!("{} - {}", display(&r.char_), r.name))
                .unwrap_or_else(|| if routes.is_empty() { "(none saved yet)".into() }
                                   else { "pick a saved combo".into() });
            egui::ComboBox::from_id_salt("saved_combo")
                .selected_text(shown)
                .width(420.0)
                .height(400.0)
                .show_ui(ui, |ui| {
                    for r in &routes {
                        let rate = if r.attempts > 0 {
                            format!("  ({}/{} landed)", r.landed, r.attempts)
                        } else {
                            "  (not practised)".into()
                        };
                        let t = format!("{} - {}{}", display(&r.char_), r.name, rate);
                        ui.selectable_value(&mut self.saved_pick, Some(r.name.clone()), t);
                    }
                });
            let tip = if self.settings.combo_on_box {
                "Re-read this folder's saved combos, after copying in the box's"
            } else {
                "Re-read this folder's saved combos (models/demo_routes.json)"
            };
            if ui.button("↻").on_hover_text(tip).clicked() {
                let from_box = self.settings.combo_on_box;
                self.refresh_saved(ui.ctx(), from_box);
            }
            let pick = self.saved_pick.as_deref()
                .and_then(|n| routes.iter().find(|r| r.name == n)).cloned();
            ui.add_enabled_ui(pick.is_some(), |ui| {
                if ui.button("Load").on_hover_text(
                    "Put it in the form below: its character, notation and name, \
                     so you can read the steps or practise it again").clicked() {
                    if let Some(r) = &pick {
                        self.settings.combo_char = r.char_.clone();
                        self.settings.combo_name = r.name.clone();
                        if let Some(n) = &r.notation {
                            self.settings.combo_text = n.clone();
                            self.preview_edit = Some(Instant::now());
                        }
                        self.save_settings();
                    }
                }
            });
            ui.add_enabled_ui(pick.is_some() && !running, |ui| {
                if ui.button(egui::RichText::new("▶ Watch").strong()).on_hover_text(
                    "Play it with the timing practice learned - nothing is tuned or saved. \
                     Pick \"This PC's game\" under Run on to see it.").clicked() {
                    if let Some(r) = &pick {
                        self.watch_saved(r);
                    }
                }
            });
            ui.add(egui::DragValue::new(&mut self.settings.combo_watch).range(1..=100));
            ui.label("times");
        });
        let sv = self.saved.lock().unwrap();
        if let Some(e) = &sv.error {
            ui.colored_label(egui::Color32::from_rgb(230, 120, 90), e);
        }
        if let Some(n) = &sv.note {
            ui.label(egui::RichText::new(n).weak());
        }
        if let Some(r) = self.saved_pick.as_deref().and_then(|n| sv.routes.iter().find(|r| r.name == n)) {
            let mut t = format!("{} steps", r.steps);
            if let Some(n) = &r.notation {
                t += &format!(" · {n}");
            }
            if !r.learned {
                t += " · no learned timing yet (plays the default gaps)";
            }
            ui.label(egui::RichText::new(t).weak());
        }
    }

    fn ui_combos(&mut self, ui: &mut egui::Ui) {
        let running = self.runner.is_some() || self.native_recorder.is_some();
        ui.heading(egui::RichText::new("Combo tester").color(ORANGE));
        ui.label("Write a combo the way players do. The bot walks up to a standing dummy on the \
                  gym and repeats it, tuning the timing of each step, until it lands it.");
        ui.add_space(6.0);
        self.ui_saved_combos(ui, running);
        ui.add_space(10.0);
        egui::Grid::new("combo_form").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
            ui.label("Character");
            let shown = display(&self.settings.combo_char);
            egui::ComboBox::from_id_salt("combo_char")
                .selected_text(shown)
                .height(400.0)
                .show_ui(ui, |ui| {
                    for n in self.roster.clone() {
                        ui.selectable_value(&mut self.settings.combo_char, n.clone(), display(&n));
                    }
                });
            ui.end_row();
            ui.label("Combo");
            let r = ui.add(egui::TextEdit::multiline(&mut self.settings.combo_text)
                .desired_rows(2)
                .desired_width(f32::INFINITY)
                .font(egui::TextStyle::Monospace)
                .hint_text("AAA > Q1 > Dodge Cancel > AA > Q1 > Jump > Q1 > Q2"));
            if r.changed() {
                self.preview_edit = Some(Instant::now());
            }
            ui.end_row();
            ui.label("Tag partners").on_hover_text(
                "Who is on the team besides the combo's character. Tags in the combo \
                 (t.Todoroki.Q1) put their target on the team first.");
            ui.text_edit_singleline(&mut self.settings.combo_partners);
            ui.end_row();
            ui.label("Save as").on_hover_text(
                "Name the learned timing is kept under (models/demo_routes.json). \
                 Empty = the combo text itself.");
            ui.add(egui::TextEdit::singleline(&mut self.settings.combo_name)
                .hint_text("(the combo text)"));
            ui.end_row();
            ui.label("Run on");
            ui.horizontal(|ui| {
                ui.selectable_value(&mut self.settings.combo_on_box, true, "Box (headless, fast)");
                ui.selectable_value(&mut self.settings.combo_on_box, false, "This PC's game (watch it)");
            });
            ui.end_row();
            ui.label("Attempts");
            ui.horizontal(|ui| {
                ui.add(egui::DragValue::new(&mut self.settings.combo_attempts).range(5..=2000));
                ui.label("at most, then");
                ui.add(egui::DragValue::new(&mut self.settings.combo_confirm).range(1..=100));
                ui.label("repeats to confirm it");
            });
            ui.end_row();
        });

        self.refresh_preview(ui.ctx());
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            ui.add_enabled_ui(!running, |ui| {
                if ui.button(egui::RichText::new("▶ Practise").strong()).clicked() {
                    self.start_practice();
                }
            });
            if let Some(r) = self.runner.as_mut() {
                if r.label.starts_with("Combo ") {
                    let stopping = r.stopping();
                    if ui.add_enabled(!stopping, egui::Button::new("■ Stop")).clicked() {
                        r.request_stop();
                    }
                } else {
                    ui.label("(something else is running - stop it on the Play tab first)");
                }
            }
            if !self.status.is_empty() {
                ui.label(egui::RichText::new(&self.status).weak());
            }
        });
        ui.add_space(8.0);
        ui.columns(2, |cols| {
            let ui = &mut cols[0];
            ui.strong("What the bot will press");
            let p = self.preview.lock().unwrap();
            if let Some(e) = &p.error {
                ui.colored_label(egui::Color32::from_rgb(230, 120, 90), e);
            } else if p.busy || p.key.0 != self.settings.combo_text {
                ui.label(egui::RichText::new("reading...").weak());
            }
            egui::ScrollArea::vertical().id_salt("combo_steps").max_height(320.0).show(ui, |ui| {
                egui::Grid::new("combo_steps_grid").striped(true).num_columns(3).show(ui, |ui| {
                    for (k, st) in p.steps.iter().enumerate() {
                        ui.label(egui::RichText::new(format!("{:2}", k + 1)).monospace().weak());
                        let mut t = st.label.clone();
                        if let Some(tag) = &st.tag {
                            t = format!("Tag in {tag}");
                        }
                        if let Some(c) = st.charge {
                            t += &format!("  (charge {c}f)");
                        }
                        ui.label(egui::RichText::new(t).monospace().color(ORANGE));
                        let mut src = st.src.clone();
                        if let Some(n) = &st.note {
                            src += &format!("  - {n}");
                        }
                        ui.label(egui::RichText::new(src).weak());
                        ui.end_row();
                    }
                });
            });
            drop(p);
            let ui = &mut cols[1];
            ui.strong("Notation");
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
                    ui.label(egui::RichText::new(c).monospace().color(ORANGE));
                    ui.label(m);
                    ui.end_row();
                }
            });
            ui.add_space(4.0);
            ui.label(egui::RichText::new(
                "Steps go between > or -. Timing is not written: each gap starts from how a \
                 real player times that pair (else a guess) and practice tunes it. \
                 Plus Ultra needs gauge; the dummy stands still.").weak());
        });
    }

    fn ui_sidebar(&mut self, ui: &mut egui::Ui) {
        let frame = egui::Frame::new().fill(theme::SIDEBAR).inner_margin(egui::Margin::same(10));
        egui::Panel::left("nav").exact_size(196.0).resizable(false).frame(frame).show(ui, |ui| {
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                ui.add_space(6.0);
                ui.label(egui::RichText::new("AJBot").size(26.0).strong().color(ORANGE));
            });
            ui.horizontal(|ui| {
                ui.add_space(7.0);
                ui.label(egui::RichText::new("All's Justice bot").color(theme::MUTED).small());
            });
            ui.add_space(16.0);
            let problems = self.setup.lock().unwrap().problems();
            let mut pages = vec![
                (Tab::Setup, "🔧", "Setup", (problems > 0).then_some(theme::WARN)),
                (Tab::Play, "▶", "Play", None),
                (Tab::Combos, "⚔", "Combos", None),
                (Tab::Stats, "📊", "Statistics", None),
            ];
            if !self.servers.is_empty() {
                pages.push((Tab::Train, "🏆", "Train", None));
            }
            pages.push((Tab::Settings, "⚙", "Settings", None));
            for (tab, icon, label, badge) in pages {
                if theme::nav_button(ui, self.tab == tab, icon, label, badge).clicked() {
                    self.tab = tab;
                }
                ui.add_space(2.0);
            }
            ui.with_layout(egui::Layout::bottom_up(egui::Align::Min), |ui| {
                ui.add_space(4.0);
                if !self.status.is_empty() {
                    ui.add(egui::Label::new(egui::RichText::new(&self.status).color(theme::MUTED).small())
                        .wrap());
                }
                let running = self.runner.as_ref().map(|r| r.label.clone())
                    .or_else(|| self.native_recorder.as_ref().map(|_| "recording".to_string()));
                match running {
                    Some(l) => theme::pill(ui, &format!("● {l}"), theme::GOOD),
                    None => theme::pill(ui, "○ idle", theme::MUTED),
                };
                if problems > 0 && self.tab != Tab::Setup {
                    if ui.link(egui::RichText::new(format!("⚠ {problems} setup item(s) to fix"))
                        .color(theme::WARN).small()).clicked()
                    {
                        self.tab = Tab::Setup;
                    }
                }
            });
        });
    }

    fn ui_settings(&mut self, ui: &mut egui::Ui) {
        theme::page_title(ui, "Settings", "Where things are, and how the bot plays.");
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            theme::titled_card(ui, "Folders", |ui| {
                egui::Grid::new("settings_paths").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                    ui.label("AJBot folder");
                    ui.add(egui::TextEdit::singleline(&mut self.settings.ajbot_dir).desired_width(480.0));
                    ui.end_row();
                    ui.label("Python");
                    ui.add(egui::TextEdit::singleline(&mut self.settings.python).desired_width(480.0));
                    ui.end_row();
                    ui.label("Policy file");
                    ui.add(egui::TextEdit::singleline(&mut self.settings.model).desired_width(480.0));
                    ui.end_row();
                });
                ui.add_space(4.0);
                if self.dir().join("bot").join("play.py").exists() {
                    ui.label(egui::RichText::new(format!("found bot/play.py · {} characters · {} icons",
                        self.roster.len(), self.icons.len())).color(theme::MUTED));
                } else {
                    ui.colored_label(theme::BAD, "bot/play.py not found in that folder");
                }
            });
            ui.add_space(8.0);
            theme::titled_card(ui, "Game screens", |ui| {
                egui::Grid::new("settings_game").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                    ui.label("Character-select cursor starts on");
                    ui.text_edit_singleline(&mut self.settings.pick_start);
                    ui.end_row();
                    ui.label("Control-mode screen shows now")
                        .on_hover_text("The game remembers each row's Normal/Manual/Custom between \
                            matches and the bot remembers what it set. Only change this if you set \
                            the rows by hand - it applies to the next Start, then resets.");
                    ui.horizontal(|ui| {
                        let c = &mut self.settings.control_now;
                        ui.selectable_value(c, String::new(), "(bot remembers)");
                        ui.selectable_value(c, "normal".to_string(), "Normal");
                        ui.selectable_value(c, "manual".to_string(), "Manual");
                        ui.selectable_value(c, "custom".to_string(), "Custom");
                    });
                    ui.end_row();
                });
            });
            ui.add_space(8.0);
            theme::titled_card(ui, "Combo practice machine", |ui| {
                ui.label(egui::RichText::new(
                    "ssh host, AJBot folder and Python on the machine with the headless game copies \
                     (optional - combos can be practised on this PC).").color(theme::MUTED));
                egui::Grid::new("settings_box").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                    ui.label("ssh host");
                    ui.add(egui::TextEdit::singleline(&mut self.settings.box_host).desired_width(260.0)
                        .hint_text("user@host"));
                    ui.end_row();
                    ui.label("AJBot folder there");
                    ui.add(egui::TextEdit::singleline(&mut self.settings.box_dir).desired_width(360.0));
                    ui.end_row();
                    ui.label("Python there");
                    ui.add(egui::TextEdit::singleline(&mut self.settings.box_python).desired_width(360.0));
                    ui.end_row();
                    ui.label("game copy");
                    ui.add(egui::DragValue::new(&mut self.settings.box_instance).range(0..=9));
                    ui.end_row();
                });
            });
            ui.add_space(8.0);
            ui.label(egui::RichText::new(
                "Training servers and odd game paths live in ajbot_local.json (see ajbot_local.example.json).")
                .color(theme::MUTED).small());
            ui.add_space(6.0);
            if theme::primary_button(ui, "Save and reload").clicked() {
                self.save_settings();
                self.roster = load_roster(&self.dir());
                self.icons.clear();
                self.icons_loaded = false;
                self.stats.loaded_mtime = None;
                self.servers = train::servers(&self.dir());
                self.setup_refresh(ui.ctx());
            }
        });
    }
}

impl eframe::App for App {
    fn logic(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.ensure_icons(ctx);
        if self.last_refresh.elapsed() > Duration::from_secs(3) {
            self.stats.refresh(&self.dir());
            self.last_refresh = Instant::now();
        }
        if let Some(r) = self.runner.as_mut() {
            if let Some(done) = r.poll() {
                self.status = format!("{} {done}", r.label);
                r.push(format!("[gui] script {done}"));
                // Keep the final log visible until the next start.
                let log = r.log.clone();
                let stopped = r.stopping();
                self.runner = None;
                self.last_log = Some(log.clone());
                self.stats.loaded_mtime = None;
                if stopped {
                    self.jobs.clear();
                }
                if !self.next_job(Some(log)) && std::mem::take(&mut self.jobs_recheck) {
                    self.setup_refresh(ctx);
                    self.servers = train::servers(&self.dir());
                }
            }
            ctx.request_repaint_after(Duration::from_millis(200));
        }
        if let Some(rec) = self.native_recorder.as_mut() {
            rec.tick();
            if let record_control::RecorderState::Error(e) = &rec.state {
                self.status = e.clone();
                self.native_recorder = None;
                self.stats.loaded_mtime = None;
            }
            ctx.request_repaint_after(Duration::from_millis(200));
        }
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.ui_sidebar(ui);
        let has_log = self.runner.is_some() || self.native_recorder.is_some() || self.last_log.is_some();
        if has_log && self.show_console {
            egui::Panel::bottom("console")
                .resizable(true)
                .default_size(260.0)
                .min_size(90.0)
                .show(ui, |ui| self.ui_console(ui));
        } else if has_log {
            egui::Panel::bottom("console_min").show(ui, |ui| {
                ui.horizontal(|ui| {
                    let (txt, col) = self.rec_indicator();
                    ui.label(egui::RichText::new(txt).color(col).monospace());
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.small_button("show console").clicked() {
                            self.show_console = true;
                        }
                    });
                });
            });
        }
        let page = egui::Frame::new().fill(theme::PANEL).inner_margin(egui::Margin::symmetric(20, 14));
        egui::CentralPanel::default().frame(page).show(ui, |ui| match self.tab {
            Tab::Setup => self.ui_setup(ui),
            Tab::Train => self.ui_train(ui),
            Tab::Play => self.ui_play(ui),
            Tab::Combos => self.ui_combos(ui),
            Tab::Stats => self.ui_stats(ui),
            Tab::Settings => self.ui_settings(ui),
        });
    }

    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        self.save_settings();
        if let Some(rec) = self.native_recorder.as_mut() {
            // Just the 'X' opcode - the DLL closes/flushes its own file, no
            // process to wait on or kill.
            rec.stop();
        }
        if let Some(r) = self.runner.as_mut() {
            r.request_stop();
            let t = Instant::now();
            while t.elapsed() < Duration::from_secs(8) {
                if r.poll().is_some() {
                    return;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            r.kill();
        }
    }
}

fn main() -> eframe::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1280.0, 860.0])
            .with_title("AJBot"),
        ..Default::default()
    };
    eframe::run_native("AJBot", options, Box::new(|cc| Ok(Box::new(App::new(cc)))))
}

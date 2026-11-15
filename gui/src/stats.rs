//! Reads stats/matches.jsonl (one line per finished match, written by the
//! Python recorders) and aggregates it for the Statistics tab.

use serde_json::Value;
use std::collections::HashMap;
use std::path::Path;
use std::time::SystemTime;

#[derive(Clone, Default)]
pub struct Match {
    pub t: f64,
    pub mode: String,
    pub side: Option<String>,
    pub result: String,
    pub reason: String,
    pub duration_s: f64,
    pub team: Vec<String>,
    pub opponents: Vec<String>,
    pub dealt: f64,
    pub taken: f64,
    pub kos_dealt: i64,
    pub kos_taken: i64,
    pub per_char: HashMap<String, CharLine>,
    pub events: HashMap<String, f64>,
    pub model: String,
    pub backfill: bool,
    /// Our combos this match, by the game's rule (victim never left hitstun).
    pub combos: f64,
    pub combo_mean: f64,
    pub combo_max: f64,
    pub chains_2: f64,
    pub chains_3: f64,
    pub pu_finishers: f64,
}

#[derive(Clone, Default)]
pub struct CharLine {
    pub dealt: f64,
    pub taken: f64,
    pub point_s: f64,
    pub ko: bool,
}

#[derive(Clone, Default)]
pub struct CharStat {
    pub name: String,
    pub matches: u32,
    pub wins: u32,
    pub losses: u32,
    pub point_s: f64,
    pub dealt: f64,
    pub taken: f64,
    pub kod: u32,
    /// Time on point across EVERY session, decided or not (training mode,
    /// stopped runs). This is the "how much data does it have" number.
    pub data_s: f64,
}

impl CharStat {
    pub fn decided(&self) -> u32 {
        self.wins + self.losses
    }
    pub fn win_rate(&self) -> Option<f64> {
        let d = self.decided();
        if d == 0 { None } else { Some(self.wins as f64 / d as f64) }
    }
}

#[derive(Clone, Default)]
pub struct TeamStat {
    pub team: String,
    pub wins: u32,
    pub losses: u32,
}

#[derive(Default)]
pub struct Stats {
    pub matches: Vec<Match>,
    pub loaded_mtime: Option<SystemTime>,
    pub error: Option<String>,
}

pub fn is_decided(r: &str) -> bool {
    r == "win" || r == "loss"
}

fn strs(v: &Value, key: &str) -> Vec<String> {
    v.get(key)
        .and_then(|x| x.as_array())
        .map(|a| a.iter().filter_map(|s| s.as_str().map(str::to_string)).collect())
        .unwrap_or_default()
}

fn num(v: &Value, key: &str) -> f64 {
    v.get(key).and_then(|x| x.as_f64()).unwrap_or(0.0)
}

fn parse(v: &Value) -> Match {
    let mut team = strs(v, "team_roster");
    if team.is_empty() {
        team = strs(v, "team");
    }
    let mut opponents = strs(v, "opponents_roster");
    if opponents.is_empty() {
        opponents = strs(v, "opponents");
    }
    let mut per_char = HashMap::new();
    if let Some(obj) = v.get("per_char").and_then(|x| x.as_object()) {
        for (k, c) in obj {
            per_char.insert(k.clone(), CharLine {
                dealt: num(c, "dealt"),
                taken: num(c, "taken"),
                point_s: num(c, "point_s"),
                ko: c.get("ko").and_then(|x| x.as_bool()).unwrap_or(false),
            });
        }
    }
    let mut events = HashMap::new();
    if let Some(obj) = v.get("events").and_then(|x| x.as_object()) {
        for (k, n) in obj {
            events.insert(k.clone(), n.as_f64().unwrap_or(0.0));
        }
    }
    let cs = v.get("combo_stats").cloned().unwrap_or(Value::Null);
    Match {
        combos: num(&cs, "combos"),
        combo_mean: num(&cs, "mean_hits"),
        combo_max: num(&cs, "max_hits"),
        chains_2: num(&cs, "chains_2"),
        chains_3: num(&cs, "chains_3"),
        pu_finishers: num(&cs, "pu_finishers"),
        t: num(v, "t"),
        mode: v.get("mode").and_then(|x| x.as_str()).unwrap_or("?").to_string(),
        side: v.get("side").and_then(|x| x.as_str()).map(str::to_string),
        result: v.get("result").and_then(|x| x.as_str()).unwrap_or("?").to_string(),
        reason: v.get("reason").and_then(|x| x.as_str()).unwrap_or("").to_string(),
        duration_s: num(v, "duration_s"),
        team,
        opponents,
        dealt: num(v, "damage_dealt"),
        taken: num(v, "damage_taken"),
        kos_dealt: v.get("kos_dealt").and_then(|x| x.as_i64()).unwrap_or(0),
        kos_taken: v.get("kos_taken").and_then(|x| x.as_i64()).unwrap_or(0),
        per_char,
        events,
        model: v.get("model").and_then(|x| x.as_str()).unwrap_or("").to_string(),
        backfill: v.get("backfill").and_then(|x| x.as_bool()).unwrap_or(false),
    }
}

impl Stats {
    /// Reload if the file changed since the last load.
    pub fn refresh(&mut self, dir: &Path) {
        let path = dir.join("stats").join("matches.jsonl");
        let mtime = std::fs::metadata(&path).and_then(|m| m.modified()).ok();
        if mtime.is_some() && mtime == self.loaded_mtime {
            return;
        }
        match std::fs::read_to_string(&path) {
            Ok(text) => {
                self.matches = text
                    .lines()
                    .filter_map(|l| serde_json::from_str::<Value>(l).ok())
                    .map(|v| parse(&v))
                    .collect();
                self.matches.sort_by(|a, b| a.t.partial_cmp(&b.t).unwrap_or(std::cmp::Ordering::Equal));
                self.error = None;
            }
            Err(_) => {
                self.matches.clear();
                self.error = Some(format!("no stats yet ({} not found) - play a match, \
                    or run `python stats_backfill.py` to import old recordings", path.display()));
            }
        }
        self.loaded_mtime = mtime;
    }
}

#[derive(Clone, PartialEq)]
pub struct Filter {
    pub bot: bool,
    pub selfplay: bool,
    pub human: bool,
    pub backfill: bool,
}

impl Filter {
    pub fn keep(&self, m: &Match) -> bool {
        let mode_ok = match m.mode.as_str() {
            "bot" => self.bot,
            "selfplay" => self.selfplay,
            "human" => self.human,
            _ => true,
        };
        mode_ok && (self.backfill || !m.backfill)
    }
}

#[derive(Clone)]
pub struct Summary {
    pub total: usize,
    pub wins: u32,
    pub losses: u32,
    pub other: u32,
    pub play_s: f64,
    pub dealt: f64,
    pub taken: f64,
    pub kos_dealt: i64,
    pub kos_taken: i64,
    pub events: HashMap<String, f64>,
    /// Combo totals over matches that recorded combo stats.
    pub combo_matches: u32,
    pub combos: f64,
    pub combo_hits: f64,
    pub combo_best: f64,
    pub chains_2: f64,
    pub chains_3: f64,
    pub pu_finishers: f64,
    pub chars: Vec<CharStat>,
    pub opponents: Vec<CharStat>,
    pub teams: Vec<TeamStat>,
}

pub fn summarize(matches: &[Match], filter: &Filter) -> Summary {
    let mut s = Summary {
        total: 0, wins: 0, losses: 0, other: 0, play_s: 0.0, dealt: 0.0, taken: 0.0,
        kos_dealt: 0, kos_taken: 0, events: HashMap::new(),
        combo_matches: 0, combos: 0.0, combo_hits: 0.0, combo_best: 0.0,
        chains_2: 0.0, chains_3: 0.0, pu_finishers: 0.0,
        chars: vec![], opponents: vec![], teams: vec![],
    };
    let mut chars: HashMap<String, CharStat> = HashMap::new();
    let mut opps: HashMap<String, CharStat> = HashMap::new();
    let mut teams: HashMap<String, TeamStat> = HashMap::new();

    for m in matches.iter().filter(|m| filter.keep(m)) {
        s.total += 1;
        s.play_s += m.duration_s;
        let decided = is_decided(&m.result);
        let win = m.result == "win";
        match m.result.as_str() {
            "win" => s.wins += 1,
            "loss" => s.losses += 1,
            _ => s.other += 1,
        }
        // Per-match averages only make sense over decided matches: a long
        // training-mode session would otherwise inflate every one of them.
        if decided {
            s.dealt += m.dealt;
            s.taken += m.taken;
            s.kos_dealt += m.kos_dealt;
            s.kos_taken += m.kos_taken;
            for (k, v) in &m.events {
                *s.events.entry(k.clone()).or_default() += v;
            }
        }
        if m.combos > 0.0 {
            s.combo_matches += 1;
            s.combos += m.combos;
            s.combo_hits += m.combo_mean * m.combos;
            s.combo_best = s.combo_best.max(m.combo_max);
            s.chains_2 += m.chains_2;
            s.chains_3 += m.chains_3;
            s.pu_finishers += m.pu_finishers;
        }

        for name in &m.team {
            let c = chars.entry(name.clone()).or_insert_with(|| CharStat {
                name: name.clone(), ..Default::default()
            });
            if let Some(pc) = m.per_char.get(name) {
                c.data_s += pc.point_s;
            }
            if !decided {
                continue;
            }
            c.matches += 1;
            if win { c.wins += 1 } else { c.losses += 1 }
            if let Some(pc) = m.per_char.get(name) {
                c.point_s += pc.point_s;
                c.dealt += pc.dealt;
                c.taken += pc.taken;
                if pc.ko {
                    c.kod += 1;
                }
            }
        }
        if decided {
            for name in &m.opponents {
                let o = opps.entry(name.clone()).or_insert_with(|| CharStat {
                    name: name.clone(), ..Default::default()
                });
                o.matches += 1;
                if win { o.wins += 1 } else { o.losses += 1 }
            }
            if !m.team.is_empty() {
                let mut t = m.team.clone();
                t.sort();
                let key = t.join(" / ");
                let e = teams.entry(key.clone()).or_insert_with(|| TeamStat {
                    team: key, ..Default::default()
                });
                if win { e.wins += 1 } else { e.losses += 1 }
            }
        }
    }
    s.chars = chars.into_values().collect();
    s.opponents = opps.into_values().collect();
    s.teams = teams.into_values().collect();
    s.teams.sort_by(|a, b| (b.wins + b.losses).cmp(&(a.wins + a.losses)));
    s
}

/// Per-character data time (seconds on point) across ALL matches of any mode,
/// for the roster overlay: which characters the bot has barely played.
pub fn data_time(matches: &[Match]) -> HashMap<String, (f64, u32, u32)> {
    let mut out: HashMap<String, (f64, u32, u32)> = HashMap::new();
    for m in matches {
        for name in &m.team {
            let e = out.entry(name.clone()).or_default();
            if let Some(pc) = m.per_char.get(name) {
                e.0 += pc.point_s;
            }
            if m.result == "win" { e.1 += 1 }
            if m.result == "loss" { e.2 += 1 }
        }
    }
    out
}

//! Ties the ported dedupe/teams/Roster/MatchTracker pipeline to actual file
//! output, reproducing `record.py`'s per-tick JSON schema and match/summary
//! file layout natively. Owned by `lib.rs`'s recording state and driven
//! once per wall-clock-gated tick from `maybe_record_tick()`.
//!
//! Mirrors `record.py`'s main loop (see its `rec = {...}` construction and
//! `open_recording()`/`finish_match()`), reusing the same field names and
//! file-naming convention so `ajbot/dataset.py`/`train.py` need no changes.
//!
//! Known gap (not ported in this pass): `record.py` calls
//! `tracker.stalled(...)` when a `HookError` shows the battle Lua has gone
//! quiet (paused, or the player backed out to a menu mid-match), which can
//! end an abandoned live match after `ABANDON_S`. Inside the DLL there is no
//! equivalent "poll failed" signal - a match that never reaches its normal
//! WinDemo/KO end would stay "live" indefinitely instead. Fine for the
//! common case (matches that actually finish); worth revisiting before this
//! fully replaces record.py for unattended/long sessions.
#![allow(dead_code)]

use std::collections::HashSet;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;

use crate::dedupe::{dedupe, key_of, prune_stale, teams as dedupe_teams};
use crate::match_tracker::{MatchEvent, MatchSummary, MatchTracker, TrackerState};
use crate::roster::Roster;
use crate::sample::{monotonic_s, py_round, snapshot, unix_now, Record, Value};

fn value_to_json(v: &Value) -> serde_json::Value {
    match v {
        // record.py's _trim() rounds every float field to 3 places before
        // writing a character record (not applied to the top-level tick's
        // own scalar fields like `t`/`lever`, which stay full precision).
        Value::Num(x) => serde_json::json!(py_round(*x, 3)),
        Value::Str(s) => serde_json::json!(s),
    }
}

fn record_to_json(r: &Record) -> serde_json::Map<String, serde_json::Value> {
    let mut obj = serde_json::Map::new();
    for (k, v) in r.iter() {
        obj.insert(k.to_string(), value_to_json(v));
    }
    obj
}

fn summary_to_json(s: &MatchSummary) -> serde_json::Value {
    let mut m = serde_json::Map::new();
    m.insert("t".into(), serde_json::json!(s.t));
    m.insert("ended".into(), serde_json::json!(s.ended));
    m.insert("duration_s".into(), serde_json::json!(s.duration_s));
    m.insert("mode".into(), serde_json::json!(s.mode));
    m.insert("side".into(), serde_json::json!(s.side));
    m.insert("model".into(), serde_json::json!(s.model));
    m.insert("result".into(), serde_json::json!(s.result));
    m.insert("reason".into(), serde_json::json!(s.reason));
    m.insert("team".into(), serde_json::json!(s.team));
    m.insert("opponents".into(), serde_json::json!(s.opponents));
    m.insert("team_ids".into(), serde_json::json!(s.team_ids));
    m.insert("opponent_ids".into(), serde_json::json!(s.opponent_ids));
    m.insert("team_roster".into(), serde_json::json!(s.team_roster));
    m.insert("opponents_roster".into(), serde_json::json!(s.opponents_roster));
    let hp_left: serde_json::Map<_, _> =
        s.hp_left.iter().map(|(k, v)| (k.to_string(), serde_json::json!(v))).collect();
    m.insert("hp_left".into(), serde_json::Value::Object(hp_left));
    let hp_left_enemy: serde_json::Map<_, _> =
        s.hp_left_enemy.iter().map(|(k, v)| (k.to_string(), serde_json::json!(v))).collect();
    m.insert("hp_left_enemy".into(), serde_json::Value::Object(hp_left_enemy));
    m.insert("kos_taken".into(), serde_json::json!(s.kos_taken));
    m.insert("kos_dealt".into(), serde_json::json!(s.kos_dealt));
    // See MatchSummary::damage_is_int_zero's doc comment: Python's sum([])
    // is the int 0, not 0.0, when a match recorded no per-character stats.
    if s.damage_is_int_zero {
        m.insert("damage_dealt".into(), serde_json::json!(0));
        m.insert("damage_taken".into(), serde_json::json!(0));
    } else {
        m.insert("damage_dealt".into(), serde_json::json!(s.damage_dealt));
        m.insert("damage_taken".into(), serde_json::json!(s.damage_taken));
    }
    let per_char: serde_json::Map<_, _> = s
        .per_char
        .iter()
        .map(|(k, cs)| {
            (
                k.to_string(),
                serde_json::json!({
                    "dealt": cs.dealt, "taken": cs.taken,
                    "point_s": cs.point_s, "ko": cs.ko,
                }),
            )
        })
        .collect();
    m.insert("per_char".into(), serde_json::Value::Object(per_char));
    let events: serde_json::Map<_, _> =
        s.events.iter().map(|(k, v)| (k.to_string(), serde_json::json!(v))).collect();
    m.insert("events".into(), serde_json::Value::Object(events));
    let enemy_events: serde_json::Map<_, _> =
        s.enemy_events.iter().map(|(k, v)| (k.to_string(), serde_json::json!(v))).collect();
    m.insert("enemy_events".into(), serde_json::Value::Object(enemy_events));
    if let Some(rec) = &s.recording {
        m.insert("recording".into(), serde_json::json!(rec));
    }
    serde_json::Value::Object(m)
}

pub struct RecordSession {
    roster: Roster,
    tracker: MatchTracker,
    /// The path handed to the 'W' opcode: empty means auto-name under
    /// runs/<timestamp>.jsonl like record.py's default; non-empty is used
    /// as-is for the first match and rotated as "<stem>.m<N><suffix>" for
    /// later matches in the same session, matching record.py's
    /// open_recording().
    base_path: String,
    matches: u32,
    file: Option<std::fs::File>,
    current_path: String,
    pub ticks_written: u64,
}

impl RecordSession {
    pub fn new(base_path: String) -> Self {
        RecordSession {
            roster: Roster::default(),
            tracker: MatchTracker::new("human", None, None),
            base_path,
            matches: 0,
            file: None,
            current_path: String::new(),
            ticks_written: 0,
        }
    }

    pub fn current_path(&self) -> &str {
        &self.current_path
    }

    fn compute_path(&self) -> String {
        if self.base_path.is_empty() {
            return format!("runs/{}.jsonl", unix_now() as i64);
        }
        if self.matches <= 1 {
            return self.base_path.clone();
        }
        let p = Path::new(&self.base_path);
        let stem = p.file_stem().and_then(|s| s.to_str()).unwrap_or("out");
        let suffix = p.extension().and_then(|s| s.to_str()).map(|s| format!(".{s}")).unwrap_or_default();
        let name = format!("{stem}.m{}{suffix}", self.matches);
        match p.parent() {
            Some(parent) if !parent.as_os_str().is_empty() => parent.join(name).to_string_lossy().into_owned(),
            _ => name,
        }
    }

    fn open_new_file(&mut self) {
        self.matches += 1;
        let path = self.compute_path();
        if let Some(parent) = Path::new(&path).parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        self.file = OpenOptions::new().create(true).write(true).truncate(true).open(&path).ok();
        self.current_path = path.clone();
        self.tracker.recording = Some(path);
    }

    fn close_file(&mut self) {
        if let Some(mut f) = self.file.take() {
            let _ = f.flush();
        }
    }

    fn write_match_summary(&self, summary: &MatchSummary) {
        let line = summary_to_json(summary).to_string();
        let _ = std::fs::create_dir_all("stats");
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open("stats/matches.jsonl") {
            let _ = writeln!(f, "{line}");
        }
    }

    /// One tick: parse the DLL's raw per-character samples text, run it
    /// through the ported dedupe/teams/Roster/MatchTracker pipeline exactly
    /// as record.py does, handle match start/end (file rotation + summary),
    /// and on a live tick with a resolved point character, write one JSON
    /// line matching record.py's schema.
    pub fn tick(&mut self, samples_text: &str) {
        let now_mono = monotonic_s();
        let now_unix = unix_now();

        let mut snap = snapshot(samples_text);
        self.roster.observe(&snap, now_mono);
        // teams() must run before dedupe(): it stamps the "key" field into
        // snap that dedupe()'s output (and so the written chars) rely on -
        // see dedupe::teams's doc comment.
        let teams = dedupe_teams(&mut snap, 800.0, Some(&self.roster), now_mono);
        let deduped = dedupe(&snap);

        match self.tracker.update_teams(&deduped, &teams, Some(now_unix)) {
            Some(MatchEvent::Started) => {
                self.close_file();
                self.open_new_file();
            }
            Some(MatchEvent::Ended) => {
                if let Some(summary) = self.tracker.commit() {
                    self.write_match_summary(&summary);
                }
                self.close_file();
            }
            None => {}
        }

        if self.tracker.state != TrackerState::Live {
            return;
        }
        let Some(me) = teams.me.as_ref() else { return };
        let Some(me_key) = key_of(me).map(str::to_string) else { return };

        let mine_keys: Vec<String> = teams.mine.iter().filter_map(|r| key_of(r).map(str::to_string)).collect();
        let theirs_keys: Vec<String> = teams.theirs.iter().filter_map(|r| key_of(r).map(str::to_string)).collect();
        let enemy_key = teams.enemy.as_ref().and_then(|r| key_of(r)).map(str::to_string);

        let mut keep: HashSet<String> = HashSet::new();
        keep.insert(me_key.clone());
        if let Some(k) = &enemy_key {
            keep.insert(k.clone());
        }
        keep.extend(mine_keys.iter().cloned());
        keep.extend(theirs_keys.iter().cloned());
        let pruned = prune_stale(&deduped, &keep);

        let mut chars_json = serde_json::Map::new();
        for (k, r) in pruned.iter() {
            let mut obj = record_to_json(r);
            obj.insert("slot".into(), serde_json::json!(k));
            chars_json.insert(k.to_string(), serde_json::Value::Object(obj));
        }

        let sides = self.roster.sides();
        let action = me.text("cmd").map(serde_json::Value::from).unwrap_or(serde_json::Value::Null);
        // Unlike character records, lever's own float values are NOT run
        // through _trim()'s rounding in record.py - they are written at
        // full precision.
        let lever_val = |field: &str| match me.get(field) {
            Some(Value::Num(x)) => serde_json::json!(x),
            Some(Value::Str(s)) => serde_json::json!(s),
            None => serde_json::Value::Null,
        };
        let rec = serde_json::json!({
            "t": py_round(now_unix, 3),
            "chars": chars_json,
            "me": me_key,
            "enemy": enemy_key,
            "mine": mine_keys,
            "theirs": theirs_keys,
            "roster": {"mine": sides.mine, "theirs": sides.theirs},
            "action": action,
            "lever": [lever_val("lang"), lever_val("lscale"), lever_val("lon")],
        });

        if let Some(f) = self.file.as_mut() {
            if writeln!(f, "{rec}").is_ok() {
                self.ticks_written += 1;
            }
        }
    }
}

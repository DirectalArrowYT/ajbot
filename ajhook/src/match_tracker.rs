//! Match boundaries and per-match statistics.
//!
//! Ported from:
//! * `ajbot/match.py` - constants (30-46), `_short` (49-53), `_merge_forms`
//!   (56-68), `_roster_names` (71-78), `class MatchTracker` (81-346),
//!   `describe` (349-357).
//! * `ajbot/sampler.py` - `ACT_CLASSES`/`act_class` (539-577), `class Envelope` (705-726).
//! * `ajbot/combos.py` - `MIN_HIT`/`MAX_HIT`/`RISE_TICKS` (32-34), `class Health` (38-77).
//!
//! State machine: `Waiting -> Live -> Over -> Live -> ...` (`update` returns
//! `Started` on entering Live, `Ended` on entering Over). See match.py's
//! module docstring for the signals: intro pose (`AppearanceDemo`), victory
//! pose (`WinDemo`), a held team KO, the battle Lua going away (menu gap /
//! `stalled`), and the `STUCK_OVER_S` full-health fallback.
//!
//! Not ported: the file write in `commit()` (stats/matches.jsonl) - the tick/
//! file-writing layer serializes [`MatchSummary`] - and the `path`/`write`
//! constructor arguments that only configure that write.
#![allow(dead_code)] // pub API surface; wired into pump() separately

use std::borrow::Borrow;
use std::collections::{HashMap, HashSet};

use crate::dedupe::{key_of, Teams};
use crate::roster::{base_name, roster_name};
use crate::sample::{lstrip_digits, py_max, py_min, py_round, py_sum, unix_now, OrderedMap, Record, Value};

// ================================================================ constants

/// match.py:30
pub const ABANDON_S: f64 = 20.0;
/// match.py:31 - sampler runs (~6 s): an action older than this is stale.
pub const RECENT_ACT: f64 = 120.0;
/// match.py:32 - a team KO must hold this long (Rising fakes one briefly).
pub const KO_CONFIRM_S: f64 = 6.0;
/// match.py:40 - "over" held this long lets full-health-nobody-posing start a match.
pub const STUCK_OVER_S: f64 = 15.0;

/// match.py:41-46 `EVENT_FIELDS`: summary name -> record counter field, in
/// the Python dict's order (which is the summary's `events` key order).
pub const EVENT_FIELDS: [(&str, &str); 12] = [
    ("just_guards", "ev_jg"),
    ("just_steps", "ev_js"),
    ("counters", "ev_ctr"),
    ("guarded_hits", "ev_gok"),
    ("change_combos", "ev_tagc"),
    ("changes", "ev_swap"),
    ("escapes", "ev_esc"),
    ("dodges", "ev_dodge"),
    ("guard_breaks", "ev_gbrk"),
    ("plus_ultras", "ev_pu"),
    ("unblockables", "ev_ub"),
    ("hits_taken", "ev_hit"),
];

/// combos.py:32 - below this is float noise, not a hit.
pub const MIN_HIT: f64 = 0.0005;
/// combos.py:33 - above this is a KO or a match reset, not one hit.
pub const MAX_HIT: f64 = 0.5;
/// combos.py:34 - consecutive ticks above the floor before a rise is real.
pub const RISE_TICKS: u32 = 8;

// =============================================================== act_class

/// sampler.py:539-565 `ACT_CLASSES`: first matching prefix wins, in this order.
pub const ACT_CLASSES: [(&str, &[&str]); 14] = [
    ("recovered", &["DamageRecover", "DamageDownRecover", "DamageNearGroundRecover", "DamageRingOutAutoRecover"]),
    ("damage", &["Damage"]),
    ("guard", &["Guard", "AirGuard", "JustGuard"]),
    ("counter", &["AttackGuardCancel"]),
    ("unblock", &["AttackUnguardable"]),
    ("pu", &["AttackPlusUltra", "AttackSuperPlusUltra", "AttackBlowDownPlusUltra", "AttackExSuperPlusUltra"]),
    ("escape", &["AttackDamageEscape"]),
    ("assist", &["AttackSidekick", "AttackRescueSidekick", "AttackTagCombo", "Sidekick", "SummonSidekick", "Rescue"]),
    ("combo", &["AttackMelee", "AttackExMelee"]),
    ("rising", &["AttackAwaking", "Awaking"]),
    ("attack", &["Attack"]),
    ("dodge", &["Dodge", "AirDodge", "JustStep", "AwakingDodge"]),
    (
        "move",
        &["Run", "Dash", "Jump", "AirDash", "AirJump", "DashJump", "AirRun", "Chase", "SlideWalk", "FastFall",
          "AirFallDown", "AirRiseUp", "Land", "WallLand"],
    ),
    ("idle", &["Idle", "AirIdle", "None"]),
];

/// Port of sampler.py:569-577 `act_class(act)`: tactical class of an `act`
/// string; `"other"` for a missing/empty/non-str act or no prefix match.
pub fn act_class(act: Option<&Value>) -> &'static str {
    let act = match act {
        Some(Value::Str(s)) if !s.is_empty() => s,
        _ => return "other",
    };
    for (name, prefixes) in ACT_CLASSES {
        if prefixes.iter().any(|p| act.starts_with(p)) {
            return name;
        }
    }
    "other"
}

// ================================================================= Envelope

/// Port of sampler.py:705-726 `class Envelope`: per-(character, counter)
/// monotone deltas. Only rises above the highest value seen count, and a
/// jump of more than 5 is taken as a lua_State switch (0).
#[derive(Clone, Debug, Default)]
pub struct Envelope {
    top: HashMap<(String, String), f64>,
}

impl Envelope {
    pub fn new() -> Self {
        Self::default()
    }

    /// `delta(key, field, value)`. A missing or non-numeric value gives 0 and
    /// does not touch the envelope. The stored top uses Python's
    /// `max(top, value)` (NaN-order-sensitive), not `f64::max`.
    pub fn delta(&mut self, key: &str, field: &str, value: Option<&Value>) -> f64 {
        let Some(value) = value.and_then(Value::as_num) else { return 0.0 };
        let k = (key.to_string(), field.to_string());
        let top = self.top.get(&k).copied();
        self.top.insert(k, match top {
            None => value,
            Some(t) => py_max(t, value),
        });
        let Some(top) = top else { return 0.0 };
        if value <= top {
            return 0.0;
        }
        let d = value - top;
        if d <= 5.0 { d } else { 0.0 }
    }
}

// =================================================================== Health

/// Port of combos.py:38-77 `class Health`: damage on the monotone health
/// envelope, per character. Health is believed downward only; a rise must
/// hold for `rise_ticks` consecutive readings before the floor moves up.
#[derive(Clone, Debug)]
pub struct Health {
    pub rise_ticks: u32,
    floor: HashMap<String, f64>,
    up: HashMap<String, u32>,
}

impl Default for Health {
    fn default() -> Self {
        Self::new(RISE_TICKS)
    }
}

impl Health {
    pub fn new(rise_ticks: u32) -> Self {
        Self { rise_ticks, floor: HashMap::new(), up: HashMap::new() }
    }

    /// `drop(key, hp)`: damage taken this tick, 0 when not believed (first
    /// sighting, a rise, or a drop outside `(MIN_HIT, MAX_HIT)`).
    pub fn drop(&mut self, key: &str, hp: f64) -> f64 {
        let Some(&floor) = self.floor.get(key) else {
            self.floor.insert(key.to_string(), hp);
            return 0.0;
        };
        if hp <= floor {
            let d = floor - hp;
            self.floor.insert(key.to_string(), hp);
            self.up.insert(key.to_string(), 0);
            return if MIN_HIT < d && d < MAX_HIT { d } else { 0.0 };
        }
        let n = self.up.get(key).copied().unwrap_or(0) + 1;
        self.up.insert(key.to_string(), n);
        if n >= self.rise_ticks {
            self.floor.insert(key.to_string(), hp); // a real heal, or a new round
            self.up.insert(key.to_string(), 0);
        }
        0.0
    }
}

// ================================================================= helpers

/// Port of match.py:49-53 `_short(cid)`: `"Pl02Bakugo"` -> `"Bakugo"`;
/// unnamed keys, and a bare `"Pl02"` (empty remainder), stay as they are.
pub fn short(cid: &str) -> String {
    if let Some(rest) = cid.strip_prefix("Pl") {
        let s = lstrip_digits(rest);
        if !s.is_empty() {
            return s.to_string();
        }
    }
    cid.to_string()
}

/// Per-character stats (match.py:123-125 `_char`, and each `_merge_forms` entry).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct CharStats {
    pub dealt: f64,
    pub taken: f64,
    pub point_s: f64,
    pub ko: bool,
}

/// Port of match.py:56-68 `_merge_forms(chars)`: stats keyed by roster
/// name, alternate forms folded into their base. Rounds to 3 places after
/// EACH merge, as the Python does.
pub fn merge_forms(chars: &OrderedMap<CharStats>) -> OrderedMap<CharStats> {
    let mut out: OrderedMap<CharStats> = OrderedMap::new();
    for (k, c) in chars.iter() {
        let name = match base_name(k) {
            Some(n) => n.to_string(),
            None => short(k),
        };
        let m = out.get_or_insert_with(&name, CharStats::default);
        m.dealt = py_round(m.dealt + c.dealt, 3);
        m.taken = py_round(m.taken + c.taken, 3);
        m.point_s = py_round(m.point_s + c.point_s, 3);
        m.ko = m.ko || c.ko;
    }
    out
}

/// Port of match.py:71-78 `_roster_names(keys)`: distinct roster names, first-seen order.
pub fn roster_names<S: AsRef<str>>(keys: &[S]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for k in keys {
        if let Some(n) = roster_name(k.as_ref()) {
            if !out.iter().any(|o| o == n) {
                out.push(n.to_string());
            }
        }
    }
    out
}

// ============================================================ MatchTracker

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TrackerState {
    Waiting,
    Live,
    Over,
}

impl TrackerState {
    pub fn as_str(self) -> &'static str {
        match self {
            TrackerState::Waiting => "waiting",
            TrackerState::Live => "live",
            TrackerState::Over => "over",
        }
    }
}

/// `update()`'s return value: Python's `"started"` / `"ended"` (None = no event).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MatchEvent {
    Started,
    Ended,
}

/// The summary dict built by match.py:293-330 `_end`. Fields are in the
/// Python dict's key order so a derived serializer writes the same order.
///
/// Serialization notes for a byte-compatible writer:
/// * `damage_dealt`/`damage_taken` are the Python int `0` (JSON `0`, not
///   `0.0`) when the match recorded no per-character stats at all
///   (`chars` empty - `sum([])`). See `damage_is_int_zero`.
/// * `recording` is ABSENT from the Python dict unless `tracker.recording`
///   was set (it is added in `commit()`); it is `str(path)`.
/// * `side`/`model` are JSON `null` when `None`.
#[derive(Clone, Debug)]
pub struct MatchSummary {
    pub t: f64,
    pub ended: f64,
    pub duration_s: f64,
    pub mode: String,
    pub side: Option<String>,
    pub model: Option<String>,
    pub result: String,
    pub reason: String,
    pub team: Vec<String>,
    pub opponents: Vec<String>,
    pub team_ids: Vec<String>,
    pub opponent_ids: Vec<String>,
    pub team_roster: Vec<String>,
    pub opponents_roster: Vec<String>,
    pub hp_left: OrderedMap<f64>,
    pub hp_left_enemy: OrderedMap<f64>,
    pub kos_taken: i64,
    pub kos_dealt: i64,
    pub damage_dealt: f64,
    pub damage_taken: f64,
    pub per_char: OrderedMap<CharStats>,
    pub events: Vec<(&'static str, i64)>,
    pub enemy_events: Vec<(&'static str, i64)>,
    /// Added by `commit()` only when `recording` is set (match.py:339-340).
    pub recording: Option<String>,
    /// True when Python's `sum()` over an empty `chars` returned the int 0.
    pub damage_is_int_zero: bool,
}

/// Port of match.py:81-346 `class MatchTracker`. Feed it every tick; it
/// reports `Started` / `Ended` and builds a [`MatchSummary`] on the end.
///
/// `mode` is `"bot"`, `"selfplay"` or `"human"` (record.py uses `"human"`).
#[derive(Clone, Debug)]
pub struct MatchTracker {
    pub mode: String,
    pub side: Option<String>,
    pub model: Option<String>,
    pub state: TrackerState,
    pub summary: Option<MatchSummary>,
    menu_gap: bool,
    no_live_since: Option<f64>,
    over_since: Option<f64>,
    // ---- per-match state, cleared by reset() (match.py:103-121)
    pub t0: Option<f64>,
    t_last: Option<f64>,
    health: Health,
    env: Envelope,
    pub mine: HashSet<String>,
    pub theirs: HashSet<String>,
    pub hp: HashMap<String, f64>,
    /// Insertion-ordered: the summary sums and `per_char` follow it.
    pub chars: OrderedMap<CharStats>,
    pub events: [i64; 12],
    pub enemy_events: [i64; 12],
    pub kos: i64,
    pub kos_enemy: i64,
    down: HashSet<String>,
    alive: HashSet<String>,
    pub point: Option<String>,
    ko_since: HashMap<&'static str, f64>,
    pub winner: Option<String>,
    pub reason: Option<String>,
    /// Path of the current match's recording, set by the recorder after
    /// `Started` (reset to None on every start, as in Python).
    pub recording: Option<String>,
}

/// Python truthiness of an optional float used as `x or fallback`.
fn or_f(x: Option<f64>, fallback: f64) -> f64 {
    match x {
        Some(v) if v != 0.0 => v,
        _ => fallback,
    }
}

impl MatchTracker {
    /// match.py:88-100 `__init__(mode="bot", side=None, model=None, ...)`.
    pub fn new(mode: &str, side: Option<String>, model: Option<String>) -> Self {
        Self {
            mode: mode.to_string(),
            side,
            model,
            state: TrackerState::Waiting,
            summary: None,
            menu_gap: false,
            no_live_since: None,
            over_since: None,
            t0: None,
            t_last: None,
            health: Health::default(),
            env: Envelope::new(),
            mine: HashSet::new(),
            theirs: HashSet::new(),
            hp: HashMap::new(),
            chars: OrderedMap::new(),
            events: [0; 12],
            enemy_events: [0; 12],
            kos: 0,
            kos_enemy: 0,
            down: HashSet::new(),
            alive: HashSet::new(),
            point: None,
            ko_since: HashMap::new(),
            winner: None,
            reason: None,
            recording: None,
        }
    }

    /// match.py:103-121 `_reset()`.
    fn reset(&mut self) {
        self.t0 = None;
        self.t_last = None;
        self.health = Health::default();
        self.env = Envelope::new();
        self.mine.clear();
        self.theirs.clear();
        self.hp.clear();
        self.chars.clear();
        self.events = [0; 12];
        self.enemy_events = [0; 12];
        self.kos = 0;
        self.kos_enemy = 0;
        self.down.clear();
        self.alive.clear();
        self.point = None;
        self.ko_since.clear();
        self.winner = None;
        self.reason = None;
        self.recording = None;
    }

    /// match.py:123-125 `_char(key)`.
    fn char_mut(&mut self, key: &str) -> &mut CharStats {
        self.chars.get_or_insert_with(key, CharStats::default)
    }

    /// Port of match.py:128-263 `update(chars, mine, theirs, me=None, now=None)`.
    ///
    /// `chars` is the tick's `dedupe(snap)` output (key -> record); `mine` /
    /// `theirs` are key lists; `me` is our side's point key. `now` defaults
    /// to `time.time()`.
    pub fn update<R: Borrow<Record>>(
        &mut self,
        chars: &OrderedMap<R>,
        mine: &[String],
        theirs: &[String],
        me: Option<&str>,
        now: Option<f64>,
    ) -> Option<MatchEvent> {
        let now = now.unwrap_or_else(unix_now);
        // live = records with age <= 60, in chars order
        let live: Vec<(&str, &Record)> =
            chars.iter().map(|(k, r)| (k, r.borrow())).filter(|(_, r)| r.age() <= 60.0).collect();
        if live.is_empty() {
            self.no_live_since = Some(or_f(self.no_live_since, now));
            if now - self.no_live_since.unwrap() >= 2.0 && self.state != TrackerState::Live {
                self.menu_gap = true;
            }
        } else {
            self.no_live_since = None;
        }
        // `recent`: act of each live record whose act began within RECENT_ACT
        // runs (`(act_n or 0) <= RECENT_ACT`). A missing act is kept as None.
        let recent: Vec<(&str, Option<&Value>)> = live
            .iter()
            .filter(|(_, r)| r.num_or_falsy("act_n", 0.0) <= RECENT_ACT)
            .map(|(k, r)| (*k, r.get("act")))
            .collect();
        let act_is = |a: &Option<&Value>, s: &str| matches!(a, Some(Value::Str(x)) if x == s);

        if matches!(self.state, TrackerState::Waiting | TrackerState::Over) {
            let intro = recent.iter().any(|(_, a)| act_is(a, "AppearanceDemo"));
            let posing = recent.iter().any(|(_, a)| act_is(a, "WinDemo"));
            let both: Vec<&Record> = live
                .iter()
                .filter(|(k, _)| mine.iter().any(|m| m == k) || theirs.iter().any(|t| t == k))
                .map(|(_, r)| *r)
                .collect();
            // `(r.get("hp") or 0) >= 0.95`. JUDGMENT CALL: a str hp would
            // raise TypeError in Python; here it reads as 0.
            let full = both.len() >= 2 && both.iter().all(|r| r.num_or_falsy("hp", 0.0) >= 0.95);
            if mine.is_empty() || theirs.is_empty() || live.is_empty() {
                return None; // no live battle (menus, loading)
            }
            if self.state == TrackerState::Over {
                let stuck = self.over_since.is_some_and(|s| now - s >= STUCK_OVER_S);
                if !(intro || (self.menu_gap && !posing) || (stuck && full && !posing)) {
                    return None;
                }
            } else if posing && !intro {
                return None; // joined on a result screen: wait
            }
            self.menu_gap = false;
            self.over_since = None;
            self.reset();
            self.state = TrackerState::Live;
            self.t0 = Some(now);
            return Some(MatchEvent::Started);
        }

        if self.state != TrackerState::Live {
            return None;
        }

        let dt = match self.t_last {
            None => 0.0,
            Some(tl) => py_max(0.0, py_min(1.0, now - tl)),
        };
        self.t_last = Some(now);
        self.mine.extend(mine.iter().cloned());
        self.theirs.extend(theirs.iter().cloned());
        if let Some(me) = me.filter(|m| !m.is_empty()) {
            self.point = Some(me.to_string());
            self.char_mut(me).point_s += dt;
        }

        // damage, KOs, events - over ALL chars (not just live), in chars order
        for (k, r) in chars.iter() {
            let r: &Record = r.borrow();
            let Some(hp) = r.num("hp") else { continue };
            let ours = self.mine.contains(k);
            if !ours && !self.theirs.contains(k) {
                continue;
            }
            self.hp.insert(k.to_string(), hp);
            let d = self.health.drop(k, hp);
            if d > 0.0 {
                if ours {
                    self.char_mut(k).taken += d;
                } else if let Some(p) = self.point.clone().filter(|p| !p.is_empty()) {
                    self.char_mut(&p).dealt += d;
                }
            }
            let gone = r.truthy("dead") || hp <= 0.001;
            if gone && self.alive.contains(k) && !self.down.contains(k) {
                self.down.insert(k.to_string());
                if ours {
                    self.kos += 1;
                    self.char_mut(k).ko = true;
                } else {
                    self.kos_enemy += 1;
                }
            } else if hp > 0.2 {
                self.alive.insert(k.to_string());
                self.down.remove(k);
            } else if hp > 0.001 {
                self.down.remove(k); // back from a momentary zero
            }

            for (idx, (_, field)) in EVENT_FIELDS.iter().enumerate() {
                if r.contains_key(field) {
                    let dv = self.env.delta(k, field, r.get(field));
                    if dv != 0.0 {
                        // `int(dv)` truncates toward zero.
                        if ours {
                            self.events[idx] += dv as i64;
                        } else {
                            self.enemy_events[idx] += dv as i64;
                        }
                    }
                }
            }
        }

        // the end: victory pose, >= 10 s into the match
        let winners: Vec<&str> = recent.iter().filter(|(_, a)| act_is(a, "WinDemo")).map(|(k, _)| *k).collect();
        let t0 = self.t0.unwrap_or(now);
        if !winners.is_empty() && now - t0 >= 10.0 {
            let ours = winners.iter().filter(|k| self.mine.contains(**k)).count();
            let theirs_n = winners.iter().filter(|k| self.theirs.contains(**k)).count();
            if ours > 0 || theirs_n > 0 {
                let result = if ours >= theirs_n { "win" } else { "loss" };
                return Some(self.end(result, "victory pose", now));
            }
        }

        // Team KO fallback: >= 2 known-alive members all down, nobody on that
        // side acting (fresh act outside damage/idle/other), held KO_CONFIRM_S.
        for side in ["loss", "win"] {
            let keys = if side == "loss" { &self.mine } else { &self.theirs };
            let known: Vec<String> = keys.iter().filter(|k| self.alive.contains(*k)).cloned().collect();
            let acting = known.iter().any(|k| {
                let rec = chars.get(k).map(|r| r.borrow());
                let cls = act_class(rec.and_then(|r| r.get("act")));
                let age = rec.map_or(1e9, |r| r.age());
                !matches!(cls, "damage" | "idle" | "other") && age <= 60.0
            });
            if known.len() >= 2 && known.iter().all(|k| self.down.contains(k)) && !acting {
                let since = *self.ko_since.entry(side).or_insert(now);
                if now - since >= KO_CONFIRM_S {
                    return Some(self.end(side, "team KO", now));
                }
            } else {
                self.ko_since.remove(side);
            }
        }
        None
    }

    /// Port of match.py:265-270 `update_teams(snap_chars, teams, now=None)`.
    pub fn update_teams<R: Borrow<Record>>(
        &mut self,
        snap_chars: &OrderedMap<R>,
        teams: &Teams,
        now: Option<f64>,
    ) -> Option<MatchEvent> {
        let keys = |xs: &[Record]| -> Vec<String> {
            xs.iter().filter_map(|r| key_of(r).filter(|k| !k.is_empty()).map(str::to_string)).collect()
        };
        let mine = keys(&teams.mine);
        let theirs = keys(&teams.theirs);
        let me = teams.me.as_ref().and_then(|r| key_of(r)).map(str::to_string);
        self.update(snap_chars, &mine, &theirs, me.as_deref(), now)
    }

    /// match.py:272-276 `left_battle()`: the next battle is a new match.
    pub fn left_battle(&mut self) {
        if self.state != TrackerState::Live {
            self.menu_gap = true;
        }
    }

    /// match.py:278-284 `stalled(since_s, now=None)`: the battle Lua has been
    /// unreachable for `since_s` s. Ends a live match as abandoned after
    /// ABANDON_S. `now` falls back to `time.time()` when None or 0 (`now or ...`).
    pub fn stalled(&mut self, since_s: f64, now: Option<f64>) -> Option<MatchEvent> {
        if since_s >= 2.0 && self.state != TrackerState::Live {
            self.menu_gap = true;
        }
        if self.state == TrackerState::Live && since_s >= ABANDON_S {
            let now = or_f(now, 0.0);
            let now = if now != 0.0 { now } else { unix_now() };
            return Some(self.end("abandoned", "left the match", now));
        }
        None
    }

    /// match.py:286-290 `close(now=None)`: stopping mid-match ends it as 'stopped'.
    pub fn close(&mut self, now: Option<f64>) -> Option<MatchEvent> {
        if self.state == TrackerState::Live && self.t0.is_some() {
            let now = or_f(now, 0.0);
            let now = if now != 0.0 { now } else { unix_now() };
            return Some(self.end("stopped", "run stopped", now));
        }
        None
    }

    /// Port of match.py:293-330 `_end(result, reason, now)`.
    fn end(&mut self, result: &str, reason: &str, now: f64) -> MatchEvent {
        self.state = TrackerState::Over;
        self.over_since = Some(now);
        self.winner = Some(result.to_string());
        self.reason = Some(reason.to_string());
        let mut mine: Vec<String> =
            self.mine.iter().filter(|k| self.alive.contains(*k) || self.chars.contains_key(k)).cloned().collect();
        mine.sort(); // byte order == Python code-point order for UTF-8
        let mut theirs: Vec<String> = self.theirs.iter().filter(|k| self.alive.contains(*k)).cloned().collect();
        theirs.sort();

        let mut hp_left = OrderedMap::new();
        for k in &mine {
            hp_left.insert(short(k), py_round(self.hp.get(k).copied().unwrap_or(0.0), 3));
        }
        let mut hp_left_enemy = OrderedMap::new();
        for k in &theirs {
            hp_left_enemy.insert(short(k), py_round(self.hp.get(k).copied().unwrap_or(0.0), 3));
        }
        let mut own_chars: OrderedMap<CharStats> = OrderedMap::new();
        for (k, c) in self.chars.iter() {
            if self.mine.contains(k) {
                own_chars.insert(k, *c);
            }
        }
        let t0 = self.t0.unwrap_or(now);
        let named = |xs: &[i64; 12]| -> Vec<(&'static str, i64)> {
            EVENT_FIELDS.iter().zip(xs.iter()).map(|((n, _), v)| (*n, *v)).collect()
        };
        self.summary = Some(MatchSummary {
            t: py_round(or_f(self.t0, now), 3),
            ended: py_round(now, 3),
            duration_s: py_round(py_max(0.0, now - t0), 1),
            mode: self.mode.clone(),
            side: self.side.clone(),
            model: self.model.clone(),
            result: result.to_string(),
            reason: reason.to_string(),
            team: mine.iter().map(|k| short(k)).collect(),
            opponents: theirs.iter().map(|k| short(k)).collect(),
            team_roster: roster_names(&mine),
            opponents_roster: roster_names(&theirs),
            team_ids: mine,
            opponent_ids: theirs,
            hp_left,
            hp_left_enemy,
            kos_taken: self.kos,
            kos_dealt: self.kos_enemy,
            damage_dealt: py_round(py_sum(self.chars.values().map(|c| c.dealt)), 3),
            damage_taken: py_round(py_sum(self.chars.values().map(|c| c.taken)), 3),
            per_char: merge_forms(&own_chars),
            events: named(&self.events),
            enemy_events: named(&self.enemy_events),
            recording: None,
            damage_is_int_zero: self.chars.is_empty(),
        });
        MatchEvent::Ended
    }

    /// Port of match.py:332-346 `commit(extra=None)`, minus the file append:
    /// takes the finished summary (adding `recording` when set) and clears it.
    /// The caller serializes it to stats/matches.jsonl. `extra` is not ported
    /// (record.py never passes it).
    pub fn commit(&mut self) -> Option<MatchSummary> {
        let mut s = self.summary.take()?;
        if let Some(rec) = self.recording.as_ref().filter(|r| !r.is_empty()) {
            s.recording = Some(rec.clone());
        }
        Some(s)
    }
}

/// Port of match.py:349-357 `describe(s)`: one console line for a finished match.
pub fn describe(s: &MatchSummary) -> String {
    // `int(duration_s)` truncates; divmod on a non-negative int.
    let total = s.duration_s as i64;
    let (mins, secs) = (total.div_euclid(60), total.rem_euclid(60));
    let join = |xs: &[String]| if xs.is_empty() { "?".to_string() } else { xs.join("/") };
    format!(
        "MATCH {} ({}) {}:{:02}  {} vs {}  dealt {:.2} taken {:.2}  KOs {}-{}",
        s.result.to_uppercase(),
        s.reason,
        mins,
        secs,
        join(&s.team),
        join(&s.opponents),
        s.damage_dealt,
        s.damage_taken,
        s.kos_dealt,
        s.kos_taken
    )
}

//! Character-name database and the per-match line-up tracker.
//!
//! Two Python sources, one Rust module:
//!
//! * `ajbot/roster.py` - `DISPLAY_ORDER`, `INDEX`, `FORMS`, `base_name()`,
//!   `index_of()` (the name database). Also `ajbot/unknowns.py:33`
//!   `roster_name()`, which is just `base_name()`.
//! * `ajbot/sampler.py:729-963` - `class Roster` (vote accumulation,
//!   orientation, pins). Despite the file name, the `Roster` CLASS lives in
//!   sampler.py, not roster.py.
//!
//! SYNC POINT: `DISPLAY_ORDER` and `FORMS` below are a hand-maintained mirror
//! of `ajbot/roster.py`. When a character or form is added there, add it here
//! too, in the same position. The same convention is used for
//! `sampler.PROJ_NAMES`/`CHAR_MODE` vs. the RESIDENT Lua tables.
#![allow(dead_code)] // pub API surface; wired into pump() separately

use std::collections::HashMap;
use std::sync::OnceLock;

use crate::dedupe::dedupe;
use crate::sample::{lstrip_digits, py_max, py_min, rstrip_digits, OrderedMap, Snap};

// ============================================================ name database

/// roster.py:7-79 `DISPLAY_ORDER` - character-select grid order (file order
/// of DT_VsCharSelect). Do not sort.
pub const DISPLAY_ORDER: [&str; 71] = [
    "DekuFinal",          // key  0
    "DekuHalfNaked",      // key 66
    "Bakugo",             // key  1
    "Todoroki",           // key  3
    "Uraraka",            // key  2
    "Iida",               // key  4
    "Asui",               // key  5
    "Kirishima",          // key  7
    "Mineta",             // key 12
    "Yaoyorozu",          // key  9
    "Tokoyami",           // key 10
    "Kaminari",           // key  6
    "DLC2",               // key 13
    "Jiro",               // key  8
    "Ashido",             // key 11
    "Shoji",              // key 18
    "Ojiro",              // key 15
    "Sero",               // key 16
    "Hagakure",           // key 19
    "Sato",               // key 14
    "Koda",               // key 17
    "Monoma",             // key 63
    "Kendo",              // key 23
    "Tetsutetsu",         // key 24
    "DLC1",               // key 28
    "Mirio",              // key 25
    "Amajiki",            // key 26
    "Nejire",             // key 27
    "Hatsume",            // key 29
    "Yoarashi",           // key 43
    "Camie",              // key 45
    "Shishikura",         // key 44
    "ArmorAllMight",      // key 31
    "Eraserhead",         // key 32
    "DLC3",               // key 39
    "DLC4",               // key 65
    "Endeavor",           // key 34
    "Hawks",              // key 38
    "Jeanist",            // key 40
    "Edgeshot",           // key 41
    "Mirko",              // key 42
    "GranTorino",         // key 33
    "Nighteye",           // key 35
    "FatGum",             // key 37
    "GangOrca",           // key 36
    "ShigarakiFinal",     // key 47
    "AllForOne",          // key 48
    "AwakeAFO",           // key 49
    "AllForOneShigaraki", // key 67
    "DabiFinal",          // key 51
    "Toga",               // key 52
    "Twice",              // key 53
    "Compress",           // key 54
    "DLC5",               // key 55
    "Nomu",               // key 57
    "HighendNomu1",       // key 58
    "Chisaki",            // key 59
    "Rappa",              // key 60
    "Muscular",           // key 56
    "Stain",              // key 61
    "Nagan",              // key 64
    "Gentle",             // key 62
    "Deku",               // key 20
    "DekuShoot",          // key 21
    "DekuEri",            // key 22
    "AllMight",           // key 30
    "ShigarakiTrench",    // key 46
    "Dabi",               // key 50
    "Stripe",             // key 69
    "Shimura",            // key 70
    "DekuOverlay",        // key 71
];

/// roster.py:90-106 `FORMS` - alternate-form id (no `Pl<NN>` prefix) ->
/// roster entry. Order kept from the Python dict (it is only ever looked up).
pub const FORMS: [(&str, &str); 15] = [
    ("DabiFullBodyBurn", "DabiFinal"),
    ("ShigarakiHandArmor", "ShigarakiFinal"),
    ("Chisaki2", "Chisaki"),
    ("FatGumSkinny", "FatGum"),
    ("Chisaki3", "Chisaki"),
    ("StripeAtmosphere", "Stripe"),
    ("KirishimaUnbreakable", "Kirishima"),
    ("TokoyamiBaldur", "Tokoyami"),
    ("DarkShadow", "Tokoyami"),
    ("MuscularMax", "Muscular"),
    ("DekuFinal2", "DekuFinal"),
    ("DekuEriCarrier", "DekuEri"),
    ("AllForOneYouth", "AwakeAFO"),
    ("AllForOneBoy", "AwakeAFO"),
    ("LaBrava", "Gentle"),
];

/// roster.py:81 `INDEX = {cid: i for i, cid in enumerate(DISPLAY_ORDER)}`
pub fn index() -> &'static HashMap<&'static str, usize> {
    static INDEX: OnceLock<HashMap<&'static str, usize>> = OnceLock::new();
    INDEX.get_or_init(|| DISPLAY_ORDER.iter().enumerate().map(|(i, c)| (*c, i)).collect())
}

fn form_of(name: &str) -> Option<&'static str> {
    FORMS.iter().find(|(f, _)| *f == name).map(|(_, base)| *base)
}

/// Port of roster.py:109-129 `base_name(cid)`: the roster entry for a Lua id,
/// following alternate forms, or `None`.
///
/// `"Pl02Bakugo"` -> `"Bakugo"`; `"Pl247DabiFullBodyBurn"` -> `"DabiFinal"`;
/// `"Pl07KaminariState"` -> `"Kaminari"` (longest roster name the id starts
/// with; `"Pl02Bakugo#1"` resolves the same way). Unnamed ids -> `None`.
pub fn base_name(cid: &str) -> Option<&'static str> {
    let rest = cid.strip_prefix("Pl")?;
    let name = lstrip_digits(rest);
    if let Some((&n, _)) = index().get_key_value(name) {
        return Some(n);
    }
    if let Some(base) = form_of(name) {
        return Some(base);
    }
    // `for base in INDEX` iterates in DISPLAY_ORDER; a later entry only
    // wins when STRICTLY longer, so ties go to the earlier one.
    let mut best: Option<&'static str> = None;
    for base in DISPLAY_ORDER {
        if name.starts_with(base) && best.is_none_or(|b| base.len() > b.len()) {
            best = Some(base);
        }
    }
    best
}

/// Port of roster.py:132-139 `index_of()`: linear display index
/// (row = i / 6, col = i % 6). `Err` where Python raises KeyError.
pub fn index_of(character_id: &str) -> Result<usize, String> {
    index()
        .get(character_id)
        .copied()
        .ok_or_else(|| format!("unknown character {character_id:?}"))
}

/// Port of unknowns.py:33-39 `roster_name(cid)`, which is `base_name(cid)`.
pub fn roster_name(cid: &str) -> Option<&'static str> {
    base_name(cid)
}

// ================================================================== Roster

/// A side of the match. Python uses the strings `"mine"` / `"theirs"`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Side {
    Mine,
    Theirs,
}

impl Side {
    pub fn as_str(self) -> &'static str {
        match self {
            Side::Mine => "mine",
            Side::Theirs => "theirs",
        }
    }
}

/// `self.votes[cid]` = `{"theirs": n, "mine": n}`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Votes {
    pub theirs: u64,
    pub mine: u64,
}

impl Votes {
    pub fn total(&self) -> u64 {
        self.theirs + self.mine
    }
}

/// `Roster.sides()` result.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Sides {
    pub mine: Vec<String>,
    pub theirs: Vec<String>,
}

/// Port of sampler.py:729-963 `class Roster`. Tracks the current match's two
/// line-ups by majority vote over fresh sightings, with name pins and an
/// enemy-flag orientation estimate.
///
/// Time: the Python reads `time.monotonic()` inside `observe()` and
/// `recent()`. Here the caller passes `now_mono` (seconds, any fixed epoch -
/// only differences matter), so a recorded tick stream can be replayed
/// deterministically. [`crate::sample::monotonic_s`] is the live source.
#[derive(Clone, Debug)]
pub struct Roster {
    pub fresh_age: f64,
    pub reset_on_new_match: bool,
    pub min_votes: u64,
    pub min_confidence: f64,
    /// Insertion-ordered: `sides()`/`side_of()` report in first-seen order.
    pub votes: OrderedMap<Votes>,
    /// Insertion-ordered: `_pin_name_for` returns the FIRST pin that matches.
    pub pinned: OrderedMap<Side>,
    was_damaged: bool,
    /// Evidence for which enemy-flag value is "theirs": +1 = the usual
    /// reading, -1 = flipped. Clamped to [-50, 50].
    pub orient: f64,
    /// Both teams pinned by name: stale records that match no pin are dropped.
    pub exclusive: bool,
    /// Monotonic time each key was last seen fresh (benched partners stay on the team).
    pub last_fresh: HashMap<String, f64>,
}

impl Default for Roster {
    /// `Roster()` with the Python defaults (sampler.py:757-758):
    /// `fresh_age=300, reset_on_new_match=False, min_votes=20, min_confidence=0.6`.
    fn default() -> Self {
        Self::new(300.0, false, 20, 0.6)
    }
}

impl Roster {
    /// sampler.py:757-780 `__init__`.
    pub fn new(fresh_age: f64, reset_on_new_match: bool, min_votes: u64, min_confidence: f64) -> Self {
        Self {
            fresh_age,
            reset_on_new_match,
            min_votes,
            min_confidence,
            votes: OrderedMap::new(),
            pinned: OrderedMap::new(),
            was_damaged: false,
            orient: 0.0,
            exclusive: false,
            last_fresh: HashMap::new(),
        }
    }

    /// sampler.py:782-786 `recent(key, within=600.0)`: seen live within the
    /// last `within` seconds. Python's default `within` is [`RECENT_WITHIN_S`].
    pub fn recent(&self, key: &str, within: f64, now_mono: f64) -> bool {
        match self.last_fresh.get(key) {
            Some(&t) => now_mono - t <= within,
            None => false,
        }
    }

    /// sampler.py:788-790 `side_of` property: `{cid: side}` for every voted
    /// cid whose side is decided, in vote order.
    pub fn side_of(&self) -> OrderedMap<Side> {
        let mut out = OrderedMap::new();
        for cid in self.votes.keys() {
            if let Some(s) = self.side(cid, true) {
                out.insert(cid, s);
            }
        }
        out
    }

    /// sampler.py:792-795 `_looks_like_match_start(fresh)`: at least 4 fresh
    /// records, all at `hp >= 0.995` (a missing hp reads as 0).
    fn looks_like_match_start(fresh: &[(String, &crate::sample::Record)]) -> bool {
        fresh.len() >= 4 && fresh.iter().all(|(_, r)| r.num_or("hp", 0.0) >= 0.995)
    }

    /// sampler.py:797-820 `observe(snap)`: one vote per fresh (age <=
    /// `fresh_age`) dedupe'd character, for the side its enemy flag reads.
    pub fn observe(&mut self, snap: &Snap, now_mono: f64) {
        let deduped = dedupe(snap);
        let fresh: Vec<(String, &crate::sample::Record)> = deduped
            .iter()
            .filter(|(_, r)| r.age() <= self.fresh_age)
            .map(|(k, r)| (k.to_string(), *r))
            .collect();
        if fresh.is_empty() {
            return;
        }
        for (k, _) in &fresh {
            self.last_fresh.insert(k.clone(), now_mono);
        }

        if self.reset_on_new_match {
            if Self::looks_like_match_start(&fresh) {
                if self.was_damaged {
                    self.votes.clear();
                    self.was_damaged = false;
                }
            } else if fresh.iter().any(|(_, r)| r.num_or("hp", 1.0) < 0.95) {
                self.was_damaged = true;
            }
        }

        for (key, rec) in &fresh {
            let v = self.votes.get_or_insert_with(key, Votes::default);
            if rec.truthy("enemy") {
                v.theirs += 1;
            } else {
                v.mine += 1;
            }
        }
    }

    /// sampler.py:822-836 `assign(ids, side="mine")`: pin roster names to a side.
    pub fn assign<S: AsRef<str>>(&mut self, ids: &[S], side: Side) {
        for cid in ids {
            self.pinned.insert(cid.as_ref(), side);
        }
    }

    /// sampler.py:838-859 `_pin_name_for(cid)`: which pinned NAME this id
    /// matches (exact, `Pl<digits><name>`, or via `base_name`), or `None`.
    pub fn pin_name_for(&self, cid: &str) -> Option<String> {
        if self.pinned.contains_key(cid) {
            return Some(cid.to_string());
        }
        for name in self.pinned.keys() {
            // `cid.endswith(name) and cid[:-len(name)].rstrip(digits) == "Pl"`.
            // Python's `cid[:-0]` is "" (not the whole string) for an empty name.
            if cid.ends_with(name) {
                let prefix = if name.is_empty() { "" } else { &cid[..cid.len() - name.len()] };
                if rstrip_digits(prefix) == "Pl" {
                    return Some(name.to_string());
                }
            }
        }
        if let Some(base) = base_name(cid) {
            if self.pinned.contains_key(base) {
                return Some(base.to_string());
            }
        }
        None
    }

    /// sampler.py:861-863 `_pin_for(cid)`. An empty name is falsy in Python,
    /// hence the `is_empty` guard.
    fn pin_for(&self, cid: &str) -> Option<Side> {
        match self.pin_name_for(cid) {
            Some(name) if !name.is_empty() => self.pinned.get(&name).copied(),
            _ => None,
        }
    }

    /// sampler.py:865-872 `pin_of(cid)`: the pinned side, or `None`.
    pub fn pin_of(&self, cid: &str) -> Option<Side> {
        self.pin_for(cid)
    }

    /// sampler.py:874-877 `flipped`: evidence says enemy=0 is the opposing team.
    pub fn flipped(&self) -> bool {
        self.orient < -3.0
    }

    /// sampler.py:879-894 `side(cid, allow_pin=True)`: pin first, then the
    /// vote majority once it has `min_votes` votes and `min_confidence`.
    pub fn side(&self, cid: &str, allow_pin: bool) -> Option<Side> {
        if allow_pin {
            if let Some(pin) = self.pin_for(cid) {
                return Some(pin);
            }
        }
        let v = self.votes.get(cid)?;
        if v.total() < self.min_votes {
            return None;
        }
        if self.confidence(cid) < self.min_confidence {
            return None;
        }
        let mut flagged = v.theirs > v.mine;
        if self.flipped() {
            flagged = !flagged;
        }
        Some(if flagged { Side::Theirs } else { Side::Mine })
    }

    /// sampler.py:896-930 `observe_control(snap, stick=None, bot_is_ai=False)`:
    /// accumulate TEAM-level orientation evidence. `ai=1` fighters are the CPU
    /// (+0.05 * flag each); a non-AI fighter whose camera-relative lever angle
    /// is within 25 degrees of the bot's stick push is ours (-0.2 * flag).
    ///
    /// `stick` is `(x, y)`. Python computes `math.hypot`, which since 3.8 is
    /// CPython's own routine rather than libm's; the two can differ in the
    /// last bit, which only matters at exactly 0.5.
    pub fn observe_control(&mut self, snap: &Snap, stick: Option<(f64, f64)>, bot_is_ai: bool) {
        let mut want: Option<f64> = None;
        if let Some((sx, sy)) = stick {
            if sx.hypot(sy) > 0.5 {
                // math.degrees(x) == x * (180.0 / pi) in CPython.
                want = Some(sx.atan2(sy) * (180.0 / std::f64::consts::PI));
            }
        }
        for (_, rec) in dedupe(snap).iter() {
            if rec.age() > 30.0 || !rec.truthy("active") {
                continue;
            }
            let flag = if rec.truthy("enemy") { 1.0 } else { -1.0 };
            if rec.truthy("ai") && !bot_is_ai {
                self.orient += 0.05 * flag;
            } else if let Some(want) = want {
                if rec.truthy("lon") && !rec.truthy("ai") {
                    if let Some(lcam) = rec.num("lcam") {
                        // `float(rec.get("lscale") or 0.0) > 30`. JUDGMENT CALL:
                        // a str lscale would make Python raise; treated as 0.
                        let lscale = rec.num_or_falsy("lscale", 0.0);
                        // Python `%` is floored modulo: rem_euclid for a positive divisor.
                        let diff = ((lcam - want + 180.0).rem_euclid(360.0) - 180.0).abs();
                        if lscale > 30.0 && diff < 25.0 {
                            self.orient -= 0.2 * flag;
                        }
                    }
                }
            }
        }
        self.orient = py_max(-50.0, py_min(50.0, self.orient));
    }

    /// sampler.py:932-935 `confidence(cid)`: majority share of the votes, 0 if none.
    pub fn confidence(&self, cid: &str) -> f64 {
        let Some(v) = self.votes.get(cid) else { return 0.0 };
        let n = v.total();
        if n == 0 {
            return 0.0;
        }
        v.theirs.max(v.mine) as f64 / n as f64
    }

    /// sampler.py:937-943 `reset()`: forget the match's sightings. Pins and
    /// orientation survive (they belong to the session).
    pub fn reset(&mut self) {
        self.votes.clear();
        self.last_fresh.clear();
        self.was_damaged = false;
    }

    /// sampler.py:945-953 `sides()`: voted characters by decided side, in vote order.
    pub fn sides(&self) -> Sides {
        let mut out = Sides::default();
        for cid in self.votes.keys() {
            match self.side(cid, true) {
                Some(Side::Mine) => out.mine.push(cid.to_string()),
                Some(Side::Theirs) => out.theirs.push(cid.to_string()),
                None => {}
            }
        }
        out
    }
}

/// Default `within` for [`Roster::recent`] (sampler.py:782).
pub const RECENT_WITHIN_S: f64 = 600.0;

//! Per-tick character collapsing and team split.
//!
//! Ported from `ajbot/sampler.py`:
//! * `_rank`       sampler.py:691-702
//! * `dedupe`      sampler.py:623-664
//! * `STALE_AGE` / `prune_stale`  sampler.py:671-688
//! * `teams`       sampler.py:966-1080
//!
//! All of these are single-tick functions: they read one snapshot (plus, for
//! `teams`, the stateful [`Roster`]).
#![allow(dead_code)] // pub API surface; wired into pump() separately

use std::borrow::Borrow;
use std::cmp::Ordering;
use std::collections::HashSet;

use crate::roster::{Roster, Side, RECENT_WITHIN_S};
use crate::sample::{OrderedMap, Record, Snap, Value};

/// Default `max_age` of `teams()` (sampler.py:966).
pub const TEAMS_MAX_AGE: f64 = 800.0;

/// sampler.py:671 `STALE_AGE` (mirrors dataset.BENCH_AGE).
pub const STALE_AGE: f64 = 3000.0;

/// Port of sampler.py:691-702 `_rank(rec)`: lower is better when two states
/// claim one character. Freshness (`age`, default 1e9) first; a state whose
/// `act` is the literal `"None"` (an auxiliary state that never runs actions)
/// gets +50.
///
/// Python also adds 50 when `act` is present with value `None`. That can't
/// happen: `_parse` only ever stores a str for `act`.
pub fn rank(rec: &Record) -> f64 {
    let mut age = rec.age();
    if rec.text("act") == Some("None") {
        age += 50.0;
    }
    age
}

/// Port of sampler.py:623-664 `dedupe(snap)`, returning dedupe key -> index
/// into `snap` (insertion position), in the same order as the Python dict.
///
/// Order: all `id='?'` records first, keyed by their slot handle (in snap
/// order); then one entry per character id in first-seen order. States that
/// share an id are split by `bool(enemy)`: the `enemy=False` group (if any)
/// takes the plain id, the other gets `"<id>#1"`. Within a group the lowest
/// [`rank`] wins, and on a tie the first in snap order (Python's `min`).
pub fn dedupe_indices(snap: &Snap) -> OrderedMap<usize> {
    let mut by_id: OrderedMap<Vec<usize>> = OrderedMap::new();
    let mut out: OrderedMap<usize> = OrderedMap::new();
    for (i, (slot, rec)) in snap.iter().enumerate() {
        let cid = rec.text("id").unwrap_or("?");
        if cid == "?" {
            let replace = match out.get(slot) {
                None => true,
                Some(&cur) => rank(rec) < rank(snap.at(cur).1),
            };
            if replace {
                out.insert(slot, i);
            }
            continue;
        }
        by_id.get_or_insert_with(cid, Vec::new).push(i);
    }

    for (cid, entries) in by_id.iter() {
        // groups keyed by bool(enemy); `sorted(groups)` puts False first.
        let mut not_enemy: Vec<usize> = Vec::new();
        let mut enemy: Vec<usize> = Vec::new();
        for &i in entries {
            if snap.at(i).1.truthy("enemy") {
                enemy.push(i);
            } else {
                not_enemy.push(i);
            }
        }
        let groups: Vec<&Vec<usize>> = [&not_enemy, &enemy].into_iter().filter(|g| !g.is_empty()).collect();
        for (gi, group) in groups.into_iter().enumerate() {
            // `min(group, key=_rank)`: first strictly-smallest wins.
            let mut best = group[0];
            for &i in &group[1..] {
                if rank(snap.at(i).1) < rank(snap.at(best).1) {
                    best = i;
                }
            }
            let key = if gi == 0 { cid.to_string() } else { format!("{cid}#{gi}") };
            out.insert(key, best);
        }
    }
    out
}

/// Port of sampler.py:623-664 `dedupe(snap)`: dedupe key -> record, borrowing
/// from `snap`. Same records, same order, as the Python dict.
pub fn dedupe(snap: &Snap) -> OrderedMap<&Record> {
    let mut out = OrderedMap::new();
    for (key, &i) in dedupe_indices(snap).iter() {
        out.insert(key, snap.at(i).1);
    }
    out
}

/// Port of sampler.py:674-688 `prune_stale(chars, keep_keys)`: drop entries
/// that are neither named this tick (`keep_keys`) nor fresher than [`STALE_AGE`].
pub fn prune_stale<R: Borrow<Record> + Clone>(chars: &OrderedMap<R>, keep_keys: &HashSet<String>) -> OrderedMap<R> {
    let mut out = OrderedMap::new();
    for (k, r) in chars.iter() {
        if keep_keys.contains(k) || r.borrow().age() <= STALE_AGE {
            out.insert(k, r.clone());
        }
    }
    out
}

/// `teams()` result (sampler.py:1073-1080). Records are copies taken AFTER
/// the `key` stamp, so each one carries its dedupe key in `rec["key"]`.
#[derive(Clone, Debug, Default)]
pub struct Teams {
    /// Point fighter of our side: first live record of `mine`.
    pub me: Option<Record>,
    /// Point fighter of their side: first live record of `theirs`.
    pub enemy: Option<Record>,
    pub mine: Vec<Record>,
    pub theirs: Vec<Record>,
    pub mine_live: Vec<Record>,
    pub theirs_live: Vec<Record>,
}

/// Dedupe key stamped by `teams()`; `r.get("key")`.
pub fn key_of(rec: &Record) -> Option<&str> {
    rec.text("key")
}

/// Port of sampler.py:966-1080 `teams(snap, max_age=800, roster=None)`.
///
/// Like the Python, this MUTATES `snap`: every record that passes the
/// staleness check gets `rec["key"] = <dedupe key>`. In Python the records are
/// shared dict objects, so the stamp is visible to a later `dedupe(snap)` in
/// the same tick. record.py writes those `dedupe(snap)` records out, so the
/// recorded `chars` entries carry a `key` field. Callers that reproduce the
/// recording must call this BEFORE building the tick's `chars` from the snap.
///
/// `now_mono` stands in for the `time.monotonic()` read inside
/// `roster.recent(key)` (default `within` = 600 s).
pub fn teams(snap: &mut Snap, max_age: f64, roster: Option<&Roster>, now_mono: f64) -> Teams {
    let deduped = dedupe_indices(snap);

    // Mirror-match pin arbitration (sampler.py:985-1002): when several keys
    // match the same pinned name, only the one whose orientation-corrected
    // flag reads as ours keeps the pin (stable sort, 0 = reads as mine).
    let mut pin_blocked: HashSet<String> = HashSet::new();
    if let Some(roster) = roster {
        if !roster.pinned.is_empty() {
            let mut by_pin_name: OrderedMap<Vec<(String, usize)>> = OrderedMap::new();
            for (key, &i) in deduped.iter() {
                if let Some(name) = roster.pin_name_for(key) {
                    by_pin_name.get_or_insert_with(&name, Vec::new).push((key.to_string(), i));
                }
            }
            for (_, cands) in by_pin_name.iter() {
                if cands.len() <= 1 {
                    continue;
                }
                let reads_as_mine = |i: usize| -> u8 {
                    let mut flagged = snap.at(i).1.truthy("enemy");
                    if roster.flipped() {
                        flagged = !flagged;
                    }
                    if flagged { 1 } else { 0 }
                };
                let mut sorted: Vec<&(String, usize)> = cands.iter().collect();
                sorted.sort_by_key(|(_, i)| reads_as_mine(*i)); // stable, like list.sort
                for (key, _) in &sorted[1..] {
                    pin_blocked.insert(key.clone());
                }
            }
        }
    }

    let mut mine: Vec<usize> = Vec::new();
    let mut theirs: Vec<usize> = Vec::new();
    for (key, &i) in deduped.iter() {
        let age = snap.at(i).1.age();
        if age > max_age {
            match roster {
                Some(r) if r.recent(key, RECENT_WITHIN_S, now_mono) => {}
                _ => continue,
            }
        }
        snap.at_mut(i).1.insert("key", Value::Str(key.to_string()));
        if let Some(r) = roster {
            if r.exclusive && r.pin_of(key).is_none() && age > 60.0 {
                continue;
            }
        }
        let mut side = roster.and_then(|r| r.side(key, !pin_blocked.contains(key)));
        if side.is_none() {
            if roster.is_some_and(|r| !r.pinned.is_empty()) {
                side = Some(Side::Theirs);
            } else {
                let rec = snap.at(i).1;
                let v = roster.and_then(|r| r.votes.get(key));
                let flagged = match v {
                    Some(v) if v.total() > 0 => v.theirs > v.mine,
                    _ => {
                        let mut f = rec.truthy("enemy");
                        if roster.is_some_and(|r| r.flipped()) {
                            f = !f;
                        }
                        f
                    }
                };
                side = Some(if flagged { Side::Theirs } else { Side::Mine });
            }
        }
        if side == Some(Side::Theirs) {
            theirs.push(i);
        } else {
            mine.push(i);
        }
    }

    // Sort key (sampler.py:1066-1068): (stale, not active, sidekick, dead, age).
    let sort_key = |i: usize| -> (u8, u8, u8, u8, f64) {
        let r = snap.at(i).1;
        let age = r.age();
        (
            if age > max_age { 1 } else { 0 },
            if r.truthy("active") { 0 } else { 1 },
            if r.truthy("sk") { 1 } else { 0 },
            if r.truthy("dead") { 1 } else { 0 },
            age,
        )
    };
    let cmp = |a: &usize, b: &usize| -> Ordering {
        let (ka, kb) = (sort_key(*a), sort_key(*b));
        (ka.0, ka.1, ka.2, ka.3)
            .cmp(&(kb.0, kb.1, kb.2, kb.3))
            .then(ka.4.partial_cmp(&kb.4).unwrap_or(Ordering::Equal))
    };
    mine.sort_by(cmp); // stable, like list.sort
    theirs.sort_by(cmp);

    // `live` uses `r.get("age", 0)`: a record with NO age counts as live.
    let is_live = |i: &usize| snap.at(*i).1.num_or("age", 0.0) <= max_age;
    let recs = |xs: &[usize]| -> Vec<Record> { xs.iter().map(|&i| snap.at(i).1.clone()).collect() };
    let mine_live: Vec<usize> = mine.iter().copied().filter(is_live).collect();
    let theirs_live: Vec<usize> = theirs.iter().copied().filter(is_live).collect();

    Teams {
        me: mine_live.first().map(|&i| snap.at(i).1.clone()),
        enemy: theirs_live.first().map(|&i| snap.at(i).1.clone()),
        mine: recs(&mine),
        theirs: recs(&theirs),
        mine_live: recs(&mine_live),
        theirs_live: recs(&theirs_live),
    }
}

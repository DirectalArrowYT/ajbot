//! Per-character sample records, and the Python semantics the ported
//! recorder logic depends on.
//!
//! Ported from `ajbot/sampler.py` (`_TEXT`, `_parse`, `snapshot`). The Python
//! code keeps a record as a plain `dict` of field -> `float` | `str`, parsed
//! from the RESIDENT chunk's `"k=v k=v ..."` output, and reads it with
//! `rec.get(field)` everywhere. The port keeps that shape - an insertion-
//! ordered map of field -> [`Value`] - instead of a fixed struct, because:
//!
//! * `_parse` decides number-vs-text PER VALUE (`float(v)`, falling back to the
//!   raw string), not per field, and consumers test `isinstance(..., float)`;
//! * `teams()` stamps an extra `key` field into records, and the recorder
//!   writes every field it finds back out;
//! * Python dict order is observable downstream (dedupe tie-breaks, the order
//!   floats are summed in, JSON key order), so every map whose iteration order
//!   Python relies on is an [`OrderedMap`] here, never a `HashMap`.
//!
//! This module also holds small helpers that copy CPython's exact behaviour
//! (`float()`, `round()`, builtin `max`/`min`, `sum()`, `str.strip`,
//! `str.splitlines`). Rust's own versions differ on NaN, ties, underscores or
//! summation order.
#![allow(dead_code)] // pub API surface; wired into pump() separately

use std::collections::HashMap;
use std::sync::OnceLock;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

// --------------------------------------------------------------------- value

/// One field value. `sampler._parse` (sampler.py:590-603) produces exactly
/// these two kinds: a `float` when `float(v)` succeeds, else the raw `str`.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Num(f64),
    Str(String),
}

impl Value {
    /// Python `bool(v)`: a float is truthy unless it is `0.0`/`-0.0` (NaN IS
    /// truthy); a str is truthy unless empty.
    pub fn truthy(&self) -> bool {
        match self {
            Value::Num(x) => *x != 0.0,
            Value::Str(s) => !s.is_empty(),
        }
    }

    /// `isinstance(v, (int, float))`
    pub fn as_num(&self) -> Option<f64> {
        match self {
            Value::Num(x) => Some(*x),
            Value::Str(_) => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            Value::Num(_) => None,
        }
    }
}

// --------------------------------------------------------------- ordered map

/// A string-keyed map that iterates in insertion order, like a Python 3.7+
/// `dict`. Re-inserting an existing key replaces its value but keeps its
/// original position, the same as `d[k] = v` on an existing key.
#[derive(Clone, Debug)]
pub struct OrderedMap<V> {
    entries: Vec<(String, V)>,
    index: HashMap<String, usize>,
}

impl<V> Default for OrderedMap<V> {
    fn default() -> Self {
        Self { entries: Vec::new(), index: HashMap::new() }
    }
}

impl<V> OrderedMap<V> {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn len(&self) -> usize {
        self.entries.len()
    }
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
    pub fn contains_key(&self, k: &str) -> bool {
        self.index.contains_key(k)
    }
    pub fn get(&self, k: &str) -> Option<&V> {
        self.index.get(k).map(|&i| &self.entries[i].1)
    }
    pub fn get_mut(&mut self, k: &str) -> Option<&mut V> {
        match self.index.get(k) {
            Some(&i) => Some(&mut self.entries[i].1),
            None => None,
        }
    }
    /// `d[k] = v`
    pub fn insert(&mut self, k: impl Into<String>, v: V) {
        let k = k.into();
        match self.index.get(&k) {
            Some(&i) => self.entries[i].1 = v,
            None => {
                self.index.insert(k.clone(), self.entries.len());
                self.entries.push((k, v));
            }
        }
    }
    /// `d.setdefault(k, f())`, returning the stored value.
    pub fn get_or_insert_with(&mut self, k: &str, f: impl FnOnce() -> V) -> &mut V {
        let i = match self.index.get(k) {
            Some(&i) => i,
            None => {
                self.index.insert(k.to_string(), self.entries.len());
                self.entries.push((k.to_string(), f()));
                self.entries.len() - 1
            }
        };
        &mut self.entries[i].1
    }
    pub fn clear(&mut self) {
        self.entries.clear();
        self.index.clear();
    }
    pub fn iter(&self) -> impl Iterator<Item = (&str, &V)> {
        self.entries.iter().map(|(k, v)| (k.as_str(), v))
    }
    pub fn iter_mut(&mut self) -> impl Iterator<Item = (&str, &mut V)> {
        self.entries.iter_mut().map(|(k, v)| (k.as_str(), v))
    }
    pub fn keys(&self) -> impl Iterator<Item = &str> {
        self.entries.iter().map(|(k, _)| k.as_str())
    }
    pub fn values(&self) -> impl Iterator<Item = &V> {
        self.entries.iter().map(|(_, v)| v)
    }
    /// Entry at insertion position `i` (for callers holding dedupe indices).
    pub fn at(&self, i: usize) -> (&str, &V) {
        let (k, v) = &self.entries[i];
        (k.as_str(), v)
    }
    pub fn at_mut(&mut self, i: usize) -> (&str, &mut V) {
        let (k, v) = &mut self.entries[i];
        (k.as_str(), v)
    }
}

// -------------------------------------------------------------------- record

/// One character's fields: the Python `rec` dict.
pub type Record = OrderedMap<Value>;

/// Slot handle (`"c12"`) -> record: the Python `snap` dict from
/// `sampler.snapshot()`. `dedupe()` output has the same shape, keyed by dedupe key.
pub type Snap = OrderedMap<Record>;

/// Accessors that spell out the `rec.get(...)` patterns the Python uses.
impl OrderedMap<Value> {
    /// `isinstance(rec.get(k), (int, float))` -> the number.
    pub fn num(&self, k: &str) -> Option<f64> {
        self.get(k).and_then(Value::as_num)
    }
    /// `bool(rec.get(k))`, where a missing field is `None` (falsy).
    pub fn truthy(&self, k: &str) -> bool {
        self.get(k).is_some_and(Value::truthy)
    }
    /// `rec.get(k)` when it is a str.
    pub fn text(&self, k: &str) -> Option<&str> {
        self.get(k).and_then(Value::as_str)
    }
    /// `rec.get(k, default)` compared as a number.
    ///
    /// JUDGMENT CALL: the Python would raise TypeError comparing a str to a
    /// number. Fields read this way (`age`, `hp`, `act_n`, `lscale`) are
    /// always numbers (the DLL writes `age`; RESIDENT's `num()`/`bit()`
    /// only emit numbers), so a str is treated like a missing field instead.
    pub fn num_or(&self, k: &str, default: f64) -> f64 {
        match self.get(k) {
            Some(Value::Num(x)) => *x,
            _ => default,
        }
    }
    /// `rec.get(k) or default` compared as a number: a falsy value (missing,
    /// `0.0`) gives `default`. A str is treated as missing, as for [`Self::num_or`].
    pub fn num_or_falsy(&self, k: &str, default: f64) -> f64 {
        match self.get(k) {
            Some(Value::Num(x)) if *x != 0.0 => *x,
            _ => default,
        }
    }
    /// `rec.get("age", 1e9)`: the default every age read in sampler.py/match.py uses.
    pub fn age(&self) -> f64 {
        self.num_or("age", 1e9)
    }
}

// ------------------------------------------------------------------- parsing

/// sampler.py:529 `_TEXT` - the fields kept as text (with any
/// `Enum::` prefix stripped) instead of being tried as numbers.
pub const TEXT_FIELDS: [&str; 4] = ["id", "cmd", "atk", "act"];

/// Port of `sampler._parse(line)` (sampler.py:590-603): `"k=v k=v"` -> record.
///
/// Splits on single spaces exactly like `line.split(" ")`; tokens without `=`
/// are skipped; a repeated key overwrites the value in its first position
/// (dict semantics - relevant for `age=` the DLL appends).
pub fn parse_record(line: &str) -> Record {
    let mut rec = Record::new();
    for f in line.split(' ') {
        let Some((k, v)) = f.split_once('=') else { continue };
        if TEXT_FIELDS.contains(&k) {
            let v = match v.split_once("::") {
                Some((_, rest)) => rest,
                None => v,
            };
            rec.insert(k, Value::Str(v.to_string()));
        } else {
            match py_float(v) {
                Some(x) => rec.insert(k, Value::Num(x)),
                None => rec.insert(k, Value::Str(v.to_string())),
            }
        }
    }
    rec
}

/// Port of the parsing half of `sampler.snapshot(hook)` (sampler.py:606-620):
/// the DLL's `S` reply text -> slot -> record. Lines are
/// `"<slot> <fields> age=<n>"`; blank lines and `#` header lines are skipped.
pub fn snapshot(text: &str) -> Snap {
    let mut out = Snap::new();
    for line in py_splitlines(text) {
        let line = py_strip(line);
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        // `slot, _, rest = line.partition(" ")`
        let (slot, rest) = match line.split_once(' ') {
            Some((s, r)) => (s, r),
            None => (line, ""),
        };
        if !rest.is_empty() {
            out.insert(slot, parse_record(rest));
        }
    }
    out
}

// ------------------------------------------------------- CPython equivalents

/// `str.isspace()` for one char: Unicode White_Space plus U+001C..U+001F,
/// which CPython also counts as whitespace (Rust's `char::is_whitespace` does not).
fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `str.strip()` with no argument.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_isspace)
}

/// `str.splitlines()` (no keepends): splits on every line boundary Python
/// recognises, not just `\n`/`\r\n` like `str::lines`.
pub fn py_splitlines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut it = s.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        let brk = matches!(
            c,
            '\n' | '\r' | '\u{0b}' | '\u{0c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}'
                | '\u{2028}' | '\u{2029}'
        );
        if brk {
            out.push(&s[start..i]);
            let mut end = i + c.len_utf8();
            if c == '\r' {
                if let Some(&(j, '\n')) = it.peek() {
                    it.next();
                    end = j + 1;
                }
            }
            start = end;
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// Python `float(s)` for a str, or `None` where Python raises ValueError.
///
/// Matches CPython on: surrounding whitespace, `inf`/`infinity`/`nan` in
/// any case with an optional sign, and PEP 515 underscores (allowed only
/// between two digits). Known gap: CPython also accepts non-ASCII Unicode
/// decimal digits (e.g. Arabic-Indic); this rejects them. Lua's `tostring`
/// never produces those.
///
/// Whitespace here is Unicode White_Space (`char::is_whitespace`), NOT
/// [`py_strip`]'s set: `float()` does not strip U+001C..U+001F although
/// `str.strip()` does (verified against CPython 3.14).
pub fn py_float(s: &str) -> Option<f64> {
    let t = s.trim_matches(char::is_whitespace);
    if t.is_empty() || !t.is_ascii() {
        return None;
    }
    let owned;
    let t = if t.contains('_') {
        let b = t.as_bytes();
        for (i, &c) in b.iter().enumerate() {
            if c == b'_'
                && (i == 0 || i + 1 >= b.len() || !b[i - 1].is_ascii_digit() || !b[i + 1].is_ascii_digit())
            {
                return None;
            }
        }
        owned = t.replace('_', "");
        owned.as_str()
    } else {
        t
    };
    t.parse::<f64>().ok()
}

/// Python `round(x, ndigits)` for a float: correctly rounded to `ndigits`
/// decimals (round-half-even on the exact binary value), NaN/inf unchanged.
/// Rust's exact `{:.N}` formatting does the same decimal rounding; the
/// result is parsed back like CPython's `strtod`.
pub fn py_round(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{:.*}", ndigits, x).parse::<f64>().unwrap_or(x)
}

/// Builtin `max(a, b)`: keeps `a` unless `b > a` (so NaN handling follows
/// argument order, unlike `f64::max`).
pub fn py_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Builtin `min(a, b)`: keeps `a` unless `b < a`.
pub fn py_min(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// Builtin `sum(floats)` as CPython 3.12+ computes it (the project runs
/// 3.14): start at int `0`, add the first float (`0 + x0`, which turns
/// `-0.0` into `0.0`), then Neumaier-compensated summation over the rest,
/// adding the compensation at the end only when it is non-zero and finite.
///
/// JUDGMENT CALL: an EMPTY sum in Python is the int `0`, which JSON-encodes
/// as `0`, not `0.0`. This returns `0.0`; a writer that must be byte-identical
/// to `json.dumps` has to special-case it (see `MatchSummary`).
pub fn py_sum(xs: impl IntoIterator<Item = f64>) -> f64 {
    let mut it = xs.into_iter();
    let Some(x0) = it.next() else { return 0.0 };
    let mut f = 0.0 + x0;
    let mut c = 0.0f64;
    for x in it {
        let t = f + x;
        if f.abs() >= x.abs() {
            c += (f - t) + x;
        } else {
            c += (x - t) + f;
        }
        f = t;
    }
    if c != 0.0 && c.is_finite() {
        f += c;
    }
    f
}

/// Python `lstrip("0123456789")`.
pub fn lstrip_digits(s: &str) -> &str {
    s.trim_start_matches(|c: char| c.is_ascii_digit())
}

/// Python `rstrip("0123456789")`.
pub fn rstrip_digits(s: &str) -> &str {
    s.trim_end_matches(|c: char| c.is_ascii_digit())
}

// ---------------------------------------------------------------------- time

/// `time.monotonic()` stand-in: seconds since the first call in this process.
/// Only differences are ever used (Roster.last_fresh / recent()).
pub fn monotonic_s() -> f64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

/// `time.time()`: Unix seconds as a float.
pub fn unix_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

"""What each move is worth at a given distance, and how long it commits the
fighter - mined from the recordings, no hand-written frame data.

The policy used to have no idea how far a move reaches. It saw `dist` but
nothing said that Bakugo's TQ2 (ExUnique2) connects up close and whiffs from
across the stage, so it spammed it from anywhere. This table is that missing
knowledge, in two parts:

  reach    per (character, decision label), split by the distance it was
           started from (DIST_EDGES bins), three rates:
             hit   it damaged the opponent itself: the first damage after it
                   started came before we started another attack, within the
                   move's own window (`window`, learned per move - a jab lands
                   inside a second, a Plus Ultra cinematic 3-5 s later, a
                   cannon shot somewhere in between).
             good  it hit, OR it led to damage: within LED_WINDOW_S the
                   opponent lost more health than we did, whatever landed it.
                   Setups that never hurt anyone themselves (Yaoyorozu's Q2)
                   are judged by what they open up.
             safe  good, or at least we were not hit back within
                   SAFE_WINDOW_S. Pressure that whiffs but is never punished
                   (Bakugo's TQ1 up close) is low on hit, high on safe.
  duration per (character, act name): how long the act normally plays before
           control comes back (in the recording's own `act_n` units). An act
           with a long tail - Bakugo's TQ2 dash - is a commitment: the
           opponent gets a free punish for the rest of it.

Everything is smoothed toward the label's overall rate and then the
character's, so a move seen twice does not read as 0% or 100%.

    python -m ajbot.moveinfo              # writes models/moveinfo.json
    python -m ajbot.moveinfo --show Bakugo
"""
import argparse
import glob
import json
import math
import random
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import dataset as D, tickcache
from .roster import base_name, move_char
from .sampler import act_class

PATH = Path("models") / "moveinfo.json"

DIST_EDGES = [0.0, 100.0, 200.0, 350.0, 550.0, 800.0, 1200.0, 2000.0, 3000.0, 4500.0]   # last bin open
HIT_WINDOW_S = 1.0             # default / shortest per-move hit window
LAT_STEP_S = 0.5               # hit-latency histogram bucket
LAT_MAX_S = 5.0                # longest hit window a move can learn
NLAT = int(LAT_MAX_S / LAT_STEP_S) + 1   # last bucket = no hit of its own
WIN_COVER = 0.9                # a move's window covers this share of its hits
WIN_MIN_HITS = 8               # fewer hits than this: use the label's window
LED_WINDOW_S = 3.0             # "led to damage" horizon
SAFE_WINDOW_S = 1.5            # not hit back within this of the start = safe
MIN_HIT = 0.004                # health-fraction drop that counts as a hit
SHRINK = 6.0                   # pseudo-observations pulled from the parent rate

# Labels that are not attacks: no reach to learn.
NON_ATTACK = {"Guard", "JustGuard", "Dodge", "Dash", "DashForward", "DashBack",
              "Jump", "None", "Escape", "DamageEscape", "Awaking", "TagCombo",
              "ExtremeAction"}

# The labels the reach features report, in a fixed order. Unlisted labels
# (follow-ups, charges) simply have no feature.
REACH_LABELS = ["Melee", "ExMelee", "Unique1", "Unique2", "ExUnique1", "ExUnique2",
                "PlusUltra1", "Unguardable"]

# Acts that end a committed move and hand control back.
FREE_CLASSES = ("other",)

# Outcome code per attack start: bit 1 = led to damage, bit 0 = not hit back.
LED_BIT, UNPUNISHED_BIT = 2, 1


def bin_of(dist):
    for i in range(len(DIST_EDGES) - 1, -1, -1):
        if dist >= DIST_EDGES[i]:
            return i
    return 0


def _pct(vals, q):
    return float(np.percentile(vals, q)) if vals else None


def _zero_hist():
    """[dist bin][latency bucket][outcome code] counts."""
    return [[[0] * 4 for _ in range(NLAT)] for _ in range(len(DIST_EDGES))]


def _add_hist(dst, src):
    for b in range(len(DIST_EDGES)):
        for k in range(NLAT):
            for c in range(4):
                dst[b][k][c] += src[b][k][c]


def _partial(path):
    """One recording's contribution: {"reach": [[char, label, hist]],
    "dur": [[char, act, [values]]]} - cached per recording (ajbot.tickcache),
    so re-mining after new matches only reads the new ones."""
    from . import tickcache

    def compute(p):
        reach = defaultdict(_zero_hist)
        dur = defaultdict(list)
        try:
            ticks = D.load(p)
        except Exception:
            return {"reach": [], "dur": []}
        if len(ticks) >= 200:
            _one(ticks, reach, dur)
        return {"reach": [[c, l, h] for (c, l), h in reach.items()],
                "dur": [[c, a, v] for (c, a), v in dur.items()]}
    return tickcache.cached_fact(path, "moveinfo", compute, version=4)


def _window_of(hist):
    """Seconds that cover WIN_COVER of a move's own hits, or None if it has
    too few hits to say."""
    by_k = [sum(hist[b][k][c] for b in range(len(DIST_EDGES)) for c in range(4))
            for k in range(NLAT - 1)]
    tot = sum(by_k)
    if tot < WIN_MIN_HITS:
        return None
    cum = 0
    for k, x in enumerate(by_k):
        cum += x
        if cum >= WIN_COVER * tot:
            return max(HIT_WINDOW_S, (k + 1) * LAT_STEP_S)
    return LAT_MAX_S


def _summarise(hist, window):
    """Per-bin and total n / hit / good / safe counts at a given hit window."""
    kwin = int(math.ceil(window / LAT_STEP_S - 1e-9))
    nb = len(DIST_EDGES)
    node = {"n": 0, "hit": 0, "good": 0, "safe": 0, "window": window,
            "bins": [[0, 0] for _ in range(nb)], "gbins": [0] * nb, "sbins": [0] * nb}
    for b in range(nb):
        for k in range(NLAT):
            for c in range(4):
                x = hist[b][k][c]
                if not x:
                    continue
                hit = k < kwin and k < NLAT - 1
                good = hit or bool(c & LED_BIT)
                safe = good or bool(c & UNPUNISHED_BIT)
                node["bins"][b][0] += x
                node["bins"][b][1] += x * hit
                node["gbins"][b] += x * good
                node["sbins"][b] += x * safe
    node["n"] = sum(r[0] for r in node["bins"])
    node["hit"] = sum(r[1] for r in node["bins"])
    node["good"] = sum(node["gbins"])
    node["safe"] = sum(node["sbins"])
    return node


def mine(files, max_files=None, seed=0, verbose=True):
    """The table, from a list of recording paths. Each recording's counts are
    cached, so this is cheap after the first run; all recordings are used
    unless max_files limits it."""
    files = list(files)
    random.Random(seed).shuffle(files)
    if max_files:
        files = files[:max_files]
    reach = defaultdict(_zero_hist)
    dur = defaultdict(list)
    from concurrent.futures import ProcessPoolExecutor
    import os
    from .parallel import safe_worker_count
    sizes = tickcache.src_sizes(files)
    with ProcessPoolExecutor(max_workers=safe_worker_count(sizes, os.cpu_count() or 4)) as ex:
        for fi, part in enumerate(ex.map(_partial, files), 1):
            for c, l, h in part["reach"]:
                _add_hist(reach[(c, l)], h)
            for c, a, v in part["dur"]:
                dur[(c, a)].extend(v)
            if verbose and fi % 100 == 0:
                print(f"  moveinfo: {fi}/{len(files)} recordings", flush=True)
    by_label = defaultdict(_zero_hist)
    for (char, label), h in reach.items():
        _add_hist(by_label[label], h)
    out = {"chars": {}, "labels": {}, "global": {"n": 0, "hit": 0, "good": 0, "safe": 0}}
    lab_win = {}
    for label, h in by_label.items():
        lab_win[label] = _window_of(h) or HIT_WINDOW_S
        out["labels"][label] = _summarise(h, lab_win[label])
    for (char, label), h in reach.items():
        node = _summarise(h, _window_of(h) or lab_win[label])
        out["chars"].setdefault(char, {}).setdefault("labels", {})[label] = node
        for f in ("n", "hit", "good", "safe"):
            out["global"][f] += node[f]
    for (char, act), vals in dur.items():
        if len(vals) >= 5:
            out["chars"].setdefault(char, {}).setdefault("acts", {})[act] = {
                "n": len(vals), "p50": _pct(vals, 50), "p90": _pct(vals, 90)}
    out["files"] = len(files)
    return out


def _one(ticks, reach, dur):
    D.stabilize(ticks)
    ts = [t.get("t", i / 15.0) for i, t in enumerate(ticks)]
    prev = None
    last_act = {}
    labs = [None] * len(ticks)
    for i, t in enumerate(ticks):
        me = D._rec(t, "me")
        if me:
            # Duration of a whole act: the last act_n before the act changed
            # to something that is not an interruption (hitstun, guard).
            key = me.get("key")
            la = last_act.get(key)
            if la and la[0] != me.get("act"):
                nxt = act_class(me.get("act"))
                if nxt not in ("damage", "guard", "recovered") and la[0]:
                    ch = move_char(me, key)
                    if act_class(la[0]) in D.ATTACK_CLASSES:
                        dur[(ch, la[0])].append(la[1])
            if isinstance(me.get("act_n"), (int, float)):
                last_act[key] = (me.get("act"), me["act_n"])
        lab = D.label_of(prev, me)
        prev = me
        if lab and lab not in NON_ATTACK and not lab.startswith("ExUnique2Follow"):
            labs[i] = lab
    for i, lab in enumerate(labs):
        if not lab:
            continue
        me, en = D._rec(ticks[i], "me"), D._rec(ticks[i], "enemy")
        if not me or not en:
            continue
        d = me.get("dist")
        if not isinstance(d, (int, float)) or not isinstance(en.get("hp"), (int, float)):
            continue
        k, code = _outcome(ticks, ts, labs, i, me, en)
        reach[(move_char(me), lab)][bin_of(d)][k][code] += 1


def _outcome(ticks, ts, labs, i, me, en):
    """(latency bucket, outcome code) of the attack started at tick i.

    The latency bucket is when its OWN damage landed: the first time the
    opponent lost health, provided we had not started another attack before
    that (NLAT-1 = it never hurt them itself). The code says whether it led
    to damage (opponent lost more than we did within LED_WINDOW_S) and
    whether we escaped being hit back within SAFE_WINDOW_S."""
    ek, ehp0 = en.get("key"), en.get("hp")
    mk, mhp0 = me.get("key"), me.get("hp")
    mine_on = isinstance(mhp0, (int, float))
    lat = NLAT - 1
    first = False
    solo = True
    dealt = taken = 0.0
    hit_back = False
    horizon = max(LAT_MAX_S, LED_WINDOW_S, SAFE_WINDOW_S)
    for j in range(i + 1, len(ticks)):
        dt = ts[j] - ts[i]
        if dt > horizon:
            break
        if labs[j]:
            solo = False
        e2 = D._rec(ticks[j], "enemy")
        if e2 and e2.get("key") == ek and isinstance(e2.get("hp"), (int, float)):
            drop = ehp0 - e2["hp"]
            if drop > MIN_HIT:
                if not first:
                    first = True
                    if solo and dt <= LAT_MAX_S:
                        lat = min(int(dt / LAT_STEP_S), NLAT - 2)
                if dt <= LED_WINDOW_S:
                    dealt = max(dealt, drop)
        if mine_on:
            m2 = D._rec(ticks[j], "me")
            if not m2 or m2.get("key") != mk:
                mine_on = False            # tagged out: the move's owner left
            elif isinstance(m2.get("hp"), (int, float)):
                lost = mhp0 - m2["hp"]
                if lost > MIN_HIT:
                    if dt <= SAFE_WINDOW_S:
                        hit_back = True
                    if dt <= LED_WINDOW_S:
                        taken = max(taken, lost)
    code = (LED_BIT if dealt > MIN_HIT and dealt > taken else 0) \
        | (0 if hit_back else UNPUNISHED_BIT)
    return lat, code


def _bins_in(data):
    """Number of distance bins the saved table was built with, or None."""
    for lab in (data.get("labels") or {}).values():
        if lab and lab.get("bins"):
            return len(lab["bins"])
    return None


class MoveInfo:
    def __init__(self, data=None):
        data = data or {}
        nb = _bins_in(data)
        if nb is not None and nb != len(DIST_EDGES):
            # Built with another DIST_EDGES layout: its bins mean other
            # distances (and indexing them crashed every decision on
            # 2026-11-06). Run without it until it is rebuilt.
            print(f"  [moveinfo] table has {nb} distance bins, code has {len(DIST_EDGES)} - "
                  "ignored until rebuilt (python -m ajbot.moveinfo)", flush=True)
            data = {}
        self.data = data
        g = self.data.get("global") or {}
        n = g.get("n", 0)
        self.base = (g.get("hit", 0) + 1.0) / (n + 3.0) if n else 0.3
        # Tables built before good/safe existed: those rates read as None and
        # callers fall back to the hit rate.
        self.good_base = (g["good"] + 1.0) / (n + 2.0) if n and "good" in g else None
        self.safe_base = (g["safe"] + 2.0) / (n + 3.0) if n and "safe" in g else None
        self.digest = "none" if not data else "%d-%d" % (n, self.data.get("files", 0))

    def __bool__(self):
        return bool(self.data.get("chars"))

    def _nodes(self, char, label):
        lab_g = (self.data.get("labels") or {}).get(label)
        node = ((self.data.get("chars") or {}).get(char, {}).get("labels") or {}).get(label)
        return lab_g, node

    def _p(self, char, label, dist, total, per_bin, base):
        """Shrunk rate of one outcome: label overall -> label in this distance
        bin -> character overall -> character in this bin."""
        if not self.data or base is None or not isinstance(dist, (int, float)):
            return None
        lab_g, node = self._nodes(char, label)
        if not lab_g and not node:
            return None
        b = bin_of(dist)

        def rate(nd, parent):
            p = (nd.get(total, 0) + SHRINK * parent) / (nd["n"] + SHRINK)
            bins = nd.get(per_bin)
            if bins:
                k = bins[b][1] if per_bin == "bins" else bins[b]
                p = (k + SHRINK * p) / (nd["bins"][b][0] + SHRINK)
            return p
        p_g = rate(lab_g, base) if lab_g else base
        return rate(node, p_g) if node else p_g

    def p_hit(self, char, label, dist):
        """Chance `label` damages the opponent itself when started from `dist`
        (within its own window), or None with no data at all for that label."""
        return self._p(char, label, dist, "hit", "bins", self.base)

    def p_good(self, char, label, dist):
        """Chance it hits OR leads to us out-damaging them within
        LED_WINDOW_S. None without data."""
        return self._p(char, label, dist, "good", "gbins", self.good_base)

    def p_safe(self, char, label, dist):
        """Chance it is good, or at least not punished within SAFE_WINDOW_S.
        None without data."""
        return self._p(char, label, dist, "safe", "sbins", self.safe_base)

    def window(self, char, label):
        """Seconds after the start within which this move's own hits land."""
        lab_g, node = self._nodes(char, label)
        for nd in (node, lab_g):
            if nd and nd.get("window"):
                return float(nd["window"])
        return HIT_WINDOW_S

    def worth(self, char, label, dist, led_credit=0.5, safe_credit=0.5):
        """How good a move is from here, 0..1: its hit chance, plus
        `led_credit` of the times it set up damage without landing itself,
        plus `safe_credit` of the times it did neither but was not punished.
        Falls back to p_hit on old tables; None with no data."""
        ph = self.p_hit(char, label, dist)
        if ph is None:
            return None
        pg = self.p_good(char, label, dist)
        if pg is None:
            return ph
        pg = max(pg, ph)
        out = ph + led_credit * (pg - ph)
        ps = self.p_safe(char, label, dist)
        if ps is not None:
            out += safe_credit * max(0.0, ps - pg)
        return out

    def act_len(self, char, act):
        """(p50, p90) of how long the act plays, in act_n units, or None."""
        a = ((self.data.get("chars") or {}).get(char, {}).get("acts") or {}).get(act)
        return (a["p50"], a["p90"]) if a and a.get("p50") else None

    def busy(self, char, act, act_n):
        """0..1.5: how far through its usual run this act is. 0 with no data."""
        ln = self.act_len(char, act)
        if not ln or not isinstance(act_n, (int, float)):
            return 0.0
        return float(min(1.5, act_n / max(ln[0], 1.0)))

    def reach_feats(self, char, dist):
        """p_hit for each of REACH_LABELS, 0 where unknown."""
        out = []
        for lab in REACH_LABELS:
            p = self.p_hit(char, lab, dist)
            out.append(0.0 if p is None else float(p))
        return out


_CACHE = {}     # path -> {"mtime", "obj"}: one per model folder in play


def get(path=None):
    """The table on disk (reloaded when the file changes), or an empty one.
    Default: the active model folder's copy (ajbot.datadir)."""
    from . import datadir
    p = Path(path) if path else datadir.path("moveinfo.json")
    c = _CACHE.setdefault(str(p), {"mtime": None, "obj": MoveInfo()})
    try:
        m = p.stat().st_mtime
    except OSError:
        return c["obj"] if c["mtime"] is None else MoveInfo()
    if c["mtime"] != m:
        try:
            c["obj"] = MoveInfo(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            c["obj"] = MoveInfo()
        c["mtime"] = m
    return c["obj"]


def show(m, char):
    labels = sorted(((m.data.get("chars") or {}).get(char, {}).get("labels") or {}).items(),
                    key=lambda kv: -kv[1]["n"])
    probe = [150, 450, 1000, 2500]
    print(f"{'':22} window   " + "   ".join(f"{'dist ' + str(d):>22}" for d in probe))
    print(f"{'':22}          " + "   ".join(f"{'hit  good safe worth':>22}" for _ in probe))
    for lab, row in labels:
        if row["n"] < 10:
            continue
        cells = []
        for d in probe:
            ph, pg, ps = m.p_hit(char, lab, d), m.p_good(char, lab, d), m.p_safe(char, lab, d)
            w = m.worth(char, lab, d)
            cells.append(" ".join("  - " if v is None else f"{v:.2f}" for v in (ph, pg, ps, w)))
        print(f"{lab:16} n={row['n']:4} {m.window(char, lab):4.1f}s   " + "   ".join(f"{c:>22}" for c in cells))
    for act, v in ((m.data.get("chars") or {}).get(char, {}).get("acts") or {}).items():
        print(f"  act {act:24} n={v['n']:4} p50={v['p50']:.0f} p90={v['p90']:.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=None)
    ap.add_argument("--max-files", type=int, default=0, help="0 = all (cheap: per-file counts are cached)")
    ap.add_argument("--show", default=None, help="print one character's table")
    a = ap.parse_args()
    if a.show:
        show(get(), a.show)
        return
    files = sorted(glob.glob(a.runs or "runs/*.jsonl")) + ([] if a.runs else sorted(glob.glob("botruns/*.jsonl")))
    print(f"mining {len(files)} recordings (up to {a.max_files})")
    out = mine(files, a.max_files or None)
    from . import datadir
    dest = datadir.out("moveinfo.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out), encoding="utf-8")
    g = out["global"]
    n = max(g["n"], 1)
    print(f"wrote {dest}: {g['n']} attack starts, {len(out['chars'])} characters, "
          f"hit {g['hit'] / n:.2f}  good {g['good'] / n:.2f}  safe {g['safe'] / n:.2f}")


if __name__ == "__main__":
    main()

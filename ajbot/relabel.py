"""Coach labels: rewrite a recording's clear mistakes as the right play.

Imitation learns nothing from the bot copying itself - a self-play match
only teaches what the bot already does. What makes human data worth more is
that it contains decisions the bot does not make. This module manufactures
those from the bot's own matches, using what we already know about every
move (framedata.json: startup / active / recovery / reach; defense.json:
which defensive answer has paid against which kind of attack):

  * missed defence - the opponent started an attack, it took long enough to
    land that a human reacts to it (REACT_S), we were free to act the whole
    time, and we did nothing defensive. The coach label, placed at the tick
    a human would have reacted, is the answer DefensePolicy rates best for
    that situation (Guard / JustGuard / CounterAttack / Dodge).

  * missed punish - the opponent's attack whiffed (or was guarded) and the
    game's own timings leave them stuck for longer than our quickest move
    that reaches needs, and we did not attack inside that window. The coach
    label is that move, at the tick the whiff became visible.

Output is a list of samples {i, label, kind, why} - tick indices into the
recording, so the trainer can featurize tick i exactly like any other
sample and add it with the coach label. Nothing here touches the training
code; see `coach()` and the CLI below.

    python -m ajbot.relabel botruns/*.jsonl          # summary
    python -m ajbot.relabel runs/*.jsonl --show 20   # the pro, as a control
"""

import collections
import glob
import sys

from . import dataset as D
from . import framedata
from .sampler import act_class
from .roster import base_name, toga_mode

# A human reliably reacts to a visible attack start in ~0.25-0.30 s; anything
# that landed faster than this is not a "should have blocked" - it was a
# mix-up or a frame trap, and the coach stays out of it.
REACT_S = 0.30
PERCEIVE_S = 0.15          # where the coach places the decision after the cue
INPUT_S = 0.08             # the press must go in at least this before contact
HIT_MIN = 0.005            # hp drop that counts as a hit
LOOK_S = 1.6               # longest an attack is followed waiting for contact
PUNISH_SLACK = 0.05        # our move must land this much inside their recovery
MIN_STARTUP = 0.05         # table startups under this are missing data

FREE = {"idle", "move", "other", "recovered"}         # can still do anything
ATTACKING = set(D.ATTACK_CLASSES) | {"rising"}
DEFENCE_ACTS = {"guard", "dodge", "counter", "escape"}

# DefensePolicy's options -> the action labels the model is trained on.
OPT_LABEL = {"guard": "Guard", "jgE": "JustGuard", "jgM": "JustGuard",
             "jgL": "JustGuard", "counter": "CounterAttack", "dodge": "Dodge"}

PUNISH_LABELS = ("Melee", "ExMelee", "Unique1", "Unique2", "ExUnique1",
                 "ExUnique2", "Unguardable")
TOGA_BLOOD_OK = ("Melee", "Unguardable")

# Why candidate events were dropped, for tuning (the CLI prints it).
REJECT = collections.Counter()


def _rej(why):
    REJECT[why] += 1


def _t(tick, i):
    v = tick.get("t")
    return float(v) if isinstance(v, (int, float)) else i / 15.0


def _hp(rec):
    h = (rec or {}).get("hp")
    return float(h) if isinstance(h, (int, float)) else None


def _started(cur, prev):
    """The record began a new act this tick."""
    if not cur or not cur.get("act"):
        return False
    if not prev or prev.get("act") != cur.get("act"):
        return True
    a0, a1 = prev.get("act_n"), cur.get("act_n")
    return isinstance(a0, (int, float)) and isinstance(a1, (int, float)) and a1 < a0


class _Defense:
    """Read-only view of DefensePolicy's table: best answer per situation."""

    def __init__(self):
        from .agent import DefensePolicy
        self.p = DefensePolicy(learn=False, epsilon=0.0)

    def answer(self, me, en):
        en_cls = act_class(en.get("act"))
        dist = float(me.get("dist") or en.get("dist") or 400.0)
        cls, _, _, _, _ = self.p._situation(me, en, en_cls, dist)
        ctx = self.p._ctx(cls, dist, _hp(me))
        best, bestv = None, -1e9
        for opt, lab in OPT_LABEL.items():
            v, _ = self.p._value(ctx, cls, opt)
            if v > bestv:
                best, bestv = lab, v
        if bestv <= 0:
            return None, cls      # the table says no defence pays here
        return best, cls


def _free(rec):
    return act_class(rec.get("act")) in FREE


def _missed_defence(ticks, s, ek, mk, dfn, out):
    """Opponent `ek` started an attack at tick s against our point `mk`."""
    t0 = _t(ticks[s], s)
    en0 = D._same(ticks[s], ek)
    me0 = D._same(ticks[s], mk)
    if not me0 or not _free(me0):
        return _rej('def: line121')
    act = en0.get("act")
    hp0 = _hp(me0)
    if hp0 is None:
        return _rej('def: line125')
    prev_me = me0
    for j in range(s + 1, len(ticks)):
        tj = _t(ticks[j], j)
        if tj - t0 > LOOK_S:
            return _rej('def: line130')
        me = D._same(ticks[j], mk)
        en = D._same(ticks[j], ek)
        if not me or not en:
            return _rej('def: line134')
        # We acted defensively (or started anything of our own) - not a miss.
        if act_class(me.get("act")) in DEFENCE_ACTS:
            return _rej('def: line137')
        lab = D.label_of(prev_me, me)
        if lab and lab not in ("Dash", "DashForward", "DashBack", "Jump"):
            return _rej('def: we committed to something; not passivity')  # we committed to something; not passivity
        prev_me = me
        h = _hp(me)
        if h is not None and hp0 - h >= HIT_MIN:
            delay = tj - t0
            if delay < REACT_S:
                return _rej('def: line146')
            if en.get("act") != act and act_class(en.get("act")) not in ATTACKING:
                return _rej('def: landed by something else')  # landed by something else
            # The coach's tick: a human's reaction after the cue, early
            # enough to still get the input in.
            want = t0 + PERCEIVE_S
            d = None
            for k in range(s + 1, j):
                tk = _t(ticks[k], k)
                if tk >= want and tk <= tj - INPUT_S:
                    d = k
                    break
            if d is None:
                return _rej('def: line159')
            med, ened = D._same(ticks[d], mk), D._same(ticks[d], ek)
            if not med or not _free(med):
                return _rej('def: line162')
            label, cls = dfn.answer(dict(med, key=mk), dict(ened, key=ek))
            if label:
                out.append({"i": d, "label": label, "kind": "defence",
                            "why": f"{base_name(ek) or ek} {act} ({cls}) landed "
                                   f"{delay:.2f}s after it started"})
            return _rej('def: line168')
        if act_class(en.get("act")) not in ATTACKING:
            return _rej('def: their attack ended without touching us')  # their attack ended without touching us


def _missed_punish(ticks, s, ek, mk, fd, out):
    """Opponent `ek` started an attack at tick s; did it whiff into a punish?"""
    en0 = D._same(ticks[s], ek)
    me0 = D._same(ticks[s], mk)
    p = fd.profile(dict(en0, key=ek)) if en0 else None
    if not p or p.get("startup") is None or not p.get("free"):
        return _rej('pun: line179')
    t0 = _t(ticks[s], s)
    act = en0.get("act")
    whiff_at = t0 + p["startup"] + (p.get("active") or 0.0)
    free_at = t0 + p["free"]
    hp_me0, hp_en0 = _hp(me0), _hp(en0)
    if hp_me0 is None or hp_en0 is None:
        return _rej('pun: line188')
    d = None
    for j in range(s + 1, len(ticks)):
        tj = _t(ticks[j], j)
        if tj > free_at:
            break
        me = D._same(ticks[j], mk)
        en = D._same(ticks[j], ek)
        if not me or not en:
            return _rej('pun: line197')
        if en.get("act") != act:
            return _rej('pun: cancelled / chained into something else')  # cancelled / chained into something else
        if (_hp(me) or 1.0) < hp_me0 - HIT_MIN:
            return _rej('pun: it hit us: not a whiff')  # it hit us: not a whiff
        if (_hp(en) or 1.0) < hp_en0 - HIT_MIN:
            return _rej('pun: we already hit them')  # we already hit them
        if d is None and tj >= whiff_at + PERCEIVE_S:
            d = j
            break
    if d is None:
        return _rej('pun: line208')
    me = D._same(ticks[d], mk)
    if not me or act_class(me.get("act")) not in FREE | {"guard"}:
        return _rej('pun: line211')
    # The window actually seen: from the cue until the opponent leaves this
    # attack or starts a new stage of it (a jab string keeps its act name,
    # so the act_n reset is what shows the next hit). Frame data alone said
    # "stuck 1.25 s" for strings that simply carried on.
    t_d, e = _t(ticks[d], d), None
    prev_en = D._same(ticks[d], ek)
    for j in range(d + 1, len(ticks)):
        tj = _t(ticks[j], j)
        if tj > free_at + 0.5:
            break
        en = D._same(ticks[j], ek)
        mj = D._same(ticks[j], mk)
        if not en or not mj:
            break
        if (_hp(mj) or 1.0) < hp_me0 - HIT_MIN:
            return _rej("pun: a later stage hit us")
        if en.get("act") != act or _started(en, prev_en):
            e = j
            break
        prev_en = en
    if e is None:
        return _rej("pun: never saw them recover")
    end_t = _t(ticks[e], e)
    left = end_t - t_d
    dist = me.get("dist")
    if not isinstance(dist, (int, float)) or left <= 0:
        return _rej('pun: line215')
    air = not me.get("ground")
    mkey = framedata._move_key(dict(me, key=mk))
    allowed = TOGA_BLOOD_OK if toga_mode(me) == "blood" else PUNISH_LABELS
    best = None
    for lab in allowed:
        f = framedata.FEAT_LABELS.get(lab)
        if f and f in me and not me[f] and not toga_mode(me):
            continue
        m = fd.move(mkey, lab, air)
        if not m or not m.get("hits") or m.get("startup") is None:
            continue
        if m["startup"] < MIN_STARTUP:
            continue              # no real timing for it (0.00 in the table)
        reach = m.get("reach")
        if reach is None or dist > reach + 30.0:
            continue
        if m["startup"] + PUNISH_SLACK > left:
            continue
        if best is None or m["startup"] < best[1]:
            best = (lab, m["startup"])
    if best is None:
        return _rej('pun: line235')
    # Did we attack inside the window anyway?
    prev = me
    for j in range(d + 1, len(ticks)):
        if _t(ticks[j], j) > end_t + 0.1:
            break
        cur = D._same(ticks[j], mk)
        if not cur:
            break
        if act_class(cur.get("act")) in ATTACKING and _started(cur, prev):
            return _rej('pun: line245')
        prev = cur
    out.append({"i": d, "label": best[0], "kind": "punish",
                "why": f"{base_name(ek) or ek} {act} whiffed, stuck {left:.2f}s, "
                       f"{best[0]} starts {best[1]:.2f}s at {dist:.0f}"})


def coach(ticks, fd=None, dfn=None):
    """Coach samples for one recording's ticks (stabilized, in order)."""
    fd = fd or framedata.get()
    dfn = dfn or _Defense()
    out = []
    if not fd:
        return out
    start = D.play_start(ticks)
    for s in range(max(1, start), len(ticks)):
        t, tp = ticks[s], ticks[s - 1]
        ek, mk = D.key_of(t, "enemy"), D.key_of(t, "me")
        if not ek or not mk:
            continue
        en, en_prev = D._same(t, ek), D._same(tp, ek)
        if not en or act_class(en.get("act")) not in ATTACKING:
            continue
        if not _started(en, en_prev):
            continue
        REJECT["(attack starts)"] += 1
        _missed_defence(ticks, s, ek, mk, dfn, out)
        _missed_punish(ticks, s, ek, mk, fd, out)
    # One coach label per tick; defence wins (being hit is the bigger loss).
    seen = {}
    for c in out:
        if c["i"] not in seen or c["kind"] == "defence":
            seen[c["i"]] = c
    return sorted(seen.values(), key=lambda c: c["i"])


def load_file(path):
    from . import tickcache
    return D.stabilize(list(tickcache.raw_ticks(path)))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--show", type=int, default=0, help="print N example labels")
    args = ap.parse_args(argv)
    files = []
    for f in args.files:
        files += sorted(glob.glob(f)) or [f]
    fd, dfn = framedata.get(), _Defense()
    kinds = collections.Counter()
    labels = collections.Counter()
    minutes = 0.0
    shown = 0
    for f in files:
        try:
            ticks = load_file(f)
        except Exception as e:          # a damaged recording: skip, say so
            print(f"  skip {f}: {e}", file=sys.stderr)
            continue
        if len(ticks) < 2:
            continue
        minutes += max(0.0, (_t(ticks[-1], len(ticks)) - _t(ticks[0], 0)) / 60.0)
        cs = coach(ticks, fd, dfn)
        for c in cs:
            kinds[c["kind"]] += 1
            labels[(c["kind"], c["label"])] += 1
            if shown < args.show:
                shown += 1
                print(f"  {f} i={c['i']} {c['kind']}: {c['label']} - {c['why']}")
    print(f"{len(files)} files, {minutes:.0f} min of play")
    for k, n in kinds.most_common():
        print(f"  {k:8s} {n:7d}  ({n / max(minutes, 1e-9):.1f}/min)")
    print("  candidates and why they were dropped:")
    for why, n in REJECT.most_common(25):
        print(f"    {n:7d}  {why}")
    for (k, lab), n in labels.most_common(20):
        print(f"    {k:8s} {lab:14s} {n}")


if __name__ == "__main__":
    main()

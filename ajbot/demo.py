"""Demo routes: a combo someone played, written down with its timing so a bot
can practise it (practice.py).

A route here is what ajbot.routes calls a route - a list of decision labels
(Melee, ExUnique1, Dodge, ...) that agent.ACTION_MAP can press - plus WHEN each
one came: `gap` is the number of game frames between the previous step
registering and this one registering, as the player did it. Directed steps
also keep where the stick pointed (`rel`, degrees from straight at the
opponent), and the route keeps how far apart the fighters were when it began
and what it paid (hits, damage).

Each step also keeps what the human's move DID: `lead` is frames from the step
before it coming out (its attack starting) to this one registering - a press
buffered during a move registers long before the move starts, so this is the
timing that reproduces the string - `cue` / `cue_lead` the last thing the
dummy did to the step before (a reaction such as DamagePulledUp, or its nth
hit) and frames from it to this press registering (a press goes in off what
the player saw, wherever and whenever it happened), `out` whether the move visibly started
(a later press in a string does not), `reacts` the dummy's hit reactions to it
(DamagePulledUp, ...) and `en_stun` whether the dummy was still in hitstun
when it came out. practice.py
checks each step against these, so a combo that broke shows where it broke.

    # combos a player landed in a recording (record.py / runs/*.jsonl)
    python -m ajbot.demo list runs/1789871851.jsonl --char Bakugo --min-hits 5
    # the OPPONENT's combos (a friend who played against the recorder)
    python -m ajbot.demo list runs/1794218709.m4.jsonl --side enemy --char Bakugo
    # save one of them by its number in that list
    python -m ajbot.demo save runs/1789871851.jsonl 3 --name "Bakugo BnB"
    # or start from a hand-written route (models/authored_routes.json),
    # every gap a first guess that practice.py then tunes
    python -m ajbot.demo authored Bakugo "cancel route" --gap 14
    python -m ajbot.demo show                 # what is saved

Saved to models/demo_routes.json:

    {"routes": [{"name", "char", "source", "start_dist", "hits", "dealt",
                 "steps": [{"label", "gap", "rel", "out", "lead", "en_stun",
                            "reacts"}, ...],
                 "learned": {...practice.py's results...}}]}
"""
import argparse
import json
import os
import sys
from pathlib import Path

from . import dataset as D
from .roster import base_name

DEMO_PATH = Path("models") / "demo_routes.json"
FPS = 60.0

# Labels that are not a decision in an offensive string.
NOISE = {None, "None", "Guard", "JustGuard"}
# A step is "directed" when its button needs a stick (agent.ACTION_MAP); the
# stick angle recorded with it is kept so the bot can push the same way.
STICK_MIN = 60.0          # lscale below this = stick (near) neutral


def _frames(dt):
    return max(0, int(round(dt * FPS)))


def load(path=DEMO_PATH):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"routes": []}


def save(data, path=DEMO_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def get(name, path=DEMO_PATH):
    """The saved route whose name matches exactly, else the only one whose
    name contains `name` (case-insensitive). None if none or ambiguous."""
    routes = load(path).get("routes") or []
    for r in routes:
        if r.get("name") == name:
            return r
    hits = [r for r in routes if name.lower() in (r.get("name") or "").lower()]
    return hits[0] if len(hits) == 1 else None


class _locked:
    """Exclusive lock on <path>.lock: several practice runs (drill.py, one per
    game copy) and the GUI save into the same file."""

    def __init__(self, path):
        self.lock = Path(path).with_suffix(".lock")

    def __enter__(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.lock, "a+")
        if os.name == "nt":
            import msvcrt
            self.fh.seek(0)
            while True:
                try:
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_LOCK, 1)   # retries ~10 s itself
                    break
                except OSError:
                    continue
        else:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if os.name == "nt":
            import msvcrt
            self.fh.seek(0)
            try:
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self.fh.close()


def put(route, path=DEMO_PATH):
    """Add `route`, replacing a saved one of the same name."""
    with _locked(path):
        data = load(path)
        routes = [r for r in data.get("routes") or [] if r.get("name") != route.get("name")]
        routes.append(route)
        data["routes"] = routes
        save(data, path)


def remove(name, path=DEMO_PATH):
    """Delete the saved route called exactly `name`. True if one was there."""
    with _locked(path):
        data = load(path)
        routes = data.get("routes") or []
        keep = [r for r in routes if r.get("name") != name]
        if len(keep) == len(routes):
            return False
        data["routes"] = keep
        save(data, path)
        return True


def _rel(me, en):
    """Stick angle relative to the opponent (0 = at them), or None if the
    stick was near neutral."""
    lc, ls = me.get("lcam"), me.get("lscale") or 0.0
    if not isinstance(lc, (int, float)) or ls < STICK_MIN:
        return None
    return round(D.wrap180(lc - D.tcam_of(me, en)), 1)


def fresh_attack(prev, cur):
    """An attack action started between two records. `cmd` sticks at the last
    command, so a move pressed again (a second TQ2 after the first) changes
    nothing label_of sees - but its attack action restarting does."""
    a1 = (cur or {}).get("act") or ""
    if not a1.startswith("Attack") or not prev:
        return False
    a0 = prev.get("act")
    return a1 != a0 or (cur.get("act_n", 0) or 0) < (prev.get("act_n", 0) or 0)


# Actions that are not "standing ready": a route that begins in one of these
# began earlier (a launcher, a jump) and must start from there.
SAME_PRESS_S = 0.2     # a command and its attack starting this close are one press
BUFFER_MAX_S = 2.0     # a registered move starting this much later is still that press

BUSY = ("Attack", "Jump", "Air", "Land", "Fall")
LEAD_MAX_S = 4.0


def _busy(rec):
    return (rec.get("act") or "").startswith(BUSY)


def lead_in(ticks, i0):
    """Back up from tick i0 to where our fighter last stood ready (not mid-move,
    not in the air), so a combo that starts airborne keeps the jump or
    launcher that put them there. Stops at a hit taken or a tag."""
    t0, key0 = ticks[i0].get("t"), D.key_of(ticks[i0], "me")
    i = i0
    while i > 0:
        t = ticks[i - 1]
        me = D._rec(t, "me")
        if not me or D.key_of(t, "me") != key0 or (t0 - t.get("t", t0)) > LEAD_MAX_S:
            break
        if (me.get("act") or "").startswith("Damage") or not _busy(me):
            break
        i -= 1
    return i


def steps_of(ticks, i0, i1):
    """[(tick index, label, me record, enemy record)] for the fighter on our
    point from tick i0 to i1, stopping at a tag (practice runs one fighter)."""
    out, prev, key0 = [], None, None
    last_t, last_cmd, pending = None, None, None
    for i in range(i0, i1 + 1):
        t = ticks[i]
        me, en = D._rec(t, "me"), D._rec(t, "enemy")
        key = D.key_of(t, "me")
        if key0 is None and me:
            key0 = key
        elif me and key != key0:
            break
        lab = D.label_of(prev, me)
        now = t.get("t")
        if lab:
            last_t, last_cmd = now, (me or {}).get("cmd")
            # Its move may start later (buffered during the move before - a
            # Unique2 pressed mid-string comes out ~half a second on).
            pending = None if fresh_attack(prev, me) and is_move(lab, me.get("act")) else lab
        elif fresh_attack(prev, me):
            # The command shows before its attack starts; that start is the
            # same press, not a second one. Only an attack that restarts with
            # no new command is a repeat ("Q2 (x2)").
            same_press = (last_cmd == me.get("cmd") and isinstance(now, (int, float))
                          and isinstance(last_t, (int, float)) and now - last_t < SAME_PRESS_S)
            buffered = (pending and is_move(pending, me.get("act"))
                        and isinstance(now, (int, float)) and isinstance(last_t, (int, float))
                        and now - last_t < BUFFER_MAX_S)
            if not (same_press or buffered):
                lab = me.get("cmd")
            pending = None
            last_t, last_cmd = now, me.get("cmd")
        lab = D.refine_dash(lab, ticks, i, key)
        if lab not in NOISE:
            out.append((i, lab, me, en))
        prev = me
    return out


OUT_MAX_TICKS = 40         # a registered move that has not come out in ~2 s never will


def is_move(label, act):
    """`act` is the move a step's label presses (AttackUnique2 for Unique2,
    DashBegin for Dash, AirDodge for Dodge)."""
    act, label = act or "", label or ""
    if not label:
        return False
    if act.startswith("Attack"):
        core = act[6:]
        return core == label or (core.endswith(label) and not core.endswith("Ex" + label))
    return label in act


def move_out(label, prev, cur):
    """The step's move came out between two records: our act switched to (or
    restarted) that move. A string that keeps one act (three Melees are one
    AttackMelee) shows no start for its later presses."""
    if not prev or not cur or not is_move(label, cur.get("act")):
        return False
    return (cur.get("act") != prev.get("act")
            or (cur.get("act_n", 0) or 0) < (prev.get("act_n", 0) or 0))


def in_hitstun(act):
    """The dummy cannot act: hit, knocked down, bounced, stunned or grabbed
    (getting up - "...Recover" - is free)."""
    act = act or ""
    return act.startswith("Damage") and "Recover" not in act


def hit_reaction(prev, cur):
    """The dummy's act if a hit just landed on it between two records; the
    generic "Damage" every hit passes through says nothing and is None."""
    a1 = (cur or {}).get("act") or ""
    if not in_hitstun(a1) or a1 == "Damage":
        return None
    a0 = (prev or {}).get("act")
    if a1 != a0 or (cur.get("act_n", 0) or 0) < ((prev or {}).get("act_n", 0) or 0):
        return a1
    return None


def owner(anchors, t):
    """Which step a moment belongs to: the step with the latest anchor at or
    before t (the first of steps sharing one), or None."""
    best = None
    for j, a in enumerate(anchors):
        if a is not None and a <= t and (best is None or a > anchors[best]):
            best = j
    return best


def _outcomes(ticks, raw, end_i):
    """Per step, what the human's move did. Each step has an anchor: the tick
    its move came out, or the tick it registered when it never visibly starts
    (a later press in a string). Kept per step: `out` (the move visibly came
    out), `lead` (frames from the step before coming out to this one
    registering - a press buffered during a move registers long before it
    starts, so this is the timing that reproduces the string), `en_stun` (the
    dummy was in hitstun at this step's anchor) and `reacts` (hit reactions
    the dummy showed from this anchor to the next one)."""
    n = len(raw)
    regs = [i for i, *_ in raw]
    outs, lo = [None] * n, 0
    for k, (i, lab, _me, _en) in enumerate(raw):
        cap = regs[k + 1] + 3 if k + 1 < n else i + OUT_MAX_TICKS
        for j in range(max(i, lo, 1), min(cap, i + OUT_MAX_TICKS, len(ticks))):
            if move_out(lab, D._rec(ticks[j - 1], "me"), D._rec(ticks[j], "me")):
                outs[k], lo = j, j + 1
                break
    anchors = [o if o is not None else r for o, r in zip(outs, regs)]
    reacts = [[] for _ in range(n)]
    stop = min(end_i + 20, len(ticks) - 1)
    for j in range(max(anchors[0], 1), stop + 1):
        hit = hit_reaction(D._rec(ticks[j - 1], "enemy"), D._rec(ticks[j], "enemy"))
        k = owner(anchors, j) if hit else None
        if k is not None and hit not in reacts[k]:
            reacts[k].append(hit)
    events = [[] for _ in range(n)]    # per step: [(tick, "hit" | reaction)]
    for j in range(max(anchors[0], 1), stop + 1):
        e0, e1 = D._rec(ticks[j - 1], "enemy"), D._rec(ticks[j], "enemy")
        k = owner(anchors, j)
        if k is None:
            continue
        h0, h1 = e0.get("hp"), e1.get("hp")
        if isinstance(h0, (int, float)) and isinstance(h1, (int, float)) and h1 < h0 - 1e-4:
            events[k].append((j, "hit"))
        hit = hit_reaction(e0, e1)
        if hit:
            events[k].append((j, hit))
    res = []
    for k in range(n):
        r = {"out": outs[k] is not None, "en_stun": in_hitstun(
            D._rec(ticks[anchors[k]], "enemy").get("act")), "reacts": reacts[k]}
        if k and outs[k - 1] is not None:
            r["lead"] = _frames(ticks[regs[k]].get("t", 0) - ticks[outs[k - 1]].get("t", 0))
        # The cue this press went in off: the last thing the dummy did to the
        # step before (a reaction such as DamagePulledUp, else its nth hit).
        before = [e for e in (events[k - 1] if k else []) if e[0] <= regs[k]]
        if before:
            j = before[-1][0]
            same = [e for e in before if e[0] == j]
            react = next((e[1] for e in same if e[1] != "hit"), None)
            cue = ({"react": react} if react
                   else {"hit": sum(1 for e in before if e[1] == "hit")})
            r["cue"] = cue
            r["cue_lead"] = _frames(ticks[regs[k]].get("t", 0) - ticks[j].get("t", 0))
        res.append(r)
    return res


def from_combo(ticks, combo, name=None, source=None):
    """A demo route from one ajbot.combos.Combo in a loaded recording."""
    i0 = lead_in(ticks, combo.start_i)
    raw = steps_of(ticks, i0, combo.end_i)
    if not raw:
        return None
    steps, t_prev = [], None
    outc = _outcomes(ticks, raw, combo.end_i)
    for n, (i, lab, me, en) in enumerate(raw):
        t = ticks[i].get("t")
        gap = 0 if t_prev is None else _frames(t - t_prev)
        t_prev = t
        # Where the stick sat between this press and the next (an aerial's
        # drift, a walk-in): the angle most of those ticks held, if most did.
        i_next = raw[n + 1][0] if n + 1 < len(raw) else min(i + 1, combo.end_i)
        rels = [_rel(D._rec(ticks[j], "me") or {}, D._rec(ticks[j], "enemy"))
                for j in range(i + 1, i_next)]
        held = sorted(r for r in rels if r is not None)
        hold = held[len(held) // 2] if rels and len(held) * 2 > len(rels) else None
        steps.append({"label": lab, "gap": gap, "rel": _rel(me, en), "hold": hold,
                      "air": (D._rec(ticks[max(i - 1, 0)], "me") or {}).get("act", "")
                      .startswith(("Jump", "Air", "Fall"))})
        oc = outc[n]
        for k in ("out", "lead", "cue", "cue_lead", "en_stun", "reacts"):
            if oc.get(k) is not None:
                steps[-1][k] = oc[k]
    me0 = raw[0][2]
    char = base_name(me0.get("id")) or base_name(D.key_of(ticks[raw[0][0]], "me")) or "?"
    d0 = me0.get("dist")
    return {"name": name or f"{char} demo", "char": char, "source": source,
            "start_dist": round(float(d0), 1) if isinstance(d0, (int, float)) else None,
            "hits": combo.hits, "dealt": round(combo.dealt, 4), "steps": steps}


def from_labels(labels, char, name, gap=14, source="authored"):
    """A demo route from a bare label list (an authored route): every gap is a
    first guess, the stick points straight at the opponent where needed."""
    return {"name": name, "char": char, "source": source, "start_dist": None,
            "hits": None, "dealt": None,
            "steps": [{"label": l, "gap": 0 if k == 0 else gap, "rel": None}
                      for k, l in enumerate(labels)]}


def combos_in(path, char=None, min_hits=3, side="me"):
    """(ticks, [combos]) for one recording, filtered to our fighter `char`.
    side="enemy" reads the recording from the opponent's side (a friend who
    played against the recorder)."""
    from . import combos as C
    ticks = D.load(str(path))
    D.calibrate_frame(ticks)   # older recordings: which stick angle is 'toward'
    if side == "enemy":
        ticks = D.swap_sides(ticks)
    out = []
    for c in C.find(ticks):
        if c.hits < min_hits:
            continue
        key = D.key_of(ticks[c.start_i], "me") or ""
        if char and (base_name(key) or "").lower() != char.lower():
            continue
        out.append(c)
    return ticks, out


def _fmt_steps(steps, n=40):
    s = " ".join(f"{x['label']}+{x['gap']}" if x["gap"] else x["label"] for x in steps[:n])
    return s + (" ..." if len(steps) > n else "")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="combos in a recording")
    p.add_argument("recording")
    p.add_argument("--side", choices=("me", "enemy"), default="me",
                   help="whose combos: the recorder's or the opponent's")
    p.add_argument("--char")
    p.add_argument("--min-hits", type=int, default=3)
    p = sub.add_parser("save", help="save one combo of a recording as a demo route")
    p.add_argument("recording")
    p.add_argument("index", type=int, help="its number in `list`")
    p.add_argument("--side", choices=("me", "enemy"), default="me")
    p.add_argument("--char")
    p.add_argument("--min-hits", type=int, default=3)
    p.add_argument("--name", required=True)
    p = sub.add_parser("authored", help="turn an authored route into a demo route")
    p.add_argument("char")
    p.add_argument("match", help="part of the authored route's name")
    p.add_argument("--gap", type=int, default=14, help="first-guess frames between steps")
    p.add_argument("--name")
    sub.add_parser("show", help="saved demo routes")
    p = sub.add_parser("delete", help="delete a saved demo route (exact name)")
    p.add_argument("name")
    a = ap.parse_args(argv)

    if a.cmd == "delete":
        if not remove(a.name):
            sys.exit(f"no saved route is called '{a.name}'")
        print(f"deleted '{a.name}' from {DEMO_PATH}")
        return 0

    if a.cmd in ("list", "save"):
        ticks, found = combos_in(a.recording, a.char, a.min_hits, a.side)
        if a.cmd == "list":
            for n, c in enumerate(found):
                r = from_combo(ticks, c)
                if not r:
                    continue
                secs = (c.end_t or 0) - (c.start_t or 0)
                print(f"{n:3d}  {r['char']:<12} hits {c.hits:2d}  dealt {c.dealt:.3f}  "
                      f"{secs:4.1f}s  dist {r['start_dist']}  t={c.start_t:.0f}")
                print(f"      {_fmt_steps(r['steps'])}")
            if not found:
                print("no combos matched")
            return 0
        if not 0 <= a.index < len(found):
            sys.exit(f"no combo {a.index} (the list has {len(found)})")
        r = from_combo(ticks, found[a.index], name=a.name,
                       source=f"{Path(a.recording).name}#{a.index}")
        if not r:
            sys.exit("that combo has no steps for our fighter")
        put(r)
        print(f"saved '{r['name']}' ({r['char']}, {len(r['steps'])} steps) to {DEMO_PATH}")
        print(f"  {_fmt_steps(r['steps'])}")
        return 0

    if a.cmd == "authored":
        from .routes import ROUTES_PATH
        data = json.loads(ROUTES_PATH.read_text())
        items = [r for r in (data.get("chars") or {}).get(a.char) or []
                 if a.match.lower() in (r.get("name") or "").lower()]
        if len(items) != 1:
            names = [r.get("name") for r in (data.get("chars") or {}).get(a.char) or []]
            sys.exit(f"{len(items)} authored {a.char} routes match '{a.match}': {names}")
        src = items[0]
        r = from_labels(src["steps"], a.char, a.name or src["name"], gap=a.gap,
                        source=f"authored:{src['name']}")
        if src.get("requires"):
            r["requires"] = src["requires"]
        put(r)
        print(f"saved '{r['name']}' ({len(r['steps'])} steps, gap {a.gap}) to {DEMO_PATH}")
        return 0

    for r in load().get("routes") or []:
        lr = r.get("learned") or {}
        best = lr.get("best")
        print(f"{r['name']}  [{r['char']}, {len(r['steps'])} steps, from {r.get('source')}]")
        print(f"  {_fmt_steps(r['steps'])}")
        if best is not None:
            print(f"  practised: best {best}/{len(r['steps'])} steps, "
                  f"landed {lr.get('landed', 0)}/{lr.get('attempts', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

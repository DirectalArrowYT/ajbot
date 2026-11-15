"""Combo routes mined from human play, for the route follower (ajbot.routes).

Hand-written routes (models/authored_routes.json) exist for two characters,
which is why only they run full combos in matches: the policy alone finds
openers and short strings, not the exact long routes a player knows. This
reads the human side of every recording - runs/ (a person played "me") and
botruns/ solo files where a person was the opponent (seen from their side) -
and writes each character's best repeatable routes in the same format, as
decision labels (dataset.label_of), which is exactly what the follower
matches against live play.

A route is one engagement of the human's point character (labels no more
than GAP_S apart, no tag in between), trimmed to run from the step that
opened the first hit to the last step that still did damage, kept when it
took at least MIN_DMG of the enemy's health. Per character the best few
distinct routes are kept: ones seen more than once rank first, a single big
one (BIG_DMG) still counts.

Each league fit writes its own copy next to its policy (ajbot.datadir), so
the champion keeps the routes it was evaluated with:

    python -m ajbot.routemine                  # -> <data dir>/mined_routes.json
    python -m ajbot.routemine --show           # print, write nothing
"""
import argparse
import bisect
import collections
import glob
import json
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import dataset as D, datadir
from .parallel import safe_worker_count
from .chain import act_class
from .roster import base_name, move_char

RECOVER_S = 0.5      # enemy out of hitstun this long = the combo dropped
# Enemy act classes that mean they are free (the combo dropped). 'combo' and
# 'other' are not here: a carried victim reads AirChaseBegin for over a second
# mid-combo (Mirio's Ex Q2), grabs and cinematics read as 'other'.
FREE = {"guard", "dodge", "attack", "idle", "move", "recovered"}
LAG_S = 0.25         # the act updates a tick after the health drop
RISE_S = 0.25        # health back up this long = a new floor (refill, regen);
                     # the flicker in old recordings never holds 0.1 s
GAP_S = 2.5          # the follower's own gap (RouteFollower gap_s)
HIT_LEAD_S = 0.6     # a step this long before the first hit opened it
TAIL_S = 0.8         # damage this long after a step still counts as its
MIN_STEPS, MAX_STEPS = 2, 24   # 2: Todoroki's Ex jab > Ex Q2 is a real combo
MIN_DMG = 0.2        # of one fighter's health bar
BIG_DMG = 0.4        # a route seen once is kept only above this
PER_CHAR = 6         # the follower takes the deepest match, so more is fine
NOISE = {None, "None", "Guard", "JustGuard"}
MOVES = {"Dash", "Jump", "Dodge", "Escape"}   # never the start or end of a route
SOLO = re.compile(r"^\d+\.jsonl$")
OUT = "mined_routes.json"


def _hp_timeline(ticks, skip=()):
    """[(t, damage dealt so far)] to the enemy we are hitting.

    Health drops of the point fighter, summed tick by tick on the same
    fighter: a tag swap mid-engagement does not zero it (comparing the two
    ends by key did), training mode refilling the bar does not undo it, and
    the benched fighters' readings (they jump around on KOs, swaps and stale
    records; summed to >1 bar for three jabs) never count.

    Only drops below the lowest reading count, and that floor rises again
    only once the bar has stayed up for RISE_S (a refill, red health
    regenerating): a reading that flickers between two values every tick
    (an old recording does) would otherwise add up to several bars."""
    out, total = [], 0.0
    key0 = floor = up = None
    for t in ticks:
        now = t.get("t")
        if not isinstance(now, (int, float)):
            continue
        en = D._rec(t, "enemy") or {}
        key, hp = en.get("key"), en.get("hp")
        if key and key not in skip and isinstance(hp, (int, float)):
            if key != key0:
                key0, floor, up = key, hp, None
            elif hp < floor:
                total += floor - hp
                floor, up = hp, None
            elif hp > floor + 0.02:
                up = up if up is not None else now
                if now - up >= RISE_S:
                    floor = hp
            else:
                up = None
        out.append((now, total))
    return out


def _flickers(ticks):
    """Enemy keys whose health reading jumps back up several times a second:
    two records sharing one key (a mirror match in old recordings), so their
    health is unusable."""
    ups, prev, bad = collections.defaultdict(collections.deque), (None, None), set()
    for t in ticks:
        en = D._rec(t, "enemy") or {}
        key, hp, now = en.get("key"), en.get("hp"), t.get("t")
        if not key or not isinstance(hp, (int, float)) or not isinstance(now, (int, float)):
            continue
        if prev[0] == key and hp > prev[1] + 0.05:
            q = ups[key]
            q.append(now)
            while q and now - q[0] > 1.0:
                q.popleft()
            if len(q) >= 4:
                bad.add(key)
        prev = (key, hp)
    return bad


def _dmg(tl, times, t0, t1):
    """Health the enemy point fighter lost between t0 and t1."""
    if not tl:
        return 0.0
    i = max(0, bisect.bisect_left(times, t0) - 1)
    j = min(len(tl) - 1, bisect.bisect_right(times, t1))
    return max(0.0, tl[j][1] - tl[i][1])


def routes_of(ticks):
    """[(char, steps, dmg, (t0, t1))] for the 'me' fighter of one recording;
    t0/t1 are the recording times of the first and last step."""
    D.stabilize(ticks)
    tl = _hp_timeline(ticks, _flickers(ticks))
    times = [x[0] for x in tl]
    out, cur, prevs = [], None, {}

    def close(c):
        if not c or len(c["steps"]) < 2:
            return
        steps = c["steps"]
        # When the enemy last lost health: a multi-hit move (Shigaraki's Ex Q1
        # hits for ~4 s) goes on doing damage long after its press.
        end = max(steps[-1][0] + TAIL_S, c.get("hit", 0) + 0.05)
        # First hit: the earliest step whose window shows the enemy losing hp.
        # A step's window runs to the next press (a slow move such as Gang
        # Orca's Ex jab lands ~1.3 s in), at most GAP_S.
        first = last = None
        for i, (ts, _) in enumerate(steps):
            nxt = steps[i + 1][0] if i + 1 < len(steps) else end
            if _dmg(tl, times, ts, min(nxt, ts + GAP_S) + 0.05) > 0.005:
                if first is None:
                    first = i
                last = i
        if first is None:
            return
        # The step that opened it may sit just before the first damaging one.
        if first > 0 and steps[first][0] - steps[first - 1][0] <= HIT_LEAD_S:
            first -= 1
        while first <= last and steps[first][1] in MOVES:
            first += 1
        while last >= first and steps[last][1] in MOVES:
            last -= 1
        seq = steps[first:last + 1]
        if not MIN_STEPS <= len(seq) <= MAX_STEPS:
            return
        # Damage up to the next press (or the enemy's last health drop when
        # this is the engagement's last step).
        tail = end if last == len(steps) - 1 else \
            min(steps[last + 1][0], seq[-1][0] + GAP_S)
        dmg = _dmg(tl, times, seq[0][0], max(tail, seq[-1][0] + TAIL_S))
        # One fighter has one bar and a KO or swap closes the combo: more is
        # bad data.
        if MIN_DMG <= dmg <= 1.0 and c["char"]:
            out.append((c["char"], [lab for _, lab in seq], round(dmg, 3),
                        (seq[0][0], seq[-1][0])))

    en_hp = en_key = drop_t = None
    for t in ticks:
        me = D._rec(t, "me")
        now = t.get("t")
        key = (me or {}).get("key")
        if not isinstance(now, (int, float)) or not key:
            continue
        # Per key: some fighters flip between two records mid-combo (Chisaki /
        # Chisaki2), and a label only makes sense against the same record.
        prev = prevs.get(key)
        lab = D.label_of(prev, me) if prev else None
        prevs[key] = me
        # The enemy still losing health keeps the combo alive: a long move
        # (Mirio's q4 hits for ~3.5 s) leaves a gap between presses longer
        # than GAP_S. Health going back up is training mode refilling it -
        # the next attempt, never part of this one.
        en = D._rec(t, "enemy") or {}
        hp = en.get("hp")
        if en.get("key") and en.get("key") != en_key:
            # A KO or a swap on their side: a combo is on one fighter.
            if cur and cur.get("hit") and en_key is not None:
                close(cur)
                cur = None
            en_key, en_hp, drop_t = en.get("key"), None, None
        act = en.get("act")
        known = act not in (None, "None")
        # Knocked down counts: the combo is still on (Down / DownLoop).
        stun = (act_class(act) == "damage" or str(act).startswith("Down")) if known else True
        free = known and not stun and act_class(act) in FREE
        if isinstance(hp, (int, float)):
            if cur and en_hp is not None:
                if hp < en_hp - 1e-4:
                    drop_t = now
                elif hp > en_hp + 0.02:
                    close(cur)
                    cur = None
            en_hp = hp
        # Only damage taken in hitstun extends the combo - not chip through a
        # guard, not an assist hitting a free opponent. The act lags the
        # health by a tick, so a drop just before the Damage act counts.
        if cur and stun and drop_t is not None and now - drop_t <= LAG_S:
            cur["hit"] = max(cur.get("hit", 0), drop_t)
        # The enemy free (guarding, acting, back up) for RECOVER_S after we
        # hit them: the combo dropped, whatever we keep pressing.
        if cur and cur.get("hit") and known:
            if not free:
                cur.pop("out", None)
            elif now - cur.setdefault("out", now) > RECOVER_S:
                # Presses after their last health drop belong to the next
                # attempt: a slow opener (Todoroki's Ex jab lands ~1.3 s in)
                # is often pressed as the previous combo ends.
                carry = [s for s in cur["steps"] if s[0] > cur["hit"]]
                cur["steps"] = [s for s in cur["steps"] if s[0] <= cur["hit"]]
                close(cur)
                cur = {"key": cur["key"], "char": cur["char"], "steps": carry} if carry else None
        if lab in NOISE:
            continue
        # Only roster fighters: the follower looks routes up by base name.
        name = move_char(me, key) if isinstance(key, str) and base_name(key) else None
        if cur and (now - max(cur["steps"][-1][0], cur.get("hit", 0)) > GAP_S
                    or (name or key) != (cur["char"] or cur["key"])):
            close(cur)
            cur = None
        if cur is None:
            cur = {"key": key, "char": name, "steps": []}
        cur["steps"].append((now, lab))
    close(cur)
    return out


def _human_enemy(ticks):
    """True when the opponent in a solo bot recording was a person, not the CPU."""
    n = ai = 0
    for t in ticks[::20]:
        en = D._rec(t, "enemy")
        if en:
            n += 1
            ai += bool(en.get("ai"))
    return n > 0 and ai / n < 0.5


def _one(path):
    try:
        ticks = D.load(path)
    except Exception:
        return []
    if not ticks:
        return []
    if "botruns" in Path(path).parts:
        if not _human_enemy(ticks):
            return []
        ticks = D.swap_sides(ticks)
    try:
        return [(c, s, d, path, span) for c, s, d, span in routes_of(ticks)]
    except Exception:
        return []


def files():
    human = sorted(glob.glob("runs/*.jsonl"))
    solo = sorted(f for f in glob.glob("botruns/*.jsonl") if SOLO.match(Path(f).name))
    return human + solo


def mine(paths, workers=4):
    found = []
    paths = list(paths)
    n = safe_worker_count([Path(p).stat().st_size for p in paths], workers) if paths else 1
    if n > 1 and len(paths) > 1:
        with ProcessPoolExecutor(max_workers=n) as ex:
            for r in ex.map(_one, paths):
                found.extend(r)
    else:
        for p in paths:
            found.extend(_one(p))
    return found


def pick(found, per_char=PER_CHAR):
    """{char: [route dict]}: the best distinct routes per character."""
    by = collections.defaultdict(lambda: collections.defaultdict(list))
    for char, steps, dmg, path, span in found:
        by[char][tuple(steps)].append((dmg, path, span))
    out = {}
    for char, seqs in by.items():
        cands = []
        for steps, hits in seqs.items():
            dmgs = sorted(d for d, _, _ in hits)
            med = dmgs[len(dmgs) // 2]
            n = len(hits)
            if n < 2 and med < BIG_DMG:
                continue
            score = med * (1 + 0.25 * min(n - 1, 4))
            # The biggest one, so the combo drill can rebuild it with the
            # player's own timing and stick (demo.from_combo).
            d, p, span = max(hits, key=lambda h: h[0])
            src = {"file": p, "t0": span[0], "t1": span[1], "dmg": d}
            cands.append((score, steps, n, med, len({p for _, p, _ in hits}), src))
        cands.sort(key=lambda c: c[:5], reverse=True)
        chosen = []
        for score, steps, n, med, nfiles, src in cands:
            # A route that is a prefix of one already kept (or the other way
            # round) adds nothing the follower would not already match.
            if any(steps[:len(c)] == c or c[:len(steps)] == steps for c, *_ in chosen):
                continue
            chosen.append((steps, n, med, nfiles, src))
            if len(chosen) >= per_char:
                break
        if chosen:
            out[char] = [{"name": f"mined {char}: {' > '.join(s)}", "steps": list(s),
                          "mined": {"seen": n, "dmg": med, "files": nf}, "src": src}
                         for s, n, med, nf, src in chosen]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", action="store_true", help="print the routes, write nothing")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=None, help=f"default: <data dir>/{OUT}")
    a = ap.parse_args(argv)
    paths = files()
    found = mine(paths, a.workers)
    chars = pick(found)
    total = sum(len(v) for v in chars.values())
    for char in sorted(chars):
        for r in chars[char]:
            m = r["mined"]
            print(f"  {char}: {len(r['steps'])} steps, {m['dmg']:.2f} dmg, seen {m['seen']}x"
                  f" - {' > '.join(r['steps'])}")
    print(f"{total} routes for {len(chars)} characters from {len(found)} human combos"
          f" in {len(paths)} recordings")
    if a.show:
        return
    dest = Path(a.out) if a.out else datadir.out(OUT)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "chars": chars}, indent=1), encoding="utf-8")
    tmp.replace(dest)
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()

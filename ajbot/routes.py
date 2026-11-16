"""Authored combo routes: a sequence a player wants a character to run.

The combo book (ajbot.combobook) is mined from what happened to work. This is
the other direction - a route written down by someone who knows the character,
e.g. Bakugo's

    Jab, TQ1, dodge-cancel, Jab, TQ1, jump-cancel, Jab x4, Counter Attack, TQ1,
    dash-cancel, Jab x4, Counter Attack, TQ1, TQ2 + four A follow-ups, Plus Ultra

Each step is a decision label - the same names the action head is trained on
(dataset.label_of): Melee, Unique1, Unique2, Unique2Follow, Dodge, Jump, Dash,
CounterAttack, PlusUltra1, ... - so every step already has a button press in
agent.ACTION_MAP, and the same route can be found in recordings.

Routes live in models/authored_routes.json:

    {"chars": {"Bakugo": [{"name": "...", "steps": ["Melee", "Unique1", ...],
                           "requires": {"awake": 1}}]}}   # requires is optional

At run time the follower watches the label stream of the fighter it controls,
finds how far into a route the fighter already is (the longest route prefix
that ends the recent labels), and suggests the next step. It only overrides
the model when the route disagrees, and only while a combo is running or the
match into the route is already two or more steps deep.
"""
import json
import re
import time
from pathlib import Path

from . import clock
from . import dataset as D
from .roster import base_name

ROUTES_PATH = Path("models") / "authored_routes.json"
# Routes the combo trainer landed on the farm (drill.py): shared by every
# model, ranked after the hand-written ones and before the mined ones.
DRILLED_PATH = Path("models") / "drilled_routes.json"

# Labels that say nothing about progress through an offensive route.
NOISE = {None, "None", "Guard", "JustGuard"}

# Steps that are only accepted inside a short window after the previous step.
# Bakugo's TQ2 takes its follow-up right at the start of the move - the choice
# first registers 0.12-0.35 s after TQ2 begins in recordings, far earlier than
# the move's declared cancel windows - so pressing it later does nothing. Keyed
# by label prefix -> (earliest, latest) seconds after the previous step.
STEP_WINDOWS = {"ExUnique2Follow": (0.05, 0.75)}

# "Wait" is a step that presses nothing. Aizawa's Q2 is a grab: if he does not
# follow it up he lets the opponent go at the end, and a release from the right
# spot leaves him on the ground to start the ground string again (the air-to-
# ground loop). Anything pressed during the grab ruins it, so the follower holds
# still - buttons and stick - until the opponent has been let go and Aizawa is
# on the ground. Never before WAIT_MIN_S; past WAIT_MAX_S the grab missed and
# the route is dropped rather than continued from the wrong place.
WAIT = "Wait"
WAIT_MIN_S = 0.5
WAIT_MAX_S = 4.0
# A route step pressed this recently is a combo attempt in progress.
FRESH_S = 0.6


def load(path=ROUTES_PATH):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_all(folder=None):
    """Authored routes, then the combo library's authored and potential
    combos (combos/, ajbot.combodb), then the ones the combo trainer landed
    (drill.py), then the routes mined from human play (ajbot.routemine) that
    belong to the model in `folder` - on a tie the earlier source wins.
    Without a folder, the active data folder (ajbot.datadir)."""
    from . import combodb, datadir
    data = load() or {"chars": {}}
    try:
        library = {"chars": combodb.routes_for_bot()}
    except (OSError, ValueError):
        library = None
    drilled = load(DRILLED_PATH)
    mined = load(datadir.path_in(folder, "mined_routes.json") if folder
                 else datadir.path("mined_routes.json"))
    chars = {c: list(v) for c, v in (data.get("chars") or {}).items()}
    for extra in (library, drilled, mined):
        for c, items in ((extra or {}).get("chars") or {}).items():
            have = {tuple(r.get("steps") or ()) for r in chars.get(c, ())}
            chars.setdefault(c, []).extend(r for r in items
                                           if tuple(r.get("steps") or ()) not in have)
    return {**data, "chars": chars}


def toks_of(hist):
    return [lab for _, lab in (hist or [])]


def label_stream(ticks, key_of=None):
    """[(t, label)] for the controlled fighter in a recording - the same labels
    the action head trains on. Used to find where a route already occurs."""
    D.stabilize(ticks)
    out, prev = [], None
    for t in ticks:
        me = D._rec(t, "me")
        lab = D.label_of(prev, me)
        if lab and lab not in NOISE:
            out.append((t.get("t"), lab))
        prev = me
    return out


def _usable(items):
    good = [(r.get("name", "route"), list(r["steps"]), dict(r.get("requires") or {}))
            for r in items if len(r.get("steps") or []) >= 2 and not r.get("disabled")]
    # Conditional routes (e.g. Rising-only) are tried first, so on a tie
    # the more specific route wins.
    good.sort(key=lambda r: not r[2])
    return good


_PL = re.compile(r"^Pl(\d+)")


class RouteFollower:
    def __init__(self, data, gap_s=2.5, min_match=2, ai=None):
        """`data`: models/authored_routes.json. `ai`: the game's CPU combos
        (ajbot.aicombos, models/ai_routes.json) - loaded by default, False to
        leave them out. CPU routes rank below every hand-written route and
        are only followed while one of our combos is running."""
        self.gap_s = gap_s
        self.min_match = min_match
        self.routes = {}
        for char, items in ((data or {}).get("chars") or {}).items():
            good = _usable(items)
            if good:
                self.routes[char] = good
        if ai is None:
            from . import aicombos
            ai = aicombos.load()
        self.ai_routes = {}               # export folder ('Pl02') -> routes
        for folder, items in ((ai or {}).get("folders") or {}).items():
            good = _usable(items)
            if good:
                self.ai_routes[folder] = good
        self.ai_followed = 0
        self.prev = {}        # fighter key -> last record seen
        self.hist = {}        # fighter key -> [(time, label)]
        self.followed = 0
        self.waiting = {}     # fighter key -> {"t0", "grab"} while holding still
        self.enemy = {}       # fighter key -> the opponent's latest record
        self.en_hp = {}       # fighter key -> the opponent's last health
        self.hit_t = {}       # fighter key -> when the opponent last lost health
        self.best = {}        # fighter key -> (depth, name, steps) this engagement
        self.runs = []        # finished engagements: (key, depth, name, steps)

    def __bool__(self):
        return bool(self.routes or self.ai_routes)

    def _ai_for(self, key):
        m = _PL.match(key or "") if isinstance(key, str) else None
        if not m:
            return None
        for folder in (f"Pl{m.group(1)}", f"Pl{int(m.group(1)):02d}"):
            if folder in self.ai_routes:
                return self.ai_routes[folder]
        return None

    def holding(self, me):
        """True while a Wait step is in force: press nothing, hold the stick."""
        return ((me or {}).get("key")) in self.waiting

    def observe(self, me, en=None, now=None):
        """Feed the controlled fighter's record (and the opponent's) every tick."""
        key = (me or {}).get("key")
        if not key:
            return
        self.enemy[key] = en or {}
        now = clock.now() if now is None else now
        # The enemy still losing health keeps the route alive: a long move
        # (Mirio's q4 hits for ~3.5 s) leaves more than gap_s between steps.
        hp = (en or {}).get("hp")
        if isinstance(hp, (int, float)):
            was = self.en_hp.get(key)
            if was is not None and hp < was - 1e-4:
                self.hit_t[key] = now
            self.en_hp[key] = hp
        prev = self.prev.get(key)
        self.prev[key] = me
        if prev is None:
            return
        lab = D.label_of(prev, me)
        if lab in NOISE:
            return
        h = self.hist.setdefault(key, [])
        if h and now - max(h[-1][0], self.hit_t.get(key, 0)) > self.gap_s \
                and key not in self.waiting:
            self._close(key)
            h.clear()                       # the combo (or the engagement) ended
        h.append((now, lab))
        del h[:-64]
        got = (self.progress_all(me) or [None])[0]
        if got and got[2] > (self.best.get(key) or (0,))[0]:
            self.best[key] = (got[2], got[0], got[1])

    def _close(self, key):
        b = self.best.pop(key, None)
        if b:
            self.runs.append((key, b[0], b[1], b[2]))

    def report(self, reset=True):
        """One line per route: how many engagements reached it, the deepest
        step, and which step the bot stalled on (the step after the deepest)."""
        for key in list(self.best):
            self._close(key)
        if not self.runs:
            return "route: no engagement reached step 1"
        by = {}
        for _, depth, name, steps in self.runs:
            by.setdefault(name, []).append((depth, steps))
        lines = []
        for name, items in by.items():
            depths = sorted(d for d, _ in items)
            steps = items[0][1]
            top = depths[-1]
            stall = steps[top] if top < len(steps) else "(finished)"
            lines.append(f"route '{name}': {len(items)} runs, deepest {top}/{len(steps)}"
                         f", median {depths[len(depths)//2]}, stalls before {stall}"
                         f"; followed {self.followed}x")
        if reset:
            self.runs.clear()
        return chr(10).join(lines)

    def _matches(self, me, routes, toks):
        found = []
        for name, steps, req in routes or ():
            # `requires` maps a fighter field to the truthiness it must have,
            # e.g. {"awake": 1} for a route that only exists while in Rising.
            if any(bool((me or {}).get(k)) != bool(v) for k, v in req.items()):
                continue
            top = min(len(steps) - 1, len(toks))
            for n in range(top, 0, -1):
                if steps[:n] == toks[-n:]:
                    found.append((name, steps, n))
                    break
        found.sort(key=lambda r: -r[2])         # stable: file order breaks ties
        return found

    def progress_all(self, me, ai=True):
        """Every route this fighter is part-way through, as [(name, steps,
        matched)]: hand-written routes deepest first, then (with `ai`) the
        game's CPU routes deepest first."""
        key = (me or {}).get("key")
        from .roster import move_char
        char = move_char(me, key)
        toks = [lab for _, lab in self.hist.get(key, [])]
        found = self._matches(me, self.routes.get(char), toks)
        if ai:
            found += self._matches(me, self._ai_for(key), toks)
        return found

    def progress(self, me):
        """(route name, steps, matched) for the hand-written route this fighter
        is deepest into, or None. CPU routes do not count: they must not keep
        the combo book (mined from human play) from overriding them."""
        got = self.progress_all(me, ai=False)
        return got[0] if got else None

    def _wait(self, me, model_pick, available, comboing, now):
        key = (me or {}).get("key")
        t = clock.now() if now is None else now
        ws = self.waiting.setdefault(key, {"t0": t, "grab": False})
        gripped = bool((self.enemy.get(key) or {}).get("grab"))
        ws["grab"] = ws["grab"] or gripped
        elapsed = t - ws["t0"]
        released = ws["grab"] and not gripped
        if elapsed > WAIT_MAX_S:                    # the grab never connected
            del self.waiting[key]
            self.hist.get(key, []).clear()
            return None
        if elapsed >= WAIT_MIN_S and released and (me or {}).get("ground"):
            del self.waiting[key]
            self.hist.setdefault(key, []).append((t, WAIT))
            return self.suggest(me, model_pick, available, comboing, now)
        self.followed += 1
        return "None"                               # press nothing

    def suggest(self, me, model_pick, available, comboing=False, now=None):
        """The route's next step when it differs from the model's pick. When the
        deepest route's next step cannot be used (Plus Ultra with no gauge),
        the next-deepest route that can is tried instead."""
        key = (me or {}).get("key")
        t = clock.now() if now is None else now
        h = self.hist.get(key) or []
        # A route's opener just thrown (or the opponent just lost health)
        # counts as a combo running: the chain tracker only opens one on the
        # hit, and in the gap before it the model's own pick (Mirio's Q1 after
        # a jab) cancelled the string, so no route ever got past step 1.
        mine = comboing or bool(h and t - h[-1][0] <= FRESH_S) \
            or t - self.hit_t.get(key, -1e9) <= 1.0
        for name, steps, n in self.progress_all(me):
            cpu = name.startswith("CPU: ")
            # CPU routes only extend a live combo, and only once two of their
            # steps have happened: on human runs the next step matched what
            # the player did 16% of the time after one step, 46%/32%/80%
            # after two/three/four.
            if cpu and (not comboing or n < 2):
                continue
            if n < self.min_match and not mine:
                continue
            nxt = steps[n]
            if nxt == WAIT:
                return self._wait(me, model_pick, available, comboing, now)
            if nxt == model_pick:
                return None
            if not available(nxt):
                continue
            if nxt.startswith("ExUnique2Follow"):
                # TQ2 takes one follow-up, or two while in Rising.
                used = 0
                for tok in reversed(toks_of(self.hist.get((me or {}).get("key")))):
                    if tok.startswith("ExUnique2Follow"):
                        used += 1
                    elif tok == "ExUnique2":
                        break
                if used >= (2 if (me or {}).get("awake") else 1):
                    continue
            win = next((w for pre, w in STEP_WINDOWS.items() if nxt.startswith(pre)), None)
            if win:
                h = self.hist.get((me or {}).get("key")) or []
                if h:
                    dt = (clock.now() if now is None else now) - h[-1][0]
                    if not (win[0] <= dt <= win[1]):
                        continue           # too early to register, or too late
            break
        else:
            return None
        self.followed += 1
        if cpu:
            self.ai_followed += 1
        return nxt

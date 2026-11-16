"""Combo practice: the bot repeats a demo route until it lands it.

    python3 bot/practice.py "Bakugo BnB"               # route saved by ajbot.demo
    python3 bot/practice.py "cancel route" --confirm 10 --max-attempts 300
    # straight from combo notation (ajbot/notation.py has the codes)
    python3 bot/practice.py --char Bakugo --notation "AAA > Q1 > Dodge Cancel > AA > Q1"

Runs on its own game copy (AJ_INSTANCE, default 4 here, so a farm on copies
0-3 is not touched) - start it with farm.py-style launching or let it start
the game itself with --launch. Each attempt:

  1. Our character (the route's) vs a dummy team on the gym. Player 2's pad
     holds guard from the first hit of each attempt, so the dummy blocks the
     moment it leaves hitstun and only a true combo lands (--no-guard: it
     just stands there).
  2. Walk in until the fighters are as far apart as when the demo began.
  3. Press the route step by step on the lockstep frame clock. A step is
     pressed `gap` frames after the previous one REGISTERED (its label showed
     up in the bot's own state, the same labels the demo was cut from), minus
     the measured press->register delay, plus a learned per-step offset.
  4. Score it: how many steps registered in order, and whether the chain of
     hits stayed one combo (the victim never left hitstun) to the end.

Timing search: the first step that fails gets its offset tried at 0, -2, +2,
-4, +4 ... frames; an offset that gets further is kept and the search moves
on to the next failing step. When no offset helps, the step before it is
re-tuned once (its timing decides the state the next one is pressed from).
Once the whole route lands, it is repeated --confirm times; it is "down" when
it lands at least --target of those. Results (offsets, best, success rate) go
back into models/demo_routes.json under "learned", and every attempt to
practice.jsonl.

Reset: the practice match is a training-mode match (the battle content byte
is set to Training before it loads). After an attempt the bot waits for the
dummy to recover, presses d-pad Left (training's reset position: both teams
back at the start) and walks back in. The match itself is only restarted
after --per-match attempts, a KO, a failed reset, or after every attempt of a
route with a tag in it (the point fighter changed).
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import argparse
import collections
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

# --instance N picks the game copy; it must be set before nextmatch is imported.
if "--instance" in sys.argv[:-1]:
    os.environ["AJ_INSTANCE"] = sys.argv[sys.argv.index("--instance") + 1]
os.environ.setdefault("AJ_INSTANCE", "4")
# Main Proton prefix; copy N runs in <prefix>-N (offline_game.sh clones it).
from ajbot import local as _local            # noqa: E402
if _local.get("game.prefix"):
    os.environ.setdefault("AJ_PREFIX", _local.get("game.prefix"))

import nextmatch as nm                        # noqa: E402 (port follows AJ_INSTANCE)
import endless as E                           # noqa: E402
from ajbot import agent as A                  # noqa: E402
from ajbot import dataset as D                # noqa: E402
from ajbot import demo                        # noqa: E402
from ajbot import notation                    # noqa: E402
from ajbot.roster import base_name            # noqa: E402
from ajbot import sampler                     # noqa: E402
from ajbot.chain import ChainTracker          # noqa: E402
from ajbot.hook import Hook, HookError        # noqa: E402
from ajbot.lockstep import Link               # noqa: E402
from ajbot.pad import GUARD                   # noqa: E402

HERE = Path(__file__).resolve().parent.parent   # the repo root
NEUTRAL = (0.0, 0.0)
DASHES = {"Dash", "DashForward", "DashBack"}
SEARCH = [0, -2, 2, -4, 4, -6, 6, -8, 8, -11, 11, -14, 14, 18, -18, 24, 30, 38]
BATTLE = "OnBeginBattleSequence = Battle"
# Training mode (EBattleContent at UBattleSettingWork+0x30: 1 local battle,
# 2 training). In training, d-pad Left puts both teams back at the start
# positions (Right: rising gauge max, Down: Plus Ultra max, Up: sidekick).
CONTENT_OFF, TRAINING = 0x30, 2
DPAD_LEFT = 0x0004
def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def same(want, got, step=None):
    if want == got or (step and got in (step.get("alias") or ())):
        return True
    if want in DASHES and got in DASHES:
        return True
    # Recordings older than `incl` name the bare follow-up.
    return want.startswith("ExUnique2Follow") and got == "ExUnique2Follow"


# The hook's "move is available" flag for each label (0 = cooldown, no gauge,
# or locked out right now).
READY = {"Melee": "ok_melee", "ExMelee": "ok_exmelee", "Unique1": "ok_u1",
         "Unique2": "ok_u2", "ExUnique1": "ok_exu1", "ExUnique2": "ok_exu2",
         "PlusUltra1": "ok_pu1", "PlusUltra2": "ok_pu2", "ExtremeAction": "ok_extreme",
         "Escape": "ok_esc", "Unguardable": "ok_ub"}


def _ready(label, me):
    """Why a press may not have come out: the move's flag when the game said
    it was not available, and what the fighter was doing."""
    me = me or {}
    key = READY.get(label)
    parts = []
    if key and me.get(key) == 0:
        parts.append(f"{key}=0")
    act = me.get("act")
    if act and act != "None":
        parts.append(f"in {act}")
    return ", ".join(parts)


class Game:
    """The practice copy: match restarts, lockstep pads, one sampled view."""

    def __init__(self, a, route):
        self.trace = None             # [frame, act, cmd, enemy act, dist, enemy hp] per view
        self.gate = None              # trainer_gate.HumanGate (--trainer)
        self.a = a
        self.route = route
        ids = {n: i for i, n, _ in nm.MENU}
        char = route["char"]
        if char not in ids:
            sys.exit(f"{char} is not on the character menu")
        partners = [int(x) for x in a.partners.split(",")]
        want = [ids[c] for c in route.get("partners") or [] if c in ids]
        missing = [c for c in route.get("partners") or [] if c not in ids]
        if missing:
            sys.exit(f"{missing} not on the character menu")
        # Tag targets join the team first; the defaults fill the rest.
        partners = (want + [x for x in partners if x not in want and x != ids[char]])[:2]
        self.p1 = [ids[char]] + partners
        # Which trigger calls whom, guessed from pick order (first partner on
        # the left) and corrected when a tag brings the other one.
        self.tag_side = {}
        self.p2 = [int(x) for x in a.dummy.split(",")]
        self.mine = [nm.NAME[i] for i in self.p1]
        self.theirs = [nm.NAME[i] for i in self.p2]
        if set(self.mine) & set(self.theirs):
            sys.exit("the dummy team shares a character with ours - pick other --dummy ids")
        self.hook = Hook(timeout=10.0)
        self.link = None
        self.pad = self.pad2 = None
        self.roster = None
        self.chain = None
        self.matches = 0
        self.attempts_here = 0
        self.watch = None
        self.home = None              # fighter distance at the start positions
        self.overrun = 0.0            # how far a walk-in carries on after letting go
        self.homes = collections.Counter()   # distances seen after resets

    # ---- match ----
    def restart(self):
        """(Re)start the practice match and link the pads. False on failure."""
        if self.link:
            self.link.release()
            self.link = None
        reloaded = False
        for _ in range(4):
            self.watch = E.LogWatch(E.game_log())
            try:
                work, _ = nm.addrs()
                nm.wr(work + CONTENT_OFF, bytes([TRAINING]))
                # Manual control mode: on Normal the game turns repeated A
                # presses into its own auto combo, so "AAAA" would not land
                nm.start(p1=self.p1, p2=self.p2, stage=E.GYM, log=lambda m: None, manual=True)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                log(f"match start failed: {e}")
                time.sleep(5)
                continue
            t0 = time.time()
            seen = ""
            while time.time() - t0 < 120:
                time.sleep(1)
                seen += self.watch.new_text()
                if BATTLE in seen and E.in_battle():
                    break
                if self.a.speed != 1:
                    nm.set_speed(self.a.speed)
            else:
                log("match did not reach Battle in 120 s")
                continue
            # a character's first battle adds its control-mode entry during the
            # load, too late for that load: set it now and load once more
            changed, missing = nm.set_manual_all([self.p1, self.p2])
            if changed and not reloaded:
                log("control mode Manual set for " + ", ".join(nm.NAME.get(c, str(c)) for c in sorted(changed))
                    + " - reloading the match")
                reloaded = True
                continue
            if changed or missing:
                log("warning: auto combo may stay on for "
                    + ", ".join(nm.NAME.get(c, str(c)) for c in sorted(changed | missing)))
            if self.a.speed != 1:
                nm.set_speed(self.a.speed)
            sampler.reset(self.hook)
            self.link = Link(self.hook)
            self.pad, self.pad2 = self.link.pad(0), self.link.pad(1)
            self.roster = sampler.Roster()
            self.roster.assign(self.mine, "mine")
            self.roster.assign(self.theirs, "theirs")
            self.roster.exclusive = True
            self.chain = ChainTracker()
            self.matches += 1
            self.attempts_here = 0
            self.step(10)
            return True
        return False

    def reset_position(self):
        """Training mode's d-pad Left: both teams back at the start positions
        without reloading the match. True once the fighters stand at the
        starting distance again."""
        me, _ = self.view()
        d0 = (me or {}).get("dist")
        self.pad.clear()
        self.pad.tap(DPAD_LEFT, hold_frames=4, gap_frames=8)
        d, last, still = None, None, 0
        for k in range(30):
            self.step(4)
            me, _ = self.view()
            d = (me or {}).get("dist")
            if not isinstance(d, (int, float)):
                continue
            still = still + 1 if last is not None and abs(d - last) < 3 else 0
            last = d
            settled = still >= 3 and (not isinstance(d0, (int, float))
                                      or abs(d - d0) > 40 or k >= 25)
            if settled and len(self.homes) < 20:
                # The resets tell where the start positions are: the distance
                # the fighters most often settle at after one (a running
                # match may have them anywhere at first, a fighter knocked
                # away reads thousands off).
                self.homes[round(d, -1)] += 1
                home = self.homes.most_common(1)[0][0]
                if home != self.home:
                    log(f"  start positions: dist {home:.0f}")
                self.home = home
                if sum(self.homes.values()) < 3:
                    self.step(10)
                    self.chain = ChainTracker()
                    return True
            if self.home is not None and abs(d - self.home) < 40:
                self.step(10)
                self.chain = ChainTracker()
                return True
        log(f"  reset: dist {d} after d-pad Left, start was "
            + (f"{self.home:.0f}" if self.home is not None else "unknown"))
        return False

    def over(self):
        """The match ended (result screen)."""
        return any(m in self.watch.new_text() for m in E.END_MARKS)

    # ---- frames ----
    def step(self, frames=1):
        self.link.step(frames)
        return self.link.frame

    def view(self):
        """(me, enemy) records this frame, or (None, None)."""
        try:
            snap = sampler.snapshot(self.hook)
        except HookError:
            return None, None
        self.roster.observe(snap)
        t = sampler.teams(snap, roster=self.roster)
        me, en = t.get("me"), t.get("enemy")
        if self.trace is not None and me and en:
            self.trace.append([self.link.frame, me.get("act"), me.get("cmd"), en.get("act"),
                               round(me.get("dist") or 0), round(en.get("hp") or 0, 4),
                               me.get("atk"), me.get("atkcount")])
        if me and en:
            self.chain.update(sampler.dedupe(snap), [r["key"] for r in t["mine"]],
                              [r["key"] for r in t["theirs"]], me["key"], en["key"])
        return me, en

    # ---- between attempts ----
    def settle(self, max_frames=600):
        """Until the dummy is back on its feet and nothing is running."""
        calm, f0 = 0, self.link.frame
        self.pad.clear()
        while self.link.frame - f0 < max_frames:
            self.step(4)
            me, en = self.view()
            if not me or not en:
                calm = 0
                continue
            busy = (sampler.act_class(en.get("act")) in ("damage", "recovered")
                    or "Down" in (en.get("act") or "")
                    or (me.get("act") or "").startswith("Attack"))
            calm = 0 if busy else calm + 4
            if calm >= 30:
                return True
        return False

    def approach(self, want, max_frames=900):
        """Walk at the dummy until `dist` <= want, then stand still. True when
        in range."""
        f0 = self.link.frame
        best, best_f = 1e9, f0
        while self.link.frame - f0 < max_frames:
            me, en = self.view()
            if not me or not en:
                self.step(2)
                continue
            d = me.get("dist")
            if isinstance(d, (int, float)) and d < best - 2:
                best, best_f = d, self.link.frame
            # Bodies touching: the distance stops shrinking above `want`
            # (about 190 units for two standing fighters), which is as close
            # as walking gets.
            # Some fighters stop further out (Mirio's run ends ~414 away), so
            # a walk that has stopped closing in for a while counts too.
            stalled = self.link.frame - best_f
            touching = (isinstance(d, (int, float))
                        and ((d < want + 150 and stalled > 20)
                             or (d < want + 400 and stalled > 90)))
            if isinstance(d, (int, float)) and (d <= want + self.overrun or touching):
                # Stand still first: a press with run momentum left is a
                # different (running) move, and the chain after it drops.
                self.pad.stick(NEUTRAL)
                self.step(self.a.stand)
                # The run carries on after letting go (530 asked, 430 got):
                # learn how far, and let go that much earlier next time - a
                # lunging opener's timing depends on where it starts.
                me2, _ = self.view()
                d2 = (me2 or {}).get("dist")
                if not touching and isinstance(d2, (int, float)):
                    self.overrun = min(400.0, max(0.0, self.overrun + 0.7 * (want - d2)))
                return True
            self.pad.stick(A.toward(me, 0.0, en=en))
            self.step(2)
        self.pad.stick(NEUTRAL)
        me, en = self.view()
        log(f"  approach gave up: dist {(me or {}).get('dist')} want {want:.0f}, "
            f"act {(me or {}).get('act')} cmd {(me or {}).get('cmd')}, "
            f"enemy act {(en or {}).get('act')}")
        return False


fresh_attack = demo.fresh_attack


def kill_copy(inst):
    """Stop every process of game copy `inst` (a hung game holds its port)."""
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            env = (d / "environ").read_bytes().split(b"\0")
            cmd = (d / "cmdline").read_bytes()
        except OSError:
            continue
        if f"AJ_INSTANCE={inst}".encode() in env and any(b in cmd for b in (b"offline_game", b"netbridge", b"AJGAME",
                                                         b"proton", b"wine", b"pressure-vessel")):
            # killpg only reaches group leaders; the wineserver and Proton's
            # wrapper are not, and one left alive holds the copy's lock.
            for kill in (os.killpg, os.kill):
                try:
                    kill(int(d.name), signal.SIGKILL)
                except OSError:
                    pass
    time.sleep(5)


def launch_game(a):
    """Start this copy headless and wait for the menu."""
    env = dict(os.environ, AJ_VPADS="2", AJ_GAME_ARGS="-nullrhi -RenderOffScreen")
    env.pop("DISPLAY", None)
    if a.xvfb is not None and Path(f"/tmp/.X11-unix/X{a.xvfb}").exists():
        # The farm's virtual screen. Without a DISPLAY the launcher falls
        # back to :0 - the desktop - and every copy takes X clients there.
        for k in ("WAYLAND_DISPLAY", "XAUTHORITY"):
            env.pop(k, None)
        env["DISPLAY"] = f":{a.xvfb}"
    out = open(HERE / f"offline_game-{os.environ['AJ_INSTANCE']}.log", "ab")
    game = subprocess.Popen(["bash", a.launcher], cwd=Path(a.launcher).parent, env=env,
                            stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    log(f"game starting (pid {game.pid})")
    if not E.wait_hook(300):
        sys.exit("hook did not come up in 300 s")
    if E.where() == "menu":
        log("waiting for the save to load")
        if not E.wait_menu(300):
            sys.exit("title / save load not seen")
        time.sleep(3)
    return game


def relaunch(a, g):
    """The match would not start (game hung): with --launch, restart the copy."""
    if not a.launch:
        return False
    log("game copy not responding - relaunching it")
    kill_copy(os.environ["AJ_INSTANCE"])
    launch_game(a)
    g.hook = Hook(timeout=10.0)
    sampler.install(g.hook, every_pumps=1)
    return g.restart()


GRAB_MAX_F = 240                  # a grab that has not let go by then missed


def plan(step, me, en):
    """(buttons, stick) for one route step."""
    lab = step.get("press") or step["label"]   # press one move, expect another
    stick = NEUTRAL
    if step.get("rel") is not None:
        # Held in the demo - for a lever move it picks the move, for an
        # aerial it steers the drift - so push the same way.
        stick = A.toward(me, float(step["rel"]), en=en)
    buttons, stick, _ = A.plan_press(lab, stick, me, en)
    return buttons, stick


TRACE = open(os.environ["AJ_TRACE"], "a") if os.environ.get("AJ_TRACE") else None


REACT_GRACE_F = 6                 # a hit landing this soon after the next step still counts
FREE_GRACE_F = 2                  # frames out of hitstun before the dummy counts as free
OUT_WAIT_F = 30                   # past the old timing with no move out: press anyway


class Judge:
    """Checks an attempt step by step against what the dummy did in the
    human's recording (demo route fields `out`, `en_stun`, `reacts`).

    Each step has an anchor: the frame its move came out, or - for a press
    that never visibly starts (a later Melee in one string) - the frame it
    registered. A step DIVERGES when the dummy's hit reactions from its anchor
    to the next differ from the human's (DamageStrong where the human got
    DamagePulledUp: the move came out as a cancel, from the wrong range...).
    The combo BREAKS when a step comes out with the dummy free where the
    human's was still in hitstun, or the dummy's health refills (training
    mode does that when a combo ends). On a break the real failure point is
    the first divergence before it, else the step that came too late."""

    def __init__(self, steps):
        self.steps = steps
        n = len(steps)
        self.out = [None] * n
        self.anchor = [None] * n
        self.last_out = -1
        self.events = []          # [(frame, reaction)]
        self.pending = []         # [(step, window end)]
        self.checked = set()
        self.diverged = None      # (step, why), the first one
        self.prev_en = None
        self.ev = [[] for _ in range(n)]   # per step: [(frame, "hit" | reaction)]
        self.last_stun = None
        self.hp_min = 1.0
        self.active = any(s.get("reacts") is not None or s.get("en_stun") is not None
                          for s in steps)

    def cue_at(self, j, cue):
        """Frame the dummy gave step j's cue ({"react": X} or {"hit": n}), or None."""
        if "react" in cue:
            return next((f for f, e in self.ev[j] if e == cue["react"]), None)
        hits = [f for f, e in self.ev[j] if e == "hit"]
        n = int(cue.get("hit") or 1)
        return hits[n - 1] if len(hits) >= n else None

    def wants_out(self, j):
        return bool(self.steps[j].get("out"))

    def see(self, f, me_prev, me, en, obs):
        """One frame. Returns the step whose move came out this frame (or None)."""
        came = None
        for j in range(self.last_out + 1, len(self.steps)):
            if obs[j] is None:
                break
            if self.out[j] is None and self.wants_out(j) \
                    and demo.move_out(self.steps[j]["label"], me_prev, me):
                self.out[j], self.last_out, came = f, j, j
                break
        hit = demo.hit_reaction(self.prev_en, en)
        k = demo.owner(self.anchor, f)
        if hit:
            self.events.append((f, hit))
            if k is not None:
                self.ev[k].append((f, hit))
        if demo.in_hitstun(en.get("act")):
            self.last_stun = f
        hp = en.get("hp")
        hp0 = (self.prev_en or {}).get("hp")
        if isinstance(hp, (int, float)) and isinstance(hp0, (int, float)) and hp < hp0 - 1e-4 \
                and k is not None:
            self.ev[k].append((f, "hit"))
        if isinstance(hp, (int, float)):
            self.hp_min = min(self.hp_min, hp)
        self.prev_en = en
        return came

    def set_anchor(self, j, f):
        if self.anchor[j] is not None:
            return
        self.anchor[j] = f
        for i in range(j):
            if i not in self.checked and self.anchor[i] is not None and self.anchor[i] < f \
                    and all(p[0] != i for p in self.pending):
                self.pending.append((i, f + REACT_GRACE_F))

    def _check(self, i, end):
        self.checked.add(i)
        want = set(self.steps[i].get("reacts") or [])
        a = self.anchor[i]
        got = {h for t, h in self.events if a is not None and a <= t < end}
        if self.diverged is None and want and not (want & got):
            got_s = "/".join(sorted(got)) if got else "no hit"
            self.diverged = (i, f"step {i} {self.steps[i]['label']}: dummy {got_s}, "
                                f"human {'/'.join(sorted(want))}")

    def flush(self, f, force=False):
        keep = []
        for i, end in self.pending:
            if force or f >= end:
                self._check(i, min(end, f + 1))
            else:
                keep.append((i, end))
        self.pending = keep

    def broke(self, f, en):
        """(fail step, why) if the combo has broken by frame f, else None."""
        if not self.active:
            return None
        nxt = next((j for j, a in enumerate(self.anchor) if a is None), None)
        if nxt is None:
            return None
        why = None
        hp = en.get("hp")
        if self.hp_min < 0.999 and isinstance(hp, (int, float)) and hp >= 0.999:
            why = "combo reset (dummy health refilled)"
        return self._blame(f, nxt, why) if why else None

    def at_anchor(self, j, f, en):
        """Step j just came out: was the dummy where the human's was?"""
        if not self.active or not self.steps[j].get("en_stun"):
            return None
        free = self.last_stun is None or f - self.last_stun > FREE_GRACE_F
        if free:
            return self._blame(f, j, f"dummy free ({en.get('act')}) when step {j} came out")
        return None

    def _blame(self, f, j, why):
        self.flush(f, force=True)
        for i in range(j):
            if i not in self.checked and self.anchor[i] is not None:
                self._check(i, f + 1)
        if self.diverged and self.diverged[0] <= j:
            return self.diverged[0], f"{self.diverged[1]}; then {why}"
        return j, why


def attempt(g, route, adj, lat, a):
    """Run the route once. Returns a result dict."""
    steps = route["steps"]
    n = len(steps)
    press_f = [None] * n
    press_state = {}              # step -> what the game said about the move at the press
    obs_f = [None] * n
    k = 0                         # next step to press
    due = g.link.frame            # frame the next press is due
    held, release_at = (), None
    trig = None                   # trigger pulled for a tag step
    key0 = None
    drift = None                  # stick angle kept held after a press
    grab_k = grab_seen = grab_rel = None   # waiting out a grab before step grab_k
    extra = []                    # [frame, step] more presses of a "Q2 (x3)" step
    prev_me = None
    fail_at = why = got = None
    dropped = False
    f_end = None
    judge = Judge(steps)
    wait_out = None               # (step, "hit"/"out", due by the old timing, give-up frame)
    guarding = False              # the dummy holds guard once our first hit lands
    g.chain.drain()

    def timing(w, f):
        """When step w+1 is due, now that step w registered at f: off the
        dummy's cue for it (the reaction or hit the human pressed off), or
        step w's move coming out; else off the registration."""
        due_gap = max(f + 1, f + steps[w + 1]["gap"] - lat + adj[w + 1])
        if not judge.active:
            return due_gap, None
        if steps[w + 1].get("cue"):
            return float("inf"), (w, "cue", due_gap, due_gap + OUT_WAIT_F)
        if steps[w + 1].get("lead") is not None and judge.wants_out(w):
            return float("inf"), (w, "out", due_gap, due_gap + OUT_WAIT_F)
        return due_gap, None

    while True:
        f = g.link.frame
        me, en = g.view()
        if me is None or en is None:
            fail_at = next((j for j in range(n) if obs_f[j] is None), None)
            why = "lost view"
            break
        key = me.get("key")
        if key0 is not None and key != key0:
            prev_me = None        # a tag: the new fighter's first record has no "before"
        key0 = key
        lab = D.label_of(prev_me, me)
        if not lab and fresh_attack(prev_me, me):
            # `cmd` sticks at the last command while the fighter stands, so a
            # new jab after an idle spell changes nothing label_of can see -
            # but its attack action starting is the press registering.
            lab = me.get("cmd")
        if TRACE and k:
            TRACE.write(f"{f} k={k} lab={lab} act={me.get('act')} cmd={me.get('cmd')} "
                        f"z={me.get('z')} dist={me.get('dist')} ls={me.get('lscale')} "
                        f"held={held} en={en.get('act')}\n")
        me_before = prev_me
        prev_me = me
        # The pressed step still waiting to register (at most one: the next
        # press waits for it).
        w = k - 1 if k and obs_f[k - 1] is None else None
        if w is not None and steps[w]["label"] == "Tag":
            arrived = base_name(key)
            if arrived == steps[w]["tag"]:
                obs_f[w] = f
                judge.set_anchor(w, f)
                if w + 1 < n:
                    due, wait_out = timing(w, f)
            elif prev_me is None and arrived:
                # The other partner came in: that trigger is theirs.
                side = g.tag_side.get(steps[w]["tag"])
                g.tag_side[arrived] = side
                g.tag_side[steps[w]["tag"]] = "right" if side == "left" else "left"
                fail_at, why, got = w, f"tag brought {arrived}", None
                break
            lab = None            # the outgoing fighter's labels are not the route's
        if lab and lab not in demo.NOISE and w is not None:
            if same(steps[w]["label"], lab, steps[w]):
                obs_f[w] = f
                if not judge.wants_out(w):
                    judge.set_anchor(w, f)
                    bad = judge.at_anchor(w, f, en)
                    if bad:
                        fail_at, why = bad
                        break
                if w + 1 < n:
                    due, wait_out = timing(w, f)
                # "Q2 (x3)": press it again while it plays out, then go on.
                # Learned: when each extra press landed after the step came out.
                at = steps[w].get("repeat_at") or []
                for r in range(1, steps[w].get("presses") or 1):
                    off = at[r - 1] - lat - 2 if r - 1 < len(at) else r * notation.REPEAT_EVERY
                    extra.append([f + max(1, off), steps[w]])
                if extra:
                    due = max(due, extra[-1][0] + a.hold + 2)
            elif not (w and same(steps[w - 1]["label"], lab, steps[w - 1])):
                fail_at, why, got = w, f"got {lab}", lab
                break
        came = judge.see(f, me_before, me, en, obs_f)
        if came is not None:
            judge.set_anchor(came, f)
            bad = judge.at_anchor(came, f, en)
            if bad:
                fail_at, why = bad
                break

        for j in range(n):
            # A move the human's came out of that never did here: anchor it
            # where it registered so the checks go on.
            if obs_f[j] is not None and judge.anchor[j] is None and f - obs_f[j] > 40:
                judge.set_anchor(j, obs_f[j])
        if wait_out:
            w0, kind, due_gap, give_up = wait_out
            at = judge.cue_at(w0, steps[w0 + 1]["cue"]) if kind == "cue" else judge.out[w0]
            if at is not None:
                lead = steps[w0 + 1]["cue_lead" if kind == "cue" else "lead"]
                due, wait_out = max(f + 1, at + lead - lat + adj[w0 + 1]), None
            elif f >= give_up:
                due, wait_out = due_gap, None
        judge.flush(f)
        bad = judge.broke(f, en)
        if bad:
            fail_at, why = bad
            break
        if g.chain.finished and k < n + 1 and any(o is None for o in obs_f) \
                and any(o is not None for o in obs_f):
            dropped = True        # the dummy left hitstun before the route ended
            if a.strict:
                fail_at = next(j for j in range(n) if obs_f[j] is None)
                why = "chain dropped"
                break
        if w is not None and f - press_f[w] > a.timeout:
            fail_at, why = w, "no response" + (f" ({press_state[w]})" if press_state.get(w) else "")
            break
        if obs_f[-1] is not None:
            f_end = f if f_end is None else f_end
            if g.chain.by_us is None or f - f_end > 120:
                break
        if not guarding and not a.no_guard and g.chain.by_us is not None:
            g.pad2.hold(GUARD)
            guarding = True
        if trig and f >= release_at:
            g.pad.trigger(trig, 0.0)
            trig = None
        if held and f >= release_at:
            g.pad.release(*held)
            held = ()
        if not held:
            # Between presses the stick stays where the demo held it.
            g.pad.stick(A.toward(me, drift, en=en) if drift is not None else NEUTRAL)
        if k < n and k and steps[k].get("after_grab") and obs_f[k - 1] is not None:
            # The step before was a grab (Aizawa's Q2): press nothing until it
            # lets the opponent go and we are on the ground, then time from there.
            if grab_k != k:
                grab_k, grab_seen, grab_rel = k, False, None
            if grab_rel is None:
                if en.get("grab"):
                    grab_seen = True
                elif grab_seen and me.get("ground"):
                    grab_rel = f
                    due = f + steps[k]["gap"] - lat + adj[k]
                if grab_rel is None:
                    if f - obs_f[k - 1] > GRAB_MAX_F:
                        fail_at, why = k, "grab missed" if not grab_seen else "grab never let go"
                        break
                    if held:
                        g.pad.release(*held)
                        held = ()
                    g.pad.stick(NEUTRAL)
                    drift = None
                    g.step(1)
                    continue
        if extra and f >= extra[0][0] and not held:
            buttons, stick = plan(extra.pop(0)[1], me, en)
            g.pad.stick(stick)
            g.pad.hold(*buttons)
            held, release_at = buttons, f + a.hold
            g.step(1)
            continue
        if k < n and f >= due and (k == 0 or obs_f[k - 1] is not None):
            if held:              # one neutral frame between two presses
                g.pad.release(*held)
                g.pad.stick(NEUTRAL)
                held = ()
                g.step(1)
                continue
            if steps[k]["label"] == "Tag":
                tgt = steps[k]["tag"]
                trig = g.tag_side.setdefault(
                    tgt, A.TAG_SIDES[g.mine.index(tgt) - 1] if tgt in g.mine[1:] else "left")
                if g.gate and not g.gate.wait(k, (), NEUTRAL, trig):
                    fail_at, why, trig = k, "quit", None
                    break
                g.pad.trigger(trig, 1.0)
                release_at = f + max(a.hold, 5)
                drift = None
                press_f[k] = f
                k += 1
                g.step(1)
                continue
            buttons, stick = plan(steps[k], me, en)
            if g.gate and not g.gate.wait(k, buttons, stick, None):
                fail_at, why = k, "quit"
                break
            drift = steps[k].get("hold")
            g.pad.stick(stick)
            g.pad.hold(*buttons)
            held, release_at = buttons, f + (steps[k].get("hold_frames") or a.hold)
            press_f[k] = f
            press_state[k] = _ready(steps[k]["label"], me)
            k += 1
        g.step(1)
    if held:
        g.pad.release(*held)
    if trig:
        g.pad.trigger(trig, 0.0)
    g.pad.stick(NEUTRAL)
    # Let the last hits land and the chain close.
    for _ in range(90):
        g.step(2)
        _me, en = g.view()
        if g.chain.by_us is None:
            break
        if judge.active:
            judge.see(g.link.frame, None, None, en or {}, obs_f)
    combos = g.chain.drain()
    if guarding:
        g.pad2.release(GUARD)
    best = max(combos, key=lambda c: c.hits, default=None)
    if judge.active:
        judge.flush(g.link.frame, force=True)
        for i in range(n):
            if i not in judge.checked and judge.anchor[i] is not None:
                judge._check(i, g.link.frame + 1)
    reached = next((j for j in range(n) if obs_f[j] is None), n)
    if fail_at is not None:
        reached = min(reached, fail_at)
    lats = [o - p for o, p in zip(obs_f, press_f) if o is not None and p is not None]
    return {"reached": reached, "fail_at": fail_at, "why": why, "got": got, "dropped": dropped,
            "hits": best.hits if best else 0, "damage": round(best.damage, 4) if best else 0.0,
            "chains": len(combos), "lat": lats,
            "diverged": judge.diverged[1] if judge.diverged else None,
            "outs": [None if o is None or obs_f[j] is None else o - obs_f[j]
                     for j, o in enumerate(judge.out)],
            "gaps": [None if obs_f[j] is None or obs_f[j - 1] is None else obs_f[j] - obs_f[j - 1]
                     for j in range(1, n)]}


def need_hits(route):
    """Hits an attempt must land to count: half the demo's, at least 1."""
    h = route.get("hits")
    return max(1, int(h) // 2) if isinstance(h, (int, float)) and h else 1


def landed(r, route):
    return (r.get("fail_at") is None and r["reached"] > 0
            and r["hits"] >= need_hits(route))


EARLY_REFOCUS = 5                 # attempts breaking before the focus step before tuning there


class Search:
    """Per-step offset search, first failing step first."""

    def __init__(self, n, adj=None):
        self.n = n
        self.adj = list(adj or [0] * n)
        self.best = -1.0
        self.reached = 0
        self.focus = None         # step being tuned
        self.tries = []           # offsets still to try there
        self.base = 0
        self.back_from = None     # set while re-tuning the step before a stuck one
        self.stuck = None
        self.early = 0            # attempts in a row that broke before the focus step

    def next_adj(self):
        trial = list(self.adj)
        if self.focus is not None and self.tries:
            trial[self.focus] = self.base + self.tries[0]
        return trial

    def _aim(self, f):
        self.focus, self.back_from = f, None
        self.tries = list(SEARCH[1:]) if f is not None else []
        self.base = self.adj[f] if f is not None else 0

    def report(self, trial, r):
        """Feed one attempt's result; returns a short note."""
        self.reached = max(self.reached, r["reached"])
        score = r["reached"] - (0.5 if r.get("dropped") else 0.0)
        fa = r.get("fail_at")
        if self.focus is not None and fa is not None and fa < self.focus:
            self.early += 1
            if self.early >= EARLY_REFOCUS:
                # The best so far was luck: an earlier step breaks nearly every
                # time with these offsets. Tune that one, scored from here.
                self.early = 0
                self.best = score
                self._aim(fa)
                return f"step {fa} keeps breaking - tuning it"
        else:
            self.early = 0
        if self.focus is not None and self.tries and r["reached"] >= self.focus:
            # Only an attempt that got to the step tested this offset; one that
            # failed earlier (start-state noise) tries the same offset again.
            self.tries.pop(0)
        if score > self.best:
            self.best = score
            self.adj = list(trial)
            if r.get("fail_at") != self.focus or self.back_from is not None:
                self._aim(r.get("fail_at"))
            return f"new best {r['reached']}/{self.n}"
        if self.focus is None:
            self._aim(r.get("fail_at"))
            return ""
        if self.tries:
            return ""
        if self.back_from is None and self.focus > 0:
            # Nothing at this step helped: re-tune the one before it once.
            stuck = self.focus
            self._aim(stuck - 1)
            self.back_from = stuck
            return f"step {stuck} stuck - re-tuning step {stuck - 1}"
        self.stuck = self.back_from if self.back_from is not None else self.focus
        return f"step {self.stuck} stuck at every offset"


def nm_ids(name):
    return {n: i for i, n, _ in nm.MENU}[name]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("route", nargs="?", help="demo route name (python -m ajbot.demo show)")
    ap.add_argument("--notation", help="practice this combo notation instead "
                    "(AAA > Q1 > Dodge Cancel > ...; see ajbot/notation.py)")
    ap.add_argument("--route-json", help="a saved route given as JSON (the GUI passes the "
                    "one picked, so it plays on any machine's game)")
    ap.add_argument("--trainer", action="store_true",
                    help="combo trainer for a person: the game holds before every step, an "
                         "overlay on the game window shows the input, and it goes on once you "
                         "press it on your controller (Back quits). Real time, nothing saved")
    ap.add_argument("--watch", type=int, default=0, metavar="N",
                    help="just play the saved timing N times to watch it: nothing is "
                         "tuned or saved")
    ap.add_argument("--char", help="who starts the notation combo")
    ap.add_argument("--name", help="name to save a notation combo under")
    ap.add_argument("--instance", help="game copy (default 4; read before the imports)")
    ap.add_argument("--stop-file", help="stop cleanly when this file appears (the GUI)")
    ap.add_argument("--partners", default="4,3", help="our two partners' menu ids")
    ap.add_argument("--partner-names", help="partners by name (Todoroki,Uraraka), instead of ids")
    ap.add_argument("--dummy", default="8,5,6", help="the dummy team's menu ids")
    ap.add_argument("--no-guard", action="store_true",
                    help="the dummy never guards (default: it holds guard from the first hit of "
                         "each attempt, so a gap in the route is blocked)")
    ap.add_argument("--speed", type=float, default=100)
    ap.add_argument("--per-match", type=int, default=40,
                    help="attempts before a match restart (training mode resets positions in between)")
    ap.add_argument("--confirm", type=int, default=10, help="repeats once it lands")
    ap.add_argument("--target", type=float, default=0.8,
                    help="share of the confirm repeats that must land")
    ap.add_argument("--max-attempts", type=int, default=200)
    ap.add_argument("--stand", type=int, default=20,
                    help="frames to stand still after walking in, before the route")
    ap.add_argument("--hold", type=int, default=3, help="frames each press is held")
    ap.add_argument("--timeout", type=int, default=45,
                    help="frames a pressed step may take to register")
    ap.add_argument("--alias-after", type=int, default=3,
                    help="accept a step's consistent other label after this many attempts")
    ap.add_argument("--start-dist", type=float, help="override the route's start distance")
    ap.add_argument("--loose", dest="strict", action="store_false",
                    help="a dropped chain does not fail the attempt")
    ap.add_argument("--fresh", action="store_true", help="ignore offsets learned before")
    ap.add_argument("--launch", action="store_true",
                    help="start the game copy (offline_game.sh) if the hook is not up")
    ap.add_argument("--launcher", default=_local.get("game.launcher") or str(HERE / "server" / "offline_game.sh"))
    ap.add_argument("--xvfb", type=int, default=99, metavar="N",
                    help="launch the game on Xvfb :N when it is running (farm.py --xvfb), "
                         "not on the desktop")
    ap.add_argument("--log", default=str(HERE / "practice.jsonl"))
    ap.add_argument("--trace", help="write every attempt's frame-by-frame states here (JSONL)")
    a = ap.parse_args()

    if a.partner_names:
        ids = []
        for nme in a.partner_names.split(","):
            try:
                ids.append(nm_ids(notation._char(nme)))
            except notation.NotationError as e:
                sys.exit(f"partner: {e}")
        a.partners = ",".join(str(i) for i in ids)
    if a.notation:
        if not a.char:
            sys.exit("--notation needs --char (who starts the combo)")
        try:
            route = notation.route(a.notation, a.char, a.name)
        except notation.NotationError as e:
            sys.exit(f"notation: {e}")
        old = demo.get(route["name"])
        if old and old.get("name") == route["name"] and old.get("notation") == route["notation"]:
            route["learned"] = old.get("learned")
            # What each step was learned to register as (CA reads as ExMelee).
            for st, o in zip(route["steps"], old.get("steps") or []):
                if o.get("label") == st["label"] and o.get("alias"):
                    st["alias"] = sorted(set(st.get("alias") or []) | set(o["alias"]))
        for k, st in enumerate(route["steps"]):
            log(f"  step {k:2d}  {notation.describe(st)}")
    elif a.route_json:
        try:
            route = json.loads(a.route_json)
        except ValueError as e:
            sys.exit(f"--route-json: {e}")
    else:
        if not a.route:
            sys.exit("name a saved route, or give --notation and --char")
        route = demo.get(a.route)
        if not route:
            sys.exit(f"no saved demo route matches '{a.route}' (python -m ajbot.demo show)")
    steps = route["steps"]
    if any(s["label"] == "Tag" for s in steps):
        a.per_match = 1           # the point fighter changed: start over each time
    missing = [s["label"] for s in steps if s["label"] not in A.ACTION_MAP and s["label"] != "Tag"]
    if missing:
        sys.exit(f"no button mapping for {missing}")
    n = len(steps)
    log(f"route '{route['name']}' ({route['char']}, {n} steps), copy {os.environ['AJ_INSTANCE']}")

    game = None
    if not E.wait_hook(5):
        if not a.launch:
            try:
                old = nm.call("F", timeout=3).startswith("!unknown")
            except OSError:
                old = False
            if old:
                sys.exit("the game's hook (ajhook.asi in HeroGame/Binaries/Win64) is older "
                         "than this AJBot - install the current one and restart the game")
            sys.exit("the game copy's hook is not answering - start it, or pass --launch")
        game = launch_game(a)

    learned = route.get("learned") or {}
    adj = None if a.fresh else learned.get("adj")
    if adj and len(adj) != n:
        adj = None
    search = Search(n, adj)
    lat = int(learned.get("lat", 2)) if not a.fresh else 2
    lat_seen = []
    # Notation has no spacing: start up close unless the combo opens with a
    # move that travels (dash, jump, tilt quirk).
    first = steps[0]["label"]
    near = 160.0 if first in ("Melee", "Unique1", "Unique2", "CounterAttack",
                              "Unguardable", "Dodge") else 300.0
    want = a.start_dist or route.get("start_dist") or near
    want = max(want, 120.0)

    if a.trainer:
        a.watch = a.watch or 10 ** 6
        a.speed = 1
    g = Game(a, route)
    sampler.install(g.hook, every_pumps=1)
    if a.trainer:
        try:
            from trainer_gate import HumanGate
            g.gate = HumanGate(g, route, a.stop_file)
        except (OSError, RuntimeError, ImportError) as e:
            sys.exit(f"trainer: {e}")
    if not g.restart() and not relaunch(a, g):
        sys.exit("could not start the practice match")

    if a.watch:
        a.max_attempts = a.watch
        if not learned.get("adj"):
            log("this combo has no learned timing yet - playing the default gaps")
        log(f"watching: {a.watch} plays with offsets {search.adj}, nothing is tuned or saved")
    tries = 0
    wrong = {}              # (step, label it registered as) -> count
    streak = []             # results since it first landed
    total_landed = 0
    t_start = time.time()
    no_range = 0
    bad_resets = dead = 0     # in a row: failed training resets, attempts nothing registered
    try:
        while tries < a.max_attempts:
            if a.stop_file and os.path.exists(a.stop_file):
                log("stop requested")
                break
            if g.attempts_here >= a.per_match or g.over():
                if not g.restart() and not relaunch(a, g):
                    sys.exit("could not restart the practice match")
            if not g.settle():
                log("dummy did not settle - restarting the match")
                g.attempts_here = a.per_match
                continue
            if not g.reset_position():
                bad_resets += 1
                if bad_resets >= 3 and a.launch:
                    # The copy is stuck (frozen world, dead pads): a match
                    # restart does not help, a fresh game does.
                    log("resets keep failing - relaunching the game copy")
                    bad_resets = dead = 0
                    if not relaunch(a, g):
                        sys.exit("could not relaunch the practice game")
                    continue
                log("training reset did not work - restarting the match")
                g.attempts_here = a.per_match
                continue
            bad_resets = 0
            if not g.approach(want):
                no_range += 1
                if no_range >= 5:
                    log("could not get in range five times running - giving up on this route")
                    break
                log("could not get in range - restarting the match")
                g.attempts_here = a.per_match
                continue
            no_range = 0
            trial = list(search.adj) if (a.watch or streak) else search.next_adj()
            g.trace = [] if a.trace else None
            r = attempt(g, route, trial, lat, a)
            if a.trace:
                # What our fighter and the dummy did every frame we looked,
                # to line up against the person's recording (drill.py --compare).
                with open(a.trace, "a") as fh:
                    fh.write(json.dumps({"route": route["name"], "attempt": tries + 1, "adj": trial,
                                         "reached": r["reached"], "fail_at": r["fail_at"],
                                         "why": r.get("why"), "diverged": r.get("diverged"),
                                         "outs": r.get("outs"), "rows": g.trace}) + "\n")
                g.trace = None
            g.attempts_here += 1
            dead = dead + 1 if (r["reached"] == 0 and not r["hits"]) else 0
            if dead >= 3 and a.launch:
                log("no press registered three times running - relaunching the game copy")
                bad_resets = dead = 0
                if not relaunch(a, g):
                    sys.exit("could not relaunch the practice game")
                continue
            tries += 1
            if a.watch:
                ok = landed(r, route)
                total_landed += ok
                if g.gate:
                    if g.gate.quit:
                        log("trainer stopped (Back on the pad, or Stop)")
                        break
                    g.gate.result(ok, r)
                tag = "LAND" if ok else f"fail@{r['fail_at']}"
                log(f"watch {tries}/{a.watch} {tag:<8} {r['reached']:2d}/{n} steps  "
                    f"hits {r['hits']:2d}  dmg {r['damage']:.3f}  {r.get('why') or ''}")
                continue
            if r["lat"]:
                lat_seen.extend(r["lat"][:1])
                lat = int(round(statistics.median(lat_seen[-20:])))
            ok = landed(r, route)
            if not ok and r.get("fail_at") is None:
                # Every step registered but the hits did not come: score it
                # as failing at the last step, so its timing gets searched.
                r["fail_at"], r["why"] = n - 1, f"only {r['hits']} hits"
                r["reached"] = n - 1
            if r.get("got") and r["fail_at"] is not None:
                # The same other label every time this step is pressed is the
                # game's name for what the press does (Bakugo's third jab reads
                # as Unique1): accept it for this step.
                key = (r["fail_at"], r["got"])
                wrong[key] = wrong.get(key, 0) + 1
                if wrong[key] >= a.alias_after:
                    st = steps[r["fail_at"]]
                    st["alias"] = sorted(set(st.get("alias") or []) | {r["got"]})
                    log(f"step {r['fail_at']} ({st['label']}) registers as {r['got']} - "
                        f"accepting that from now on")
                    wrong.pop(key)
                    search._aim(r["fail_at"])
            total_landed += ok
            note = "" if streak else search.report(trial, r)
            if ok and not streak:
                note = (note + " - LANDED, confirming").strip(" -")
            if streak or ok:
                streak.append(ok)
            tag = "LAND" if ok else f"fail@{r['fail_at']}"
            odd = f"  (but {r['diverged']})" if ok and r.get("diverged") else ""
            log(f"#{tries:3d} {tag:<8} {r['reached']:2d}/{n} steps  hits {r['hits']:2d}  "
                f"dmg {r['damage']:.3f}  {r.get('why') or ''}{odd}  {note}")
            with open(a.log, "a") as fh:
                fh.write(json.dumps({"t": round(time.time()), "route": route["name"],
                                     "adj": trial, "lat": lat, **r}) + "\n")
            if streak and len(streak) >= a.confirm:
                rate = sum(streak[-a.confirm:]) / a.confirm
                if rate >= a.target:
                    log(f"DOWN: landed {sum(streak[-a.confirm:])}/{a.confirm} in a row of repeats")
                    break
                # Not consistent yet: go back to searching from where it fails.
                log(f"only {rate:.0%} of the repeats landed - searching again")
                streak = []
                search.best = -1.0
                search._aim(None)
            if search.stuck is not None and not streak:
                log(f"giving up: step {search.stuck} ({steps[search.stuck]['label']}) did not "
                    f"work at any offset")
                break
    finally:
        if g.link:
            g.link.release()
        if g.gate:
            g.gate.close()
        if a.watch:
            log(f"watched {tries} plays, landed {total_landed}")
        else:
            rec = dict(route)
            rec["learned"] = {"adj": search.adj, "lat": lat,
                              "best": max(search.reached, learned.get("best", 0)),
                              "attempts": learned.get("attempts", 0) + tries,
                              "landed": learned.get("landed", 0) + total_landed,
                              "updated": round(time.time())}
            demo.put(rec)
            log(f"{tries} attempts in {(time.time() - t_start) / 60:.1f} min, landed {total_landed}; "
                f"offsets {search.adj}, press->register {lat} frames - saved to {demo.DEMO_PATH}")
            if a.launch:
                log(f"leaving game copy {os.environ['AJ_INSTANCE']} running for the next practice")


if __name__ == "__main__":
    main()

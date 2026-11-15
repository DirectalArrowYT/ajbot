"""Live policy: game state in, controller input out.

Reads a trained policy, turns a sampler snapshot into the same feature vector
the model was trained on, and maps its two outputs onto the pad:

  action   -> a move, pressed via the button map below
  movement -> a camera-relative stick angle plus magnitude

The movement head predicts [sin(lcam), cos(lcam), lscale]. Because the stick
is camera-relative and forward is +Y, those two components ARE the stick
vector, so no conversion is needed beyond scaling.

Around the model sit four rule-and-bandit policies for what it cannot learn
from the recordings yet (priority order in play.py):

  EscapePolicy    - emergency dodge out of a combo
  DefensePolicy   - just guard / guard / counter / dodge against an attack,
                    choosing by LEARNED reward per situation
  ResourcePolicy  - Rising, Plus Ultra, Plus Ultra Combo
  TagPolicy       - Change and Change Combo

All of them read `act` - the action each fighter is actually running, from the
sampler's action wrappers. Before that existed they leaned on `stun`, `kb` and
`grab`, which on the opponent never fire, so "they are in hitstun" had to be
guessed from health deltas.

Game rules they encode, from the game's own tutorial text:
  * Change (LT/RT) swaps the controlled character; each partner then has a
    cooldown. Changing DURING an attack is a Change Combo, which builds more
    Plus Ultra than an ordinary combo.
  * Just Guard = press and immediately release Guard as the hit arrives. It
    opens a counter window.
  * Counter Attack (Guard + Q1) is armoured. As the opponent attacks it is a
    Counter Crash, which refills Plus Ultra and Rising gauge.
  * Target Combo beats Unblockable, Unblockable beats Counter, Counter beats
    Target Combo.
  * Guarding and dodging spend stamina; guarding on empty is a Stamina Break.
"""
import collections
import json
import math
import os
import pickle
import random
import time
from pathlib import Path

import numpy as np

from . import dataset as D
from . import clock
from .model_utils import tune_for_live


def _write_atomic(path, text):
    """Whole-file replace, so another game copy's bot never reads half a file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
from . import sampler
from .action_log import UnknownActionLog
from .sampler import act_class, Envelope
from .roster import move_char, toga_mode
from .pad import (NATTACK, QUIRKTEC1, QUIRKTEC2, GUARD, JUMP, DASH, RSTICK)

# Action -> (buttons, needs_stick). The "Ex" variants are lever-input forms: a
# direction held with the attack button.
#
# Two bindings come from playing the game, NOT from the shipped KeyConfig,
# which disagrees:
#   Plus Ultra    = GUARD + B   (KeyConfig lists PULTRA1 as GUARD + Y)
#   GUARD + Y     = Counter Attack, a different mechanic entirely
ACTION_MAP = {
    "Melee":         ((NATTACK,), False),
    "ExMelee":       ((NATTACK,), True),
    "Unique1":       ((QUIRKTEC1,), False),
    "ExUnique1":     ((QUIRKTEC1,), True),
    "Unique2":       ((QUIRKTEC2,), False),
    # Follow-ups inside Bakugo's TQ2 (ExUnique2, tilt + B), one per controller
    # button, pressed within ~0.1-0.4 s of TQ2 starting (routes.STEP_WINDOWS):
    # X = blast (the jab follow-up), Y = unblockable, A = armor jump-like move,
    # Dash = the fast step (steps in the stick direction), B = TQ2 pressed
    # again (tilt + B; runs no body, ends the move so TQ2 can be re-cast).
    # "ExUnique2Follow" is the bare label from recordings that predate `incl`;
    # it presses the jab. One follow-up per TQ2, two while in Rising.
    "ExUnique2Follow":     ((NATTACK,), False),
    "ExUnique2FollowX":    ((NATTACK,), False),
    "ExUnique2FollowY":    ((QUIRKTEC1,), False),
    "ExUnique2FollowA":    ((JUMP,), False),
    "ExUnique2FollowB":    ((QUIRKTEC2,), True),
    "ExUnique2FollowDash": ((DASH,), False),
    "ExUnique2":     ((QUIRKTEC2,), True),
    "Unguardable":   ((GUARD, NATTACK), False),
    "PlusUltra1":    ((GUARD, QUIRKTEC2), False),
    "PlusUltra2":    ((GUARD, QUIRKTEC2), False),
    "Awaking":       ((RSTICK,), False),        # Rising: right stick click
    # The Rising action (Extreme Action, e.g. Bakugo's shot lunge) is RB + A =
    # GUARD + JUMP, confirmed by the player; only usable while in Rising (gated
    # on ok_extreme). ResourcePolicy already presses the same buttons for its
    # rule-based "rising_action".
    "ExtremeAction": ((GUARD, JUMP), False),
    # The emergency escape out of hitstun is the same right-stick click; the
    # game reads it as an escape while the fighter is being hit (confirmed by
    # the player). The model already knows WHEN via ok_esc / ev_esc.
    "Escape":        ((RSTICK,), False),
    "Jump":          ((JUMP,), False),
    "Dash":          ((DASH,), False),
    # Directed dashes: the Dash button with the stick forced toward / away from
    # the opponent (the stick frame here is opponent-relative: +y is toward).
    # A retreat dash followed by a forward one is how a whiff gets punished.
    "DashForward":   ((DASH,), True),
    "DashBack":      ((DASH,), True),
    "Guard":         ((GUARD,), False),
    # Defensive labels, learned from `act` in recordings made since the
    # action wrappers went in. A 3-frame press IS a just-guard attempt.
    "JustGuard":     ((GUARD,), False),
    "CounterAttack": ((GUARD, QUIRKTEC1), False),
    "Dodge":         ((GUARD,), True),          # Guard + direction
}

COUNTER_ATTACK = (GUARD, QUIRKTEC1)
RISING_ACTION = (GUARD, JUMP)
RISING_ENTER = (RSTICK,)

# Change is on the analog triggers - one per partner.
TAG_SIDES = ("left", "right")

# Actions we can observe but have no confident input for yet.
# "Wait" is the trained hold label: keep doing what you are doing, no new press.
UNMAPPED = {"None", "Wait", "DamageEscape", "TagCombo"}

QUIRK_ACTIONS = {"Unique1", "Unique2", "ExUnique1", "ExUnique2",
                 "UniqueCharge1", "UniqueCharge2", "PlusUltra1", "PlusUltra2"}
ERASED_FALLBACK = ("ExMelee", "Melee")

# Buttons `learn_actions.py` is allowed to name in models/learned_actions.json,
# by the friendly name used there rather than the vgamepad enum directly - the
# file has to survive being read back without importing vgamepad's constants.
BUTTON_BY_NAME = {"NATTACK": NATTACK, "QUIRKTEC1": QUIRKTEC1,
                  "QUIRKTEC2": QUIRKTEC2, "GUARD": GUARD, "JUMP": JUMP,
                  "DASH": DASH, "RSTICK": RSTICK}

LEARNED_PATH = Path("models") / "learned_actions.json"

# Per-action hold override in frames, for moves learned from observed
# durations (a held quirk fires later than a tapped one). Populated below.
ACTION_HOLD_OVERRIDE = {}


def _load_learned(path=LEARNED_PATH):
    """Merge models/learned_actions.json into ACTION_MAP, filling gaps only -
    a hand-written entry always wins over a guessed one."""
    if not path.exists():
        return
    try:
        learned = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for action, spec in learned.items():
        try:
            buttons = tuple(BUTTON_BY_NAME[b] for b in spec["buttons"])
        except KeyError:
            continue          # a button name this version of pad.py dropped
        if action not in ACTION_MAP:
            ACTION_MAP[action] = (buttons, bool(spec.get("needs_stick")))
        if spec.get("hold_frames"):
            ACTION_HOLD_OVERRIDE[action] = int(spec["hold_frames"])


_load_learned()

# Actions the model or the game's `cmd` names that still have no button press,
# even after learned_actions.json - logged once per session so they surface
# for learn_actions.py (or a human) to resolve instead of being silently
# dropped by plan_press.
_unknown_actions = UnknownActionLog(source="agent")

AVAILABILITY = {
    "Melee": "ok_melee", "ExMelee": "ok_exmelee",
    "Unique1": "ok_u1", "Unique2": "ok_u2",
    "ExUnique1": "ok_exu1", "ExUnique2": "ok_exu2",
    "Unguardable": "ok_ub",
    "PlusUltra1": "ok_pu1", "PlusUltra2": "ok_pu2",
    "Awaking": "ok_awk",
    "ExtremeAction": "ok_extreme",
    # Escape shares the right-stick click with Rising, so never send it unless
    # the game says the emergency escape is usable right now.
    "Escape": "ok_esc",
}

OFFENCE = {"combo", "attack", "unblock", "pu", "rising", "assist"}


def _cls(rec):
    return act_class((rec or {}).get("act")) if rec and "act" in rec else None


def _hp(rec):
    h = (rec or {}).get("hp")
    return float(h) if isinstance(h, (int, float)) else None


def _dist(rec):
    d = (rec or {}).get("dist")
    return float(d) if isinstance(d, (int, float)) else 1e9


def slow_of(rec):
    """1 if this fighter is frozen in hit-slow / time dilation now, 0 if not,
    None if the sampler does not report it (older sampler, benched fighter)."""
    if not rec:
        return None
    hs, td = rec.get("hslow"), rec.get("tdil")
    if isinstance(td, (int, float)) and 0.0 < td < 0.85:
        return 1
    if hs is None and td is None:
        return None
    return 1 if hs else 0


def slow_edge(me, en):
    """The game's 'slow mode' as an advantage: +1 when the opponent is slowed
    and we are not (after our just guard 0.6 s, counter 0.7 s, escape 0.4 s -
    a free punish), -1 the reverse (do not press into their free turn), 0
    when both or neither are (an ordinary hit-stop, an RPS clash)."""
    a, b = slow_of(me), slow_of(en)
    if a is None or b is None or a == b:
        return 0
    return 1 if b else -1


def update_chain(chain, teams, now=None):
    """Feed one tick of `teams` (sampler.teams output) to a ChainTracker."""
    if chain is None:
        return
    recs = list(teams.get("mine") or []) + list(teams.get("theirs") or [])
    chars = {r.get("key"): r for r in recs if r.get("key")}
    chain.update(chars, [r.get("key") for r in teams.get("mine") or []],
                 [r.get("key") for r in teams.get("theirs") or []],
                 (teams.get("me") or {}).get("key"),
                 (teams.get("enemy") or {}).get("key"), now)


def toward(me, rel_deg=0.0, mag=1.0, en=None):
    """Stick (x, y) pointing rel_deg away from straight-at-the-opponent
    (0 = at them, 180 = away, +90 = circle right). Never assume 'up' is toward
    them: the stick is camera-relative and the camera is behind Player 1, so on
    Player 2 'up' runs away (measured: P2 holding up went 840 -> 1520 units
    from P1)."""
    a = np.deg2rad(D.tcam_of(me, en) + rel_deg)
    return (float(np.sin(a) * mag), float(np.cos(a) * mag))


def stick_rel(me, stick, en=None):
    """Angle of a stick relative to the opponent (0 = toward), or None if neutral."""
    sx, sy = stick
    if abs(sx) + abs(sy) < 0.2:
        return None
    return D.wrap180(float(np.rad2deg(np.arctan2(sx, sy))) - D.tcam_of(me, en))


class FrameTracker:
    """Which stick angle points at the opponent, read from the game itself.

    The stick follows the battle camera, and where that camera sits depends
    on the mode and moves during a versus match, so no fixed rule holds (in
    versus, the side rule's 'toward' ran bots into the arena fence 5000 units
    from their opponent). Every sample where our fighter is holding the stick
    carries the game's own answer: `rside` is IsLeverOnTargetRightSide - is
    the stick to the right of the direction to the target - and `lcam` is the
    stick angle as the game read it. Each one rules out half of the circle;
    a decaying log-likelihood over 5-degree bins keeps the angle that agrees
    with the most recent ones, so it follows the camera as it swings. Pushing
    at the estimate is then a bisection: the answer says which way it is off.

    An earlier version fitted distance changes to stick angles. Homing moves,
    knockback and the opponent walking in all change the distance whatever
    the stick does, so it confirmed whatever it was pushing and could lock
    on 180 degrees out.

    The sign of `rside` was first checked offline and came out backwards; it
    is now from a live stick sweep (see observe). Re-check with that probe,
    not with recordings: the bot pushes nearly one angle, so recorded
    (lcam, rside, distance) cannot tell the two half-planes apart.

    `frame` is the correction on top of the side rule (dataset.tcam_of:
    Player 1 = 0, Player 2 = 180) that toward()/plan_press read via
    rec['_frame']."""
    BIN_DEG = 5.0
    DECAY = 0.97          # per sample: ~30 samples of memory
    P_AGREE = 0.85        # how often an rside reading is taken at face value
    MIN_WEIGHT = 6.0      # samples (decayed) before the estimate is used
    MIN_PUSH = 50.0       # lscale: the stick must really be held

    def __init__(self):
        self.bins = np.arange(-180.0, 180.0, self.BIN_DEG) + self.BIN_DEG / 2
        self.ll = {}                # side -> log-likelihood per bin
        self.weight = {}            # side -> decayed sample count
        self.frames = {}            # side -> correction degrees
        self.updates = 0
        self._ok = np.log(self.P_AGREE)
        self._no = np.log(1.0 - self.P_AGREE)

    def frame_for(self, side):
        return self.frames.get(side, 0.0)

    PLATEAU = 1.0         # log-likelihood below the best still counted as best

    def _center(self, ll):
        """Middle of the best-scoring arc. NOT argmax: the bot pushes at its
        own estimate, so the readings mostly say 'somewhere on this side' and
        a whole half-circle ties - argmax took the tie's first bin, right at
        the edge next to the stick, and the estimate never moved (2026-11-06:
        stuck at -162 for 166 s while 97% of readings said it was wrong; the
        bots ran apart all match). The arc's middle is a real bisection step."""
        near = ll >= ll.max() - self.PLATEAU
        r = np.deg2rad(self.bins[near])
        return float(np.rad2deg(np.arctan2(np.sin(r).sum(), np.cos(r).sum())))

    def observe(self, me, now=None, en=None):
        """Fold in this tick's (lcam, rside) reading for our fighter."""
        lcam, rs = me.get("lcam"), me.get("rside")
        if (not me.get("lon") or (me.get("lscale") or 0.0) < self.MIN_PUSH
                or not isinstance(lcam, (int, float)) or not isinstance(rs, (int, float))):
            return
        side = 1 if me.get("enemy") else 0
        rel = (lcam - self.bins + 180.0) % 360.0 - 180.0     # stick relative to each candidate
        # rside=1 <=> the stick is at a LARGER lcam than the target direction.
        # Measured live 2026-11-06 (scratchpad probe_dir.py: stick swept in
        # 30-degree steps while running, enemy idle): P1 closed fastest at
        # lcam ~+10 and read rside=1 for lcam +23..+157; P2 closed at ~-165
        # and read rside=1 for -157..-23. The old sign had it backwards, so
        # the estimate settled on the angle pointing AWAY (bots 5000+ apart).
        agree = (rel > 0) if rs > 0.5 else (rel < 0)
        ll = self.ll.get(side)
        ll = np.zeros_like(self.bins) if ll is None else ll * self.DECAY
        self.ll[side] = ll + np.where(agree, self._ok, self._no)
        self.weight[side] = self.weight.get(side, 0.0) * self.DECAY + 1.0
        if self.weight[side] < self.MIN_WEIGHT:
            return
        best = self._center(self.ll[side])
        base = 180.0 if side else 0.0
        new = D.wrap180(best - base)
        old = self.frames.get(side)
        self.frames[side] = new
        self.updates += 1
        if old is None or abs(D.wrap180(new - old)) > 45:
            print(f"  [frame] {'P2' if side else 'P1'}: the game says stick angle {best:+.0f} "
                  f"points at the opponent (was assuming {base + (old or 0.0):+.0f})", flush=True)

    def pushed(self, me, en, stick, now):
        """Kept for callers: the game's own reading (observe) already has the stick."""
        return

def pinned(rec):
    """Being hit right now. `act` when available; the old flags otherwise."""
    c = _cls(rec)
    if c is not None:
        return c == "damage"
    return bool((rec or {}).get("stun") or (rec or {}).get("kb")
                or (rec or {}).get("grab"))


CRITIC_TAKEN_COST = 1.2     # train.TAKEN_COST
CRITIC_LONG_WEIGHT = 0.6    # train.LONG_WEIGHT
# The critic is a tree model over a one-hot action column: for actions it saw
# only a few dozen times it extrapolates (2026-11-09 review: Awaking had 34
# training samples and was the critic's top pick 16% of the time). Actions
# with fewer training decisions than this are left at the policy's odds.
CRITIC_MIN_N = 100
# Emergency escape / R3: only meaningful while being hit. In neutral R3 is
# Rising (when the gauge is full) or nothing - ok_esc reads 1 all the time.
HIT_ONLY = {"Escape", "DamageEscape"}

# Toga's blood transform (copied an opponent): only the copy's jabs, counter
# and unblockable work. Everything quirk/meter-based is off; movement, guard
# and dodge are not listed and stay available.
TOGA_BLOOD_LOCKED = {"ExMelee", "Unique1", "ExUnique1", "Unique2", "ExUnique2",
                     "UniqueCharge1", "UniqueCharge2", "PlusUltra1",
                     "PlusUltra2", "Awaking", "ExtremeAction"} | {
                     a for a in ACTION_MAP if a.startswith("ExUnique2Follow")}


class Agent:
    def __init__(self, path="models/policy.pkl", flip_x=False, deadzone=0.18,
                 approach_dist=700.0):
        with Path(path).open("rb") as fh:
            p = pickle.load(fh)
        # One-row predictions: skip the thread pools the models were
        # trained with (model_utils.tune_for_live; same outputs).
        self.action = tune_for_live(p["action"])
        self.movement = tune_for_live(p["movement"])
        self.actions = p["actions"]
        self.chars = p["chars"]
        self.flip_x = flip_x
        self.deadzone = deadzone
        # Past this distance a neutral stick is wasted time: walk in. Humans
        # push the stick 12-31% of ticks; the bot was down at 6%.
        self.approach_dist = approach_dist
        self.n_features = p.get("n_features")
        # Action-value critic (train.py critic_head): how well each action has
        # gone in situations like this. Absent in older policies.
        self.critic = tune_for_live(p.get("critic"))
        self.critic_scale = p.get("critic_scale") or 1.0
        # {label: training decisions}; absent in older policies (no skipping).
        self.critic_counts = p.get("critic_counts") or {}
        self.critic_beta = 1.0        # 0 = ignore the critic
        self.critic_topk = 6
        self.critic_used = 0
        # Discovery: chance per decision (at most once per EXPLORE_GAP_S) of
        # trying something other than the top pick. Only for self-play, where
        # two copies of the same policy otherwise repeat each other's habits
        # forever and the recordings show nothing new.
        self.explore = 0.0
        self.explored = collections.Counter()
        # Sparring boss (play2 --boss): blend in a second policy - one fitted
        # on human recordings only - so the boss leans on how people play.
        self.lean = None
        self.lean_w = 0.0
        self._lean_idx = None
        self._last_explore = 0.0
        # Combo history per fighter (the `prev_cmd` feature). Keyed by
        # character, so one Agent can serve both sides of a self-play match.
        self._hist = {}
        # Recent-tick history for the HISTORY_K spacing/timing features,
        # reset whenever a different fighter is on point - a tag partner does
        # not inherit the outgoing fighter's approach, and training never
        # lets one streak's history bleed into another's either.
        self._tick_hist = {}
        self._last_key = None
        self._pacing = {}             # per fighter, see _pace
        self._stuck = {}              # per fighter, see _unstick
        self.frames = FrameTracker()
        self._opp = {}                # our side -> oppmodel.OpponentModel
        self._serialize()

    def _serialize(self):
        """Predict on one thread: live play asks for ONE row at a time, and a
        thread pool costs more than the trees it parallelises."""
        heads = [self.action, self.movement]
        if isinstance(self.critic, dict):
            heads += list(self.critic.values())
        elif self.critic is not None:
            heads.append(self.critic)
        for head in heads:
            est = head[-1] if hasattr(head, "steps") else head
            for e in (est, getattr(est, "clf", None), getattr(est, "estimator", None)):
                if e is None:
                    continue
                # xgboost: n_jobs via set_params reaches the booster; sklearn: attribute
                try:
                    if hasattr(e, "get_booster"):
                        e.set_params(n_jobs=1)
                        e.get_booster().set_param({"nthread": 1})
                    elif hasattr(e, "n_jobs"):
                        e.n_jobs = 1
                except Exception:
                    pass

    def _fit_features(self, x):
        """A policy trained before newer features (appended last, see
        dataset.EXTRA_N) simply does not see them."""
        want = self.n_features
        if want is None:
            want = getattr(self.action, "n_features_in_", None)
        if want is not None and want < x.shape[1] <= want + D.EXTRA_N:
            return x[:, :want]
        return x

    def _check_features(self, x):
        want = self.n_features
        if want is None:
            want = getattr(self.action, "n_features_in_", None)
        if want is not None and x.shape[1] != want:
            raise RuntimeError(
                f"policy expects {want} features but the current code builds "
                f"{x.shape[1]}. The feature set changed since it was trained - "
                f"retrain with:  python train.py --save models"
            )

    @staticmethod
    def tick_from(snap, roster=None):
        """Build the same dict shape the recorder writes, from a live snapshot."""
        t = sampler.teams(snap, roster=roster)
        chars = dict(sampler.dedupe(snap))
        me, en = t["me"], t["enemy"]
        return {
            "chars": chars,
            "me": (me or {}).get("key"),
            "enemy": (en or {}).get("key"),
            "mine": [c.get("key") for c in t["mine"]],
            "theirs": [c.get("key") for c in t["theirs"]],
            # Sides were decided live; never let the offline repair second-
            # guess them.
            "selfplay": True,
        }, t

    def _prev_cmd(self, key, cmd):
        h = self._hist.setdefault(key, [None, None])      # [prev, last]
        if cmd != h[1]:
            h[0], h[1] = h[1], cmd
        return h[0]

    def decide(self, snap, roster=None):
        """(action_name, (stick_x, stick_y), teams) for one snapshot."""
        tick, teams = self.tick_from(snap, roster)
        if not tick["me"]:
            return None, (0.0, 0.0), teams
        me = teams["me"] or {}
        key = tick["me"]
        en0 = teams.get("enemy") or {}
        me["_frame"] = self.frames.frame_for(1 if me.get("enemy") else 0)
        self.frames.observe(me, clock.now(), en0)
        prev = self._prev_cmd(key, me.get("cmd"))
        if key != self._last_key:
            self._tick_hist[key] = collections.deque(maxlen=D.HISTORY_K)
            self._last_key = key
        hist_buf = self._tick_hist[key]

        from .oppmodel import OpponentModel
        om = self._opp.setdefault(1 if me.get("enemy") else 0, OpponentModel())
        om.update(me, teams.get("enemy") or {}, clock.now())
        x = D.featurize(tick, self.actions, self.chars, prev,
                        list(hist_buf), om.feats()).reshape(1, -1)
        x = self._fit_features(x)
        self._check_features(x)
        lean = self._lean_probs(tick, prev, list(hist_buf), om.feats())
        hist_buf.append(tick)

        probs = (self.action.predict_proba(x)[0]
                 if hasattr(self.action, "predict_proba") else None)
        if probs is not None and lean is not None:
            probs = (1.0 - self.lean_w) * probs + self.lean_w * lean
        if probs is not None:
            probs = self._rerank(probs, key, me, teams.get("enemy") or {})
            probs = self._critique(probs, x, me)
            idx = int(np.argmax(probs))
        else:
            idx = int(self.action.predict(x)[0])
        name = self.actions[idx] if 0 <= idx < len(self.actions) else None

        if name in QUIRK_ACTIONS and me.get("erased"):
            name = self._best_non_quirk(probs)
        elif not self._available(name, me):
            name = self._best_available(probs, me) or name

        if self.explore > 0 and probs is not None and not pinned(me):
            name = self._explore(probs, me, teams.get("enemy") or {}, name, key)

        sin_a, cos_a, scale = self.movement.predict(x)[0]
        mag = float(np.clip(scale, 0.0, 1.0))
        # The movement head predicts an angle relative to the opponent (older
        # policies learned camera angles from Player 1's side, where the two
        # nearly coincide); turn it into the camera-relative stick.
        rel = float(np.rad2deg(np.arctan2(sin_a, cos_a)))
        en_rec = teams.get("enemy") or {}
        sx, sy = toward(me, rel, mag, en_rec)
        if self.flip_x:
            sx = -sx
        if mag < self.deadzone:
            sx = sy = 0.0
            if _dist(me) > self.approach_dist and not pinned(me):
                sx, sy = toward(me, en=en_rec)

        # "Wait" (hold): no new press, and keep the movement head's stick
        # unless the pacing rules below decide on a dash of their own.
        waiting = name == "Wait"
        model_stick = (sx, sy)
        if waiting:
            name = None
        name, (sx, sy) = self._pace(tick["me"], me, teams.get("enemy") or {},
                                    name, (sx, sy))
        name, (sx, sy) = self._neutral(tick["me"], me, teams.get("enemy") or {},
                                       name, (sx, sy))
        if waiting and name is None:
            sx, sy = model_stick
        jab = self._jg_jab(key, me)
        if jab:
            name = jab
        self.frames.pushed(me, teams.get("enemy") or {}, (float(sx), float(sy)), clock.now())
        return name, (float(sx), float(sy)), teams

    def _lean_probs(self, tick, prev, hist, omf):
        """The lean policy's action probabilities in THIS policy's action
        order (its own vocabulary and features may differ), or None."""
        lp = self.lean
        if lp is None or self.lean_w <= 0 or not hasattr(lp.action, "predict_proba"):
            return None
        try:
            x = D.featurize(tick, lp.actions, lp.chars, prev, hist, omf).reshape(1, -1)
            x = lp._fit_features(x)
            p = lp.action.predict_proba(x)[0]
        except Exception:
            return None
        if self._lean_idx is None:
            where = {a: i for i, a in enumerate(lp.actions)}
            self._lean_idx = [where.get(a) for a in self.actions]
        out = np.array([p[j] if j is not None and j < len(p) else 0.0
                        for j in self._lean_idx])
        t = out.sum()
        return out / t if t > 0 else None

    EXPLORE_GAP_S = 0.9

    def _explore(self, probs, me, en, name, key):
        """Occasionally swap the pick for a sensible alternative: mapped,
        available, not a defensive filler, and an attack only if it has a real
        chance to reach from here. Rarely-tried actions are favoured, so over
        a long run the bots cover the whole move list instead of re-playing
        the same three buttons."""
        from . import moveinfo
        from .roster import base_name
        now = clock.now()
        if now - self._last_explore < self.EXPLORE_GAP_S or random.random() >= self.explore:
            return name
        mi = moveinfo.get()
        char = move_char(me, key)
        dist = _dist(me)
        cands, ws = [], []
        for i, a in enumerate(self.actions):
            if a == name or a not in ACTION_MAP or a in ("Guard", "JustGuard", "None", "Wait"):
                continue
            if not self._available(a, me) or (a in QUIRK_ACTIONS and me.get("erased")):
                continue
            if a in moveinfo.REACH_LABELS and mi:
                ph = self._move_worth(mi, char, a, dist)
                if ph is not None and ph < 0.15:
                    continue
            cands.append(a)
            # Novelty per character: what THIS fighter has rarely tried
            # weighs most, so every character's move list gets covered.
            ws.append((float(probs[i]) + 0.02) / (1.0 + self.explored[(char, a)]) ** 1.5)
        if not cands:
            return name
        pick = random.choices(cands, weights=ws)[0]
        self._last_explore = now
        self.explored[(char, pick)] += 1
        return pick

    def _critique(self, probs, x, me):
        """Shift the policy's top candidates by the critic's estimate of how
        each would go from here: p * exp(beta * advantage / scale). Only the
        top-k mapped, available actions are scored - the critic is trusted to
        choose among things the policy already considers, not to invent moves
        it has rarely seen tried."""
        if self.critic is None or self.critic_beta <= 0:
            return probs
        counts = self.critic_counts
        order = [i for i in np.argsort(-probs)
                 if self.actions[i] in ACTION_MAP and self._available(self.actions[i], me)
                 and (not counts or counts.get(self.actions[i], 0) >= CRITIC_MIN_N)]
        cand = order[:self.critic_topk]
        if len(cand) < 2:
            return probs
        n = len(self.actions)
        z = np.zeros((len(cand), x.shape[1] + n), dtype=np.float32)
        z[:, :x.shape[1]] = x[0]
        for r, i in enumerate(cand):
            z[r, x.shape[1] + i] = 1.0
        try:
            if isinstance(self.critic, dict):
                q = (self.critic["dealt"].predict(z)
                     - CRITIC_TAKEN_COST * self.critic["taken"].predict(z))
                if "dealt_long" in self.critic:
                    q = q + CRITIC_LONG_WEIGHT * (self.critic["dealt_long"].predict(z)
                                                  - CRITIC_TAKEN_COST * self.critic["taken_long"].predict(z))
            else:
                q = self.critic.predict(z)
            q = np.asarray(q, dtype=np.float64)
        except Exception:
            return probs
        adv = (q - q.mean()) / max(self.critic_scale, 1e-6)
        p = np.array(probs, dtype=np.float64)
        for r, i in enumerate(cand):
            p[i] *= float(np.exp(np.clip(self.critic_beta * adv[r], -4, 4)))
        self.critic_used += 1
        return p / p.sum()

    # Move knowledge ----------------------------------------------------------
    REACH_POW = 1.5        # how hard an out-of-range attack is marked down
    REACH_REF = 0.45       # a move at this worth (moveinfo.worth) is left alone
    PUNISH_S = 0.7         # after a just guard, attack for this long
    LED_CREDIT = 0.5       # share of "set up damage without landing itself"
    SAFE_CREDIT = 0.5      # share of a safe whiff (not punished) counted as success
    SAFE_MAX_DIST = 900.0  # beyond this a safe whiff is just a whiff - no pressure

    def _move_worth(self, mi, char, label, dist):
        """A move's success rate from here (moveinfo.worth). A hit counts in
        full, inside the move's own window - so a slow projectile or a Plus
        Ultra cinematic is not a whiff. A move that led to damage it did not
        deal itself (a setup like Yaoyorozu's Q2) earns LED_CREDIT. Up close a
        whiff that is not punished still keeps pressure on (Bakugo's TQ1) and
        earns SAFE_CREDIT; from far away it does not, or moves thrown from
        across the stage would look safe and get spammed."""
        near = isinstance(dist, (int, float)) and dist <= self.SAFE_MAX_DIST
        return mi.worth(char, label, dist, self.LED_CREDIT,
                        self.SAFE_CREDIT if near else 0.0)

    def _rerank(self, probs, key, me, en):
        """Re-weight the policy's action probabilities by what is known about
        the moves (ajbot.moveinfo). The cloned policy always has a move to
        offer, so at range it offered Bakugo's Q2 - a move that connects
        0.7 of the time up close and 0.2 across the stage. Attacks that
        will not reach from here are marked down, dashes toward an opponent
        who is not attacking are marked up, and just after a just guard the
        attacks are marked up: that opening is the whole point of guarding."""
        from . import moveinfo
        from .roster import base_name
        mi = moveinfo.get()
        now = clock.now()
        jg = me.get("ev_jg")
        st = self._pacing.setdefault(("jg", key), {"n": jg or 0, "until": 0.0})
        if isinstance(jg, (int, float)):
            if jg > st["n"]:
                st["until"] = now + self.PUNISH_S
            st["n"] = jg
        edge = slow_edge(me, en)
        punishing = now < st["until"] or edge > 0
        dist = _dist(me)
        char = move_char(me, key)
        en_committed = _cls(en) in OFFENCE
        # A red on its way: guarding it is a loss (and a counter loses to it),
        # so those are marked right down; hitting first or moving is the answer.
        red_near = D.is_red(en) and dist <= self.RED_REACH
        p = np.array(probs, dtype=np.float64)
        for i, a in enumerate(self.actions):
            if red_near and a in ("Guard", "JustGuard", "CounterAttack"):
                p[i] *= 0.05
                continue
            if a in moveinfo.REACH_LABELS and mi:
                ph = self._move_worth(mi, char, a, dist)
                if ph is not None:
                    f = min(1.3, max(0.1, ph / self.REACH_REF)) ** self.REACH_POW
                    p[i] *= f * (3.0 if punishing else 1.0)
            elif punishing and a == "CounterAttack":
                p[i] *= 2.0
            elif a in ("DashForward", "Dash") and dist > 600 and not en_committed:
                p[i] *= 1.8
            elif a == "DashForward" and edge > 0 and dist > 350:
                p[i] *= 3.0           # frozen opponent out of reach: close in
            if edge < 0 and a in moveinfo.REACH_LABELS:
                p[i] *= 0.4           # we are the frozen one: their turn
        tot = p.sum()
        return p / tot if tot > 0 else probs

    RED_REACH = 700.0      # a red from this close is answered, not guarded

    JAB_S = 1.2            # after our just guard, jab until it comes out (at most)
    JAB_DIST = 450.0       # no moveinfo: the jab reaches from this close
    JAB_MIN_WORTH = 0.15   # moveinfo: the jab connects at least this often from here

    def _jg_jab(self, key, me):
        """"Melee" while a just guard's punish is open, else None.

        Raising attacks in _rerank was not enough: the policy still picked a
        quirk move (Bakugo's Q1) that does not cancel the just-guard animation
        and came out after the window, and the Lv5 CPU hit us first. The jab
        is the fastest button and every route starts from it, so press it -
        every tick, since a press during the just-guard animation can be
        eaten - until our own attack is out (route / combo book take over
        from there), we are hit, or JAB_S passes. The animation alone can
        last 0.9 s, so the window runs from the just guard, not to a fixed
        0.7 s."""
        jg = me.get("ev_jg")
        st = self._pacing.setdefault(("jab", key), {"n": jg or 0, "until": 0.0})
        now = clock.now()
        if isinstance(jg, (int, float)):
            if jg > st["n"]:
                st["until"] = now + self.JAB_S
            st["n"] = jg
        if now >= st["until"]:
            return None
        cls = _cls(me)
        if cls in OFFENCE or cls == "damage":
            st["until"] = 0.0         # the jab (or something) is out, or too late
            return None
        if "Melee" not in ACTION_MAP or not self._available("Melee", me):
            return None
        from . import moveinfo
        mi = moveinfo.get()
        dist = _dist(me)
        ph = self._move_worth(mi, move_char(me, key), "Melee", dist) if mi else None
        if (ph is None and dist > self.JAB_DIST) or (ph is not None and ph < self.JAB_MIN_WORTH):
            return None
        return "Melee"

    # Pacing ------------------------------------------------------------------
    REPRESS_S = 0.6        # same move again within this, while it still plays
    STALL_S = 2.5          # no hit either way this long, apart = stalemate
    STALL_DIST = 450.0

    def _pace(self, key, me, en, name, stick):
        """Stop the bot machine-gunning one button, and break stalemates.

        The action head is trained on the moments a player STARTED something,
        so it always has a move to suggest - it never learned "let this one
        finish". Pressing its pick every decision meant the same quirk skill
        ten times a second, rooting the fighter in place; two bots doing that
        from range just stood apart spamming Y.
        """
        now = clock.now()
        st = self._pacing.setdefault(key, {"last": None, "t": 0.0, "hp": None,
                                           "quiet_since": now, "dash_t": 0.0})
        # Anything landing either way resets the stalemate clock.
        hp = (me.get("hp"), en.get("hp"))
        if st["hp"] is not None and hp != st["hp"]:
            st["quiet_since"] = now
        st["hp"] = hp
        cls = _cls(me)
        busy = cls in OFFENCE and (me.get("act_n") or 0) < 12

        # Same move again while the last one is still playing: let it finish.
        if name and name == st["last"] and now - st["t"] < self.REPRESS_S and busy:
            name = None

        # Stalemate: nobody has hit anybody for a while and we are apart -
        # walk in (and dash now and then) instead of trading projectiles.
        if (now - st["quiet_since"] >= self.STALL_S and _dist(me) > self.STALL_DIST
                and not pinned(me)):
            stick = toward(me, en=en)
            if now - st["dash_t"] >= 1.2:
                st["dash_t"] = now
                name = "Dash"
            elif name in QUIRK_ACTIONS or name in ("Melee", "ExMelee"):
                name = None           # close the distance first

        if name:
            st["last"], st["t"] = name, now
        return name, stick

    NEUTRAL_DIST = 320.0    # closer than this the opponent is already in play
    FAR_DIST = 900.0        # beyond this, attacks must earn their place
    FAR_MIN_HIT = 0.2       # measured hit rate (moveinfo) an attack needs from there
    FAR_HARD = 3000.0       # beyond this, only projectiles / long homing (framedata)
    FAR_CHECK = {"Melee", "ExMelee", "Unique1", "Unique2", "ExUnique1", "ExUnique2", "Unguardable"}
    STUCK_DIST = 1500.0     # only this far apart can we be stuck behind something
    STUCK_S = 2.5           # this long without closing STUCK_GAIN = stuck
    STUCK_GAIN = 150.0
    UNSTICK_S = 1.6         # then go around (and over) for this long
    UNSTICK_ANGLE = 70.0

    def _unstick(self, key, me, en, name, stick):
        """No path finding: when pushing straight at a far opponent stops
        closing the distance, something is in the way. In the 2026-10-24 run
        Nighteye dashed into stage geometry 4600 units from a sniping Lady
        Nagant for half of an 8-minute match. Go around it at an angle,
        alternating sides each attempt, and jump. Returns (name, stick) while
        going around, else None."""
        now = clock.now()
        dist = _dist(me)
        st = self._stuck.get(key)
        if st is None or now - st.get("seen", now) > 1.0:
            st = self._stuck[key] = {"t0": now, "d0": dist, "side": st["side"] if st else 1.0}
        st["seen"] = now
        if now < st.get("until", 0.0):
            stick = toward(me, self.UNSTICK_ANGLE * st["side"], en=en)
            if now - st.get("jumped", 0.0) > 0.8 and "Jump" in ACTION_MAP:
                st["jumped"] = now
                return "Jump", stick
            # DashForward would force the stick straight back at the obstacle
            return (None if name in FORCED_STICK else name), stick
        if dist < st["d0"] - self.STUCK_GAIN:
            st["t0"], st["d0"] = now, dist          # getting there
        elif now - st["t0"] > self.STUCK_S:
            st["side"] = -st["side"]
            st["until"] = now + self.UNSTICK_S
            st["t0"], st["d0"] = now + self.UNSTICK_S, dist
        return None

    ZONE_DIST = 450.0       # beyond this an attacking opponent is zoning us
    ZONE_ANGLE = 55.0       # close on a shooter at this angle, not head-on

    def _off_wall(self, key, me, en, stick):
        """Touching a wall (the arena fence): there is no position or stage
        size to read, only IsTouchWall, so get off it - toward the opponent
        at an angle (a corner is beside us as often as behind us), unless
        the stick already points that way."""
        rel = stick_rel(me, stick, en)
        if rel is not None and abs(rel) <= 60:
            return stick
        st = self._pacing.setdefault(key, {})
        side = st.get("wall_side", 1.0)
        now = time.monotonic()
        if now - st.get("wall_t", 0.0) > 2.0:
            side = -side                       # a new wall contact: try the other way
            st["wall_side"] = side
        st["wall_t"] = now
        return toward(me, 40.0 * side, en=en)

    def _under_fire(self, key, me, en, stick):
        """The opponent is attacking. Up close that is defence's business,
        but from range it is a zoner (Nejire, 2026-11-10): the movement head
        learned to back away from shots, which walked the bot into the corner
        to be spammed. Never retreat from a shooter - close in at an angle,
        which also sidesteps straight shots - and get off a wall."""
        if _cls(me) in OFFENCE:
            return stick
        dist = _dist(me)
        if me.get("twal") and dist >= self.NEUTRAL_DIST:
            return self._off_wall(key, me, en, stick)
        if dist < self.ZONE_DIST:
            return stick
        rel = stick_rel(me, stick, en)
        if rel is not None and abs(rel) <= 70:
            return stick                       # already going in
        st = self._pacing.setdefault(key, {})
        now = time.monotonic()
        if now - st.get("zone_t", 0.0) > 1.5:
            st["zone_side"] = -st.get("zone_side", 1.0)
        st["zone_t"] = now
        return toward(me, self.ZONE_ANGLE * st.get("zone_side", 1.0), en=en)

    def _neutral(self, key, me, en, name, stick):
        """The policy is cloned from moments something HAPPENED, so in quiet
        neutral it has nothing to say and predicted Guard/None, or retreated
        (learned from bots that both backed away). While the opponent is not
        attacking and is out of reach: never stand, never back off - walk in,
        dash now and then."""
        if pinned(me) or not en:
            return name, stick
        if _cls(en) in OFFENCE:
            return name, self._under_fire(key, me, en, stick)
        dist = _dist(me)
        if me.get("twal") and dist >= self.NEUTRAL_DIST and _cls(me) not in OFFENCE:
            stick = self._off_wall(key, me, en, stick)
        if dist < self.NEUTRAL_DIST:
            return name, stick
        # An attack that cannot reach from here is replaced by closing in.
        # The reach reranking only lowers such attacks relative to each other;
        # in the 2026-10-24 self-play run the bots sat 4500-6600 apart for whole
        # matches pressing Melee / Unique1 into empty space.
        if name in self.FAR_CHECK and dist > self.FAR_DIST:
            from . import moveinfo
            from .roster import base_name
            mi = moveinfo.get()
            ph = self._move_worth(mi, move_char(me), name, dist) if mi else None
            if ph is not None and ph < self.FAR_MIN_HIT:
                name = None
        # The measured table lumps everything past 2000 into one bin, so it
        # cleared Unique1 from 5000 away; pressed from there it only starts
        # an AirChase that does not close the distance, and both bots did it
        # for minutes. The game's own data is a hard limit: from this far only
        # a projectile, or a move that homes at least that far, can land.
        if name in self.FAR_CHECK and dist > self.FAR_HARD:
            from . import framedata
            p = framedata.get().move(me.get("key"), name, not me.get("ground"))
            if p is None or not (p.get("proj") or (p.get("reach") or 0.0) >= 0.9 * dist):
                name = None
        rel = stick_rel(me, stick, en)
        if rel is not None and abs(rel) > 105:      # backing away for no reason
            stick = toward(me, 45.0 if rel > 0 else -45.0, en=en)   # circle in instead
        if name in (None, "None", "Guard", "JustGuard") and _cls(me) not in OFFENCE:
            st = self._pacing.setdefault(key, {})
            now = clock.now()
            stick = toward(me, en=en)
            if dist > 700 and now - st.get("nd", 0.0) > 1.4:
                st["nd"] = now
                name = "DashForward" if "DashForward" in ACTION_MAP else "Dash"
            else:
                name = None
        if dist > self.STUCK_DIST:
            going_around = self._unstick(key, me, en, name, stick)
            if going_around is not None:
                name, stick = going_around
        return name, stick

    @staticmethod
    def _available(name, me):
        # Escapes only while being hit - and Escape still needs ok_esc below
        # (it shares the right-stick click with Rising).
        if name in HIT_ONLY and not pinned(me):
            return False
        # Aizawa's Erasure (status "Erase", 8 s): quirk buttons do nothing.
        # Checked here so the route follower and combo book skip them too.
        if name in QUIRK_ACTIONS and me.get("erased"):
            return False
        # Toga transformed: her own state's ok_* flags read 0 most of the
        # time then (measured: ok_melee 0.47 in neutral, ok_u2 0.0), so they
        # cannot gate. The blood copy only has the copied fighter's jabs,
        # counter and unblockable; the Plus Ultra copy (her partner) has
        # that partner's whole moveset, Plus Ultra included.
        tm = toga_mode(me)
        if tm == "blood":
            return name not in TOGA_BLOOD_LOCKED
        if tm == "pu":
            return name != "Escape" or bool(me.get("ok_esc"))
        field = AVAILABILITY.get(name)
        if field is None or field not in me:
            return True
        return bool(me[field])

    def _best_available(self, probs, me):
        if probs is None:
            return None
        for i in sorted(range(len(probs)), key=lambda i: -probs[i]):
            a = self.actions[i]
            if a in ACTION_MAP and self._available(a, me):
                if not (a in QUIRK_ACTIONS and me.get("erased")):
                    return a
        return None

    def _best_non_quirk(self, probs):
        if probs is not None:
            for i in sorted(range(len(probs)), key=lambda i: -probs[i]):
                a = self.actions[i]
                if a not in QUIRK_ACTIONS and a in ACTION_MAP:
                    return a
        for a in ERASED_FALLBACK:
            if a in self.actions:
                return a
        return None


# ------------------------------------------------------------------ defence

class DefensePolicy:
    """Answers an incoming attack, and LEARNS which answer pays per situation.

    When the opponent starts an attack that is a threat from where we stand,
    one response is chosen and committed to for that attack:

      jgE/jgM/jgL  just guard, the tap timed onto the 25th / 50th / 75th
                   percentile of when THIS character's THIS move, started at
                   THIS distance, has touched someone (models/threat.json,
                   mined from every recording). The window is 0.192 s after
                   the press and a second tap inside 1.0 s does nothing (DB
                   ActionParam ジャストガード), so the tap must be placed, not
                   mashed. Until the tap the fighter stands still (holding
                   Guard first would itself be the press); after the window
                   it holds Guard so a late hit is still blocked.
      guard        hold Guard for as long as the attack lasts
      counter      Guard + Q1 just before contact - armoured Counter Attack /
                   Counter Crash
      dodge        Guard + sideways stick just before contact (a timed dodge
                   is a Just Step)
      attack       a Target Combo at once, which beats an Unblockable
      none         leave it to the model

    Which attacks are threats: the measured chance the move connects from
    this distance (threat.p_contact). The old fixed 450-unit cut-off missed
    every dash-in string and projectile: in the friend matches 58 of 140
    Target Combos started at 450-900 landed and were never answered. A
    projectile is its own situation ('proj'), and a move the game marks
    Unguardable (DamageParam, models/movedata.json) is answered as one even
    when its name does not say so.

    Each choice is scored ~1.2 s later: +3 per just guard, +2 per just step,
    +3 when a counter is followed by the opponent in hitstun, +1 per guarded
    hit, +1 if the opponent got hit at all, -2 for being guard-broken and
    -10 x health lost.

    Learning is two-layered so it adapts to the PERSON in front of it: the
    long-run table (models/defense.json) is recency-capped at N_CAP tries
    per arm, and a per-match table on top counts double. A human who delays
    their strings stops being answered with the timing that beat the CPU.
    """

    OPTIONS = ("jgE", "jgM", "jgL", "guard", "counter", "dodge", "attack", "none")
    JG = {"jgE": 0, "jgM": 1, "jgL": 2}
    JG_OPTS = frozenset(JG)
    PRIORS = {
        "combo":   {"counter": 1.2, "jgM": 1.0, "jgE": 0.9, "jgL": 0.7,
                    "guard": 0.7, "dodge": 0.2, "attack": -0.5},
        "attack":  {"jgM": 1.0, "jgE": 0.8, "jgL": 0.8, "guard": 0.7, "dodge": 0.4},
        "proj":    {"jgM": 0.9, "guard": 0.8, "dodge": 0.8, "jgE": 0.6, "jgL": 0.6,
                    "counter": -0.6, "attack": -0.8},
        "unblock": {"attack": 1.2, "dodge": 1.0, "counter": -1.5,
                    "guard": -1.5, "jgE": -1.2, "jgM": -1.2, "jgL": -1.2},
        "pu":      {"guard": 0.6, "dodge": 0.6, "jgM": 0.4, "counter": -1.5},
        "rising":  {"guard": 0.5, "dodge": 0.5, "jgM": 0.4},
        "assist":  {"guard": 0.5, "jgM": 0.5, "jgE": 0.4},
    }
    # Fallback when there is no threat table (or the move was never seen):
    # how close an attack must be to be worth answering, per class.
    REACH = {"combo": 450.0, "unblock": 450.0, "assist": 600.0,
             "attack": 450.0, "pu": 900.0, "rising": 700.0, "proj": 1600.0}
    MIN_P = 0.2                 # measured contact chance worth answering
    TRICK_BONUS = 0.0           # OFF: paid for interrupting the counter (see dataset)
    MIN_P_RED = 0.08            # a red: answering costs little, guarding a lot
    PRIOR_N = 3.0
    N_CAP = 30.0                # long-run table: recency cap per arm
    MATCH_W = 2.0               # this match's tries count double
    HORIZON = 1.2
    # A red is scored over longer: its hit is cheap (~7% health) and the real
    # cost is the combo the attacker gets after it, which 1.2 s cut off - so
    # Guard looked free. Only these answers are allowed against one: hit
    # first (a Target Combo beats it), dodge, or leave it to the model (move
    # away, jump...), which is told not to guard it (Agent._rerank).
    HORIZON_RED = 2.0
    RED_OPTS = ("attack", "dodge", "none")
    # Tap this long before the expected contact. The threat table measures
    # start->touch in OBSERVED time, so the sample latency cancels, but the
    # touch is only seen on the tick after it lands (~half a tick late) and
    # the pad press reaches the game a frame or two after we send it; the
    # 0.192 s window should open just before the hit, not on it. Replaying the
    # pro matches: 0.07 put the median contact 0.29 s after the tap.
    JG_LEAD = 0.13
    PRE_LEAD = 0.16             # counter / dodge: this long before
    TICK = 0.055                # one decision (~18 Hz)
    PUNISH_GAP = 0.25           # a projectile's thrower still stuck this long = go in
    ZONE_DIST = 450.0           # a shot from beyond this is never answered by attacking
    VOLLEY_MAX = 2.5            # longest a ranged answer is held
    SHOT_GUARD_S = 0.9          # guard this long after the enemy's shot count rises
    SHOT_MAX_DIST = 2600.0
    JG_MOVE_LEAD = 0.35         # stop moving this long before a timed tap on a shot

    def __init__(self, path="models/defense.json", epsilon=0.12, learn=True,
                 max_start=8, assist=False):
        self.path = Path(path) if path else None
        # Guard assist (the sparring boss): answer every attack that can
        # reach at all, even ones already well under way, always with the
        # best-known answer - never 'none', no exploration, and it leaves the
        # shared table alone (its own scores would be inflated by the assist).
        self.assist = assist
        if assist:
            epsilon, learn, max_start = 0.0, False, 20
        self.epsilon = epsilon
        self.learn = learn
        # Only react to an attack we saw START (act_n small); one already
        # deep in its animation is too late to time.
        self.max_start = max_start
        self.q = {}                      # ctx -> {opt: [sum, n]}  (long run)
        self.qm = {}                     # the same, this match only
        self.active = None               # the commitment for the current attack
        self.pending = []                # choices waiting to be scored
        self.env = Envelope()
        self.stats = {"chosen": {}, "jg": 0, "js": 0, "counter_hit": 0, "gok": 0,
                      "gbrk": 0, "scored": 0, "approach": 0}
        self._last_en = (None, None, None)
        self._updates = 0
        self._approached = None
        self._en_start = None            # when the opponent's current act began
        self._punish_until = 0.0
        self._shots = (None, None)       # (enemy key, proj_n) last tick
        self._shot_t = -1e9              # when that count last rose
        self._red_t = -1e9               # last time the opponent was mid a red
        self.warned_no_act = False
        self._load()

    # ---- persistence ----
    def _load(self):
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            q = raw.get("q", {}) if isinstance(raw, dict) else {}
            # Version 1 tables had jg0..jg3 (tap N decisions after the start)
            # and counts in the thousands from CPU play; keep what still means
            # the same thing, capped so it can move again.
            ver = raw.get("version", 1) if isinstance(raw, dict) else 1
            self.q = {}
            for ctx, arms in q.items():
                # Version 3 (2026-11-13) scores reds differently: caught by
                # one costs caught_red, over HORIZON_RED, and only RED_OPTS
                # may answer. Older red rows learned that guarding was free.
                if ver < 3 and ctx.split("|", 1)[0] == "unblock":
                    continue
                for opt, (s, n) in arms.items():
                    if opt not in self.OPTIONS or n <= 0:
                        continue
                    if n > self.N_CAP:
                        s, n = s * self.N_CAP / n, self.N_CAP
                    self.q.setdefault(ctx, {})[opt] = [s, n]

    def save(self):
        if not (self.path and self.learn):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.path, json.dumps({"version": 3, "q": self.q}, indent=1))
        except OSError:
            pass

    def new_match(self):
        """A new opponent (or the same one, freshly): forget the per-match layer."""
        self.qm = {}
        self.active = None
        self.pending = []
        self._approached = None

    # ---- bandit ----
    def _ctx(self, cls, dist, hp=None):
        band = "near" if dist < 250 else "mid" if dist < 600 else "far"
        # Low health is its own situation: a counter that is a good gamble at
        # full health is how a fighter at 20% dies.
        low = "|low" if isinstance(hp, (int, float)) and hp < D.DANGER_HP else ""
        return f"{cls}|{band}{low}"

    def _value(self, ctx, cls, opt):
        s, n = self.q.get(ctx, {}).get(opt, (0.0, 0))
        sm, nm = self.qm.get(ctx, {}).get(opt, (0.0, 0))
        prior = self.PRIORS.get(cls, {}).get(opt, 0.0)
        w = self.MATCH_W
        return (s + w * sm + prior * self.PRIOR_N) / (n + w * nm + self.PRIOR_N), n + nm

    def _choose(self, ctx, cls, allowed):
        if self.learn and random.random() < self.epsilon:
            return random.choice(allowed)
        total = sum(n for _, n in self.q.get(ctx, {}).values()) \
            + sum(n for _, n in self.qm.get(ctx, {}).values()) + 1
        best, bestv = "none", -1e9
        for opt in allowed:
            v, n = self._value(ctx, cls, opt)
            v += 0.3 * math.sqrt(math.log(total + 1) / (n + 1))   # UCB bonus
            if v > bestv:
                best, bestv = opt, v
        return best

    def _update(self, ctx, opt, r):
        arm = self.q.setdefault(ctx, {}).setdefault(opt, [0.0, 0])
        if arm[1] >= self.N_CAP:
            # Recency: the oldest evidence fades as new comes in.
            k = (self.N_CAP - 1.0) / arm[1]
            arm[0] *= k
            arm[1] *= k
        arm[0] += float(r)
        arm[1] += 1
        m = self.qm.setdefault(ctx, {}).setdefault(opt, [0.0, 0])
        m[0] += float(r)
        m[1] += 1
        self._updates += 1
        if self._updates % 50 == 0:
            self.save()

    # ---- live ----
    def _events(self, me, en):
        """Monotone event deltas this tick for the current point fighters."""
        mk, ek = me.get("key"), en.get("key")
        return {
            "jg": self.env.delta(mk, "ev_jg", me.get("ev_jg")),
            "js": self.env.delta(mk, "ev_js", me.get("ev_js")),
            "gok": self.env.delta(mk, "ev_gok", me.get("ev_gok")),
            "gbrk": self.env.delta(mk, "ev_gbrk", me.get("ev_gbrk")),
            "ctr": self.env.delta(mk, "ev_ctr", me.get("ev_ctr")),
            "en_hit": self.env.delta(ek, "ev_hit", en.get("ev_hit")),
        }

    def _score(self, ev, me, now):
        """Accumulate outcomes into every pending choice; close finished ones."""
        hp = _hp(me)
        for p in self.pending:
            if me.get("key") != p["me"]:
                p["dead"] = True           # tagged out; the outcome is not ours
                continue
            p["r"] += 3.0 * ev["jg"] + 2.0 * ev["js"] + 1.0 * ev["gok"] - 2.0 * ev["gbrk"]
            if (p["opt"] == "counter" and not p.get("trick")
                    and me.get("act") == "AttackTagComboConfirm"):
                # The counter trick: the counter turned into a Tag Combo
                # (dataset.REWARD["counter_trick"] pays it in training too).
                p["r"] += self.TRICK_BONUS
                p["trick"] = True
                self.stats["counter_trick"] = self.stats.get("counter_trick", 0) + 1
            if ev["en_hit"]:
                p["r"] += 1.0 if not p["hit"] else 0.0
                p["hit"] = True
                if p["opt"] == "counter" and not p["ctr_hit"]:
                    p["r"] += 3.0
                    p["ctr_hit"] = True
                    self.stats["counter_hit"] += 1
            if hp is not None:
                if (hp < p["hp_min"] - 0.001 and not p.get("caught")
                        and now - self._red_t <= D.RED_GRACE_S):
                    p["caught"] = True         # the red connected
                p["hp_min"] = min(p["hp_min"], hp)
        self.stats["jg"] += int(ev["jg"])
        self.stats["js"] += int(ev["js"])
        self.stats["gok"] += int(ev["gok"])
        self.stats["gbrk"] += int(ev["gbrk"])

        keep = []
        for p in self.pending:
            if p.get("dead"):
                continue
            if now - p["t"] >= (self.HORIZON_RED if p.get("red") else self.HORIZON):
                lost = max(0.0, p["hp0"] - p["hp_min"]) if p["hp0"] is not None else 0.0
                # Same pricing as training: damage costs more the lower it
                # leaves us, and losing the fighter is the worst outcome.
                cost = 10.0 * lost * D.taken_weight(p["hp_min"])
                if lost <= 0.001 and p["opt"] in ("guard", "dodge") + tuple(self.JG):
                    cost -= 0.4       # the attack passed and cost nothing
                if p.get("caught"):
                    cost += -D.REWARD["caught_red"]
                    self.stats["caught_red"] = self.stats.get("caught_red", 0) + 1
                if p["hp0"] is not None and p["hp0"] > 0.001 and p["hp_min"] <= 0.001:
                    cost += -D.REWARD["our_death"]
                    self.stats["deaths"] = self.stats.get("deaths", 0) + 1
                if self.learn:
                    self._update(p["ctx"], p["opt"], p["r"] - cost)
                self.stats["scored"] += 1
            else:
                keep.append(p)
        self.pending = keep

    def _situation(self, me, en, en_cls, dist):
        """(class, threat?, contact-time quartiles) of the opponent's current
        attack from where we stand."""
        from . import movedata, threat as T
        from .roster import base_name
        act = en.get("act")
        md = movedata.get().move(en.get("key"), act)
        cls = en_cls
        if cls != "unblock" and D.is_red(en):
            cls = "unblock"
        ranged = bool(md and md.get("proj") and (md.get("prj_speed") or 0) > 0
                      and (md.get("reach") or 0) >= 450)
        if ranged and cls == "attack" and dist >= 300:
            cls = "proj"
        th = T.get()
        char = move_char(en)
        p = th.p_contact(char, act, dist) if th else None
        if p is not None:
            is_threat = p >= (0.05 if self.assist
                              else self.MIN_P_RED if cls == "unblock" else self.MIN_P)
        else:
            reach = self.REACH.get(cls)
            if ranged:
                reach = max(reach or 0.0, min(md["reach"], 2500.0))
            is_threat = reach is not None and dist <= reach
        ct = th.contact_time(char, act, dist) if th else None
        if ct is None:
            # No measurement: a projectile's flight time, else a dash-in that
            # takes longer the further it has to come.
            if ranged:
                fl = dist / max(md["prj_speed"], 500.0)
                ct = (0.15 + fl * 0.8, 0.2 + fl, 0.3 + fl * 1.3)
            else:
                base = 0.25 + max(0.0, dist - 200.0) / 2500.0
                ct = (base * 0.8, base, base * 1.35)
        return cls, is_threat, ct, md, ranged

    def decide(self, teams, now=None):
        """A defensive input for this decision, or None to let others act.

        Returns (label, buttons, stick_or_None, hold_frames).
        """
        now = clock.now() if now is None else now
        me, en = teams.get("me") or {}, teams.get("enemy") or {}
        if not me or not en:
            self.active = None
            return None
        if "act" not in en:
            self.warned_no_act = True
            return None

        ev = self._events(me, en)
        if D.is_red(en):
            self._red_t = now
        self._score(ev, me, now)
        # The opponent's live projectile count (sampler proj_n): a rise is a
        # new shot in the air, whatever the thrower is doing by now.
        pn, pk = en.get("proj_n"), en.get("key")
        if isinstance(pn, (int, float)):
            lk, ln = self._shots
            if pk == lk and isinstance(ln, (int, float)) and pn > ln:
                self._shot_t = now
            self._shots = (pk, pn)
        if ev["jg"] or ev["js"] or ev["ctr"]:
            # It worked: the attacker is frozen in hit-slow. Holding Guard
            # through that would throw the punish away - hand it to the model
            # (Agent._rerank marks attacks up while the opponent is slowed).
            # As long as Agent._jg_jab keeps pressing the jab: the just-guard
            # animation alone can last 0.9 s, and guarding again then would
            # eat the jab press.
            self.active = None
            self._punish_until = now + Agent.JAB_S
        if _cls(me) in OFFENCE:
            self._punish_until = 0.0       # our attack is out: back to normal
        if now < self._punish_until or slow_edge(me, en) > 0:
            self.active = None
            return None

        my_cls, en_cls = _cls(me), _cls(en)
        if my_cls == "damage":
            self.active = None
            return None                    # the escape policy's problem
        if en_cls == "damage":
            self.active = None
            return None                    # we are the ones on offence

        act, n = en.get("act"), en.get("act_n") or 0
        last_key, last_act, last_n = self._last_en
        started = (en.get("key") != last_key or act != last_act
                   or (isinstance(last_n, (int, float)) and n < last_n))
        self._last_en = (en.get("key"), act, n)
        if started:
            self._en_start = now

        dist = _dist(me)
        if started:
            self.active = None
        if self.active is not None:
            a = self.active
            if now - a["t0"] > a["until"]:
                self.active = None
                return None
            return self._input(a, me, en, now)
        shot = self._shot_guard(me, en, dist, now)
        if shot is not None:
            return shot
        if en_cls not in self.REACH:
            return self._approach(me, en, en_cls, dist, now)
        if n > self.max_start and not started:
            return self._approach(me, en, en_cls, dist, now)

        cls, is_threat, ct, md, ranged = self._situation(me, en, en_cls, dist)
        if not is_threat:
            return None
        ctx = self._ctx(cls, dist, _hp(me))
        allowed = list(self.OPTIONS)
        if self.assist:
            allowed = [o for o in allowed if o != "none"]
        if cls == "unblock":
            allowed = [o for o in allowed if o in self.RED_OPTS]
        if me.get("jg_off"):
            # Just guard is locked out (a tap inside the last 1.0 s): the JG
            # arms would only stand still.
            allowed = [o for o in allowed if o not in self.JG_OPTS]
        if cls == "proj" and dist >= self.ZONE_DIST:
            # A Target Combo or a counter from range only walks into the shot
            # (the low-health table had learned 'attack' there: everything
            # scored badly, it was merely the least bad).
            allowed = [o for o in allowed if o not in ("attack", "counter", "none")]
        opt = self._choose(ctx, cls, allowed)
        q = tuple(max(0.0, float(x)) for x in ct)
        until = min(2.0, max(q[2] + 0.35, 0.6))
        if ranged:
            # A volley: keep the answer up (guard after the timed part) until
            # the thrower is free again plus one flight time, not just past
            # the first shot - the later shots of the same move hit an idle
            # fighter otherwise.
            from . import framedata
            fd = framedata.get().profile(en) or {}
            fl = dist / max(float(md.get("prj_speed") or 0.0), 500.0)
            free = fd.get("free")
            if isinstance(free, (int, float)):
                until = max(until, min(self.VOLLEY_MAX, float(free) + fl))
        self.active = {"opt": opt, "t0": now, "q": q, "ctx": ctx, "me": me, "en": en,
                       "tapped": None, "done": False, "side": random.choice((-90.0, 90.0)),
                       "until": until, "ranged": ranged}
        self.stats["chosen"][opt] = self.stats["chosen"].get(opt, 0) + 1
        self.pending.append({"ctx": ctx, "opt": opt, "t": now, "me": me.get("key"),
                             "hp0": _hp(me), "hp_min": _hp(me) or 0.0,
                             "r": 0.0, "hit": False, "ctr_hit": False,
                             "red": cls == "unblock", "caught": False})
        return self._input(self.active, me, en, now)

    def _due(self, a, at, now):
        """Is the moment `at` seconds after the start inside this decision?"""
        return now - a["t0"] >= at - self.TICK * 0.5

    def _input(self, a, me, en, now):
        opt = a["opt"]
        q = a["q"]
        if opt in self.JG:
            at = max(0.0, q[self.JG[opt]] - self.JG_LEAD)
            if a["tapped"] is None:
                if self._due(a, at, now):
                    a["tapped"] = now
                    return ("JUST GUARD", (GUARD,), (0.0, 0.0), 2)
                # Stand still until the timed tap - attacking or guarding into
                # it throws the timing away. A far shot can take a second to
                # arrive: keep moving (no stick override) until it is close,
                # instead of standing frozen in front of the zoner.
                if a.get("ranged") and at - (now - a["t0"]) > self.JG_MOVE_LEAD:
                    return ("wait", (), None, 1)
                return ("wait", (), (0.0, 0.0), 1)
            if now - a["tapped"] < 0.2:
                return ("wait", (), (0.0, 0.0), 1)      # inside the JG window
            return ("GUARD", (GUARD,), (0.0, 0.0), 4)   # a late hit is still blocked
        if opt == "guard":
            return ("GUARD", (GUARD,), (0.0, 0.0), 4)
        if opt == "counter":
            if a["done"]:
                return None
            if self._due(a, max(0.0, q[0] - self.PRE_LEAD), now):
                a["done"] = True
                return ("COUNTER", COUNTER_ATTACK, None, 3)
            return ("wait", (), (0.0, 0.0), 1)
        if opt == "dodge":
            if a["done"]:
                return None
            if self._due(a, max(0.0, q[1] - self.PRE_LEAD), now):
                a["done"] = True
                # sideways relative to the attacker, not the camera
                return ("DODGE", (GUARD,), toward(me or a.get("me") or {}, a["side"],
                                                  en=en or a.get("en")), 3)
            return ("wait", (), (0.0, 0.0), 1)
        if opt == "attack":
            if a["done"]:
                return None
            a["done"] = True
            return ("TARGET COMBO", (NATTACK,), None, 3)
        return None

    def _shot_guard(self, me, en, dist, now):
        """A shot is in the air (the opponent's projectile count just rose)
        and no answer is committed: hold Guard instead of standing there.
        This covers shots thrown from states the start-of-attack trigger
        never sees (mid-string, from a hover, after the answer ran out)."""
        if now - self._shot_t > self.SHOT_GUARD_S:
            return None
        if not (self.ZONE_DIST <= dist <= self.SHOT_MAX_DIST):
            return None
        if _cls(me) in OFFENCE or pinned(me):
            return None
        self.stats["shot_guard"] = self.stats.get("shot_guard", 0) + 1
        return ("GUARD", (GUARD,), (0.0, 0.0), 3)

    def _approach(self, me, en, en_cls, dist, now):
        """Go in on a thrower: once the opponent's projectile move has had
        its chance to connect and they are still stuck in it, dash at them -
        that recovery is the opening a zoner gives (the friend matches: the
        bot stood answering projectiles from range instead of closing)."""
        if en_cls not in ("attack",) or not (450.0 <= dist <= 1800.0):
            return None
        if _cls(me) in OFFENCE or pinned(me):
            return None
        key = (en.get("key"), en.get("act"))
        if self._approached == key:
            return None
        from . import framedata, movedata
        act = en.get("act")
        md = movedata.get().move(en.get("key"), act)
        # A real shot: flies (speed) and reaches past melee range. Several
        # punches are 'projectile' hitboxes in the DB (Bakugo's blasts).
        if not (md and md.get("proj") and (md.get("prj_speed") or 0) > 0
                and (md.get("reach") or 0) >= 450):
            return None
        fd = framedata.get().profile(en)
        if not fd or self._en_start is None:
            return None
        elapsed = now - self._en_start
        free = fd.get("free")
        if free is None or free - elapsed < self.PUNISH_GAP + dist / 3000.0 * 0.3:
            return None
        if (fd.get("startup") or 0.0) + 0.1 > elapsed:
            return None                    # not thrown yet - answered above
        self._approached = key
        self.stats["approach"] += 1
        name = "DashForward" if "DashForward" in ACTION_MAP else "Dash"
        return ("APPROACH", ACTION_MAP[name][0], toward(me, 0.0, en=en), 3)

    def summary(self):
        s = self.stats
        return (f"defence: chose {dict(sorted(s['chosen'].items()))}; "
                f"just guards {s['jg']}, just steps {s['js']}, counters landed "
                f"{s['counter_hit']}, guarded hits {s['gok']}, guard breaks "
                f"{s['gbrk']}, approaches {s['approach']}, shot guards "
                f"{s.get('shot_guard', 0)}")

    def table(self, top=12):
        """Best-known answer per situation, for the end-of-run report."""
        rows = []
        for ctx in sorted(set(self.q) | set(self.qm)):
            cls = ctx.split("|")[0]
            best = max(self.OPTIONS, key=lambda o: self._value(ctx, cls, o)[0])
            v, n = self._value(ctx, cls, best)
            total = sum(nn for _, nn in self.q.get(ctx, {}).values())
            rows.append(f"    {ctx:<16} -> {best:<8} ({v:+.2f}, {total:.0f} tries)")
        return rows[:top]


# ---------------------------------------------------------------------- tag

class TagPolicy:
    """Decides when to Change, and to whom.

    What the old version got wrong, from reading tonight's runs and the game:

    * It judged "the opponent is in hitstun" from health deltas, because the
      opponent's stun/kb/grab never fire. Health flickers between duplicate
      lua_States, and in any run where the opponent was not identified it saw
      no hits at all - so it never tagged. It now reads the opponent's `act`.
    * About half its presses did nothing. Each partner has a cooldown after a
      Change (the tutorial says so), and it kept pressing into it. A press
      that produces no swap now marks that partner unavailable, and the
      cooldown estimate is learned from those failures.
    * Which trigger calls whom was learned as trigger -> character, which
      breaks as soon as the point character rotates. It is now learned per
      (point character, trigger).

    When it tags:
      change combo - hand an ongoing combo to the next fighter, DURING one of
                     our attacks (that is what makes it a Change Combo: the
                     game gives it -30 down value and +10 combo scaling, so
                     the victim stays juggled). Pressed when this fighter's
                     segment has reached its per-character target, or when
                     the fighter is in a move human players hand off from
                     (combobook tag points, e.g. Bakugo's Q1 ~18 ticks in) -
                     in both cases right as a hit lands or just after (the
                     pro's timing in the friend matches), within the game's
                     700-unit reach. At most three fighters per chain - the
                     third one finishes with Plus Ultra (ResourcePolicy).
      rescue       - the victim is still in hitstun but nothing of ours has
                     connected for rescue_stall seconds: the launch is about
                     to run out, and the partner's entry attack (0.1-1.3 s
                     after the swap) is the only thing that can continue it.
      retreat      - our fighter is hurt and there is a safe window (they are
                     in hitstun, far away, or we are guarding - "Change while
                     Guarding" is an advanced technique in the tutorial).
      rebalance    - a partner is much healthier and it is safe.
      death pick   - our point fighter was KO'd: hold the trigger of the best
                     remaining partner (health, meter, matchup) while the
                     game's select window is open (death_pick / hold_tag).
    Never from hitstun, a tech, or while a switch is still playing out - the
    game ignores the press and the partner that was pressed for used to be
    marked as on cooldown for it. Partners are tracked as uncallable for the
    learned cooldown from the moment they LEAVE the point, so a press is only
    made when it can work.
    """

    def __init__(self, min_hits=2, cooldown=12, retreat_hp=0.4, save_hp=0.2,
                 min_incoming_hp=0.2, hit_window=10, cd_guess=12.0,
                 swap_timeout=1.2, rebalance_gap=0.35, team=None):
        # cd_guess: the pro's partners re-entered a median 15 s after leaving
        # (friend matches, 2026-11-09); the old 5 s guess pressed into the
        # cooldown and then blocked the trigger, 61 wasted presses of 80.
        self.min_hits = min_hits
        self.cooldown = cooldown
        self.retreat_hp = retreat_hp
        # Below this, tag out even without a safe window: see should_tag.
        self.save_hp = save_hp
        self.min_incoming_hp = min_incoming_hp
        self.hit_window = hit_window
        self.cd = cd_guess               # learned partner cooldown, seconds
        self.swap_timeout = swap_timeout
        self.rebalance_gap = rebalance_gap
        self.team = list(team or [])     # pick order, a prior for the triggers

        self.partner = TAG_SIDES[0]
        self.hits = 0
        self.since_hit = 10 ** 6
        self.since_tag = 10 ** 6
        self.chain_len = 0
        self.reason = ""
        self.trigger_map = {}            # (point, trigger) -> arrival key
        self.blocked = {}                # trigger -> monotonic time it frees
        self.last_seen_out = {}          # key -> time it left the point
        self._pending = None             # (trigger, from_key, t, target)
        self._last_me = None
        self._last_en_act = None
        self.env = Envelope()
        self.stats = {"pressed": 0, "swapped": 0, "failed": 0, "save": 0,
                      "change_combo": 0, "rescue": 0, "retreat": 0, "rebalance": 0,
                      "death_pick": 0, "death_pick_swapped": 0}
        # Combo chains (ajbot.chain.ChainTracker), shared with the other
        # policies and updated once per tick by the caller.
        self.chain = None
        # Per character: segment lengths that ended with the combo DROPPING
        # (not with a tag) - how far that character can take a combo alone.
        self.seg_hist = {}
        self.seg_prior = {}               # from the combo book, if loaded
        self.default_target = 4
        # Change Combo timing, from the friend-match recordings (2026-11-09):
        # the pro's mid-combo tags came in two modes - right on the hit (0 to
        # 0.45 s after it, the outgoing move still active) or as the launch
        # was about to run out (0.67-1.33 s after the last hit, victim still
        # in Damage*). Inter-hit gaps inside live combos are almost all under
        # 1.0 s, and the partner's first hit lands 0.1-1.3 s after the swap.
        self.hit_fresh = 0.35             # s after a hit: "during the attack"
        self.rescue_stall = 0.5           # s since last hit: combo about to drop
        # The game's own limits (Common/ActionParam.json, サイドキック交代):
        # a Change during an attack only reaches the target within 700 units
        # (1200 while in Plus Ultra / awakened).
        self.tag_reach = 700.0
        self.tag_reach_pu = 1200.0
        # Hand over once the victim's estimated down value (chain.Combo.down)
        # nears the 100 that blows them out of the combo - the Change takes
        # 30 off, so the next fighter gets a fresh run at it.
        self.down_handoff = 85.0
        # Actions a partner may never be called out of: the cinematic Tag
        # Combo (state.SetCallSidekick(false) for its duration), the emergency
        # escape, assists, and Plus Ultra (a trigger during PU is the PU Combo,
        # owned by ResourcePolicy).
        self.no_tag_from = ("AttackTagCombo", "AttackDamageEscape", "AttackSidekick",
                            "AttackRescue", "AttackPlusUltra", "AttackSuperPlusUltra",
                            "AttackExSuperPlusUltra", "AttackBlowDownPlusUltra")
        # Per character: the actions human players Change Combo out of
        # (combobook.ComboPolicy.tag_prior) - {act: {"count", "act_n", ...}}.
        self.tag_prior = {}
        self.last_hit_t = None            # when we last landed a hit
        self._last_combo = None
        # Post-KO partner pick: while our point fighter is dead the game reads
        # a HELD LT/RT to choose who comes in (default otherwise, after ~4.8 s
        # in the recordings; a held pick brought the partner in 1.6-2.6 s).
        self._death = None                # {"key", "t0", "side", "target"}
        self.held = None                  # trigger currently held for the pick
        self.pick_name = None
        self.pick_timeout = 8.0
        self.matchup = None               # optional (partner rec, enemy rec) -> score
        # Learned partner choice and openers for Change Combos (ajbot.tagbook);
        # set by the caller, None = pick the healthiest partner.
        self.tagbook = None
        self.handoff = None               # the last Change Combo we started

    def segment_target(self, char):
        """Hits a character should land before handing the combo on."""
        hist = self.seg_hist.get(char) or []
        if len(hist) >= 3:
            h = sorted(hist[-15:])
            median = h[len(h) // 2]
            # Hand over a little BEFORE the typical drop point.
            return max(3, min(12, int(round(median * 0.85))))
        return self.seg_prior.get(char, self.default_target)

    def _track_segments(self):
        c = self.chain.by_us if self.chain else None
        prev = self._last_combo
        if prev is not None and prev is not c and prev.segments and prev.hits >= 2:
            char, hits = prev.segments[-1][0], prev.segments[-1][1]
            # The last segment ended because the combo dropped, not a tag.
            self.seg_hist.setdefault(char, []).append(hits)
        self._last_combo = c

    def observe(self, teams, now=None):
        now = clock.now() if now is None else now
        self.since_tag += 1
        self.since_hit += 1
        self._teams = teams
        self._track_segments()
        me, en = teams.get("me") or {}, teams.get("enemy") or {}
        mk = me.get("key")

        # Hits: the opponent entering hitstun (new damage action) or its
        # event counter moving.
        hit = self.env.delta(en.get("key"), "ev_hit", en.get("ev_hit")) > 0
        en_act = (en.get("key"), en.get("act"))
        if _cls(en) == "damage" and en_act != self._last_en_act:
            hit = True
        self._last_en_act = en_act
        if hit:
            self.hits += 1
            self.since_hit = 0
            self.last_hit_t = now
        elif self.since_hit > self.hit_window:
            self.hits = 0

        # A swap: the point character changed.
        if mk and self._last_me and mk != self._last_me:
            self.last_seen_out[self._last_me] = now
            self.point_t = now
            if self._pending:
                trig, before, t0, _target = self._pending
                self.trigger_map[(before, trig)] = mk
                self.stats["swapped"] += 1
                self._pending = None
            if self._death and self._death["key"] == self._last_me:
                # The post-KO pick resolved: the newcomer is on point. Learn
                # which trigger that was (a prior for the next KO on this team)
                # and leave the triggers alone for a moment.
                if self._death["side"]:
                    self.trigger_map[(self._last_me, self._death["side"])] = mk
                    self.stats["death_pick_swapped"] += 1
                self._death = None
                self.held = None
                self.since_tag = 0
        if mk:
            self._last_me = mk

        # Our point fighter died: the partner-select window is open until the
        # game brings the next one in (teams() keeps the dead fighter as `me`
        # until then - 1.6-5.1 s in the recordings).
        if mk and me and not me.get("sk"):
            dead = bool(me.get("dead")) or ((_hp(me) or 1.0) <= 0.001)
            if dead and self._death is None:
                self._death = {"key": mk, "t0": now, "side": None, "target": None}
        if self._death and now - self._death["t0"] > self.pick_timeout:
            self._death = None
            self.held = None

        if self._pending and now - self._pending[2] > self.swap_timeout:
            trig, before, t0, target = self._pending
            # Nothing happened: that partner is on cooldown. Learn roughly how
            # long it lasts from how soon after its last Change we tried.
            self.blocked[trig] = now + self.cd
            left = self.last_seen_out.get(target) if target else None
            if left is not None:
                self.cd = min(20.0, max(self.cd, now - left + 0.5))
            self.stats["failed"] += 1
            self._pending = None

    @staticmethod
    def _bench(teams):
        """Team-mates that can actually be tagged in.

        Excludes the point fighter's own alternate form (Dabi and his burning
        form are one fighter with two ids) and unnamed companion states that
        never act - neither is a partner the triggers can call.
        """
        from .roster import base_name
        me_rec = teams.get("me") or {}
        me = me_rec.get("key")
        me_base = base_name(me)
        out = []
        for c in (teams.get("mine") or []):
            k = c.get("key")
            if k == me or c.get("dead"):
                continue
            if me_base and base_name(k) == me_base:
                continue
            if base_name(k) is None and c.get("act") in (None, "None") \
                    and c.get("cmd") in (None, "None"):
                continue
            out.append(c)
        return out

    def _trigger_for(self, point, key):
        for (p, trig), who in self.trigger_map.items():
            if p == point and who == key:
                return trig
        tried = {trig for (p, trig) in self.trigger_map if p == point}
        untried = [t for t in TAG_SIDES if t not in tried]
        return untried[0] if untried else TAG_SIDES[0]

    def partner_cooldown(self, bench_size):
        """Seconds a partner stays uncallable after leaving the point. The
        game's ActionParam gives the two-fighter recast rate as 0.4x, so with
        one partner left on the bench the wait is much shorter."""
        return self.cd * (0.4 if bench_size <= 1 else 1.0)

    def _candidates(self, teams, now):
        point = (teams.get("me") or {}).get("key")
        # An opposing Toga in her blood transform locks our Change out
        # (game rule), so a press would only burn the trigger.
        if toga_mode(teams.get("enemy")) == "blood":
            return []
        bench = self._bench(teams)
        cd = self.partner_cooldown(len(bench))
        out = []
        for c in bench:
            hp = _hp(c)
            if hp is None or hp < self.min_incoming_hp:
                continue
            # Still on cooldown from its own exit: a press now does nothing
            # (and used to mark the TRIGGER blocked for even longer).
            left = self.last_seen_out.get(c.get("key"))
            if left is not None and now - left < cd:
                continue
            trig = self._trigger_for(point, c.get("key"))
            if self.blocked.get(trig, 0.0) > now:
                continue
            out.append((hp, c, trig))
        out.sort(key=lambda x: -x[0])
        return out

    NEW_POINT_S = 1.5      # no Change for this long after our point changed (bar 'save')

    def should_tag(self, teams, now=None):
        now = clock.now() if now is None else now
        if self.since_tag < self.cooldown or self._pending or self._death:
            return False
        me, en = teams.get("me") or {}, teams.get("enemy") or {}
        if not me or pinned(me):
            return False
        my_cls, en_cls = _cls(me), _cls(en)
        # No Change comes out of hitstun or a tech, nor while a switch is
        # still playing out (SidekickEnter/SwitchWait on the newcomer): 21 of
        # the 61 wasted presses in the friend matches were exactly these.
        if my_cls in ("damage", "recovered", "assist"):
            return False
        cands = self._candidates(teams, now)
        if not cands:
            return False
        in_hp, incoming, trig = cands[0]

        en_stunned = en_cls == "damage" or self.since_hit <= self.hit_window
        attacking = my_cls in OFFENCE if my_cls is not None else True
        my_hp = _hp(me) or 0.0
        far = _dist(me) > 700

        reason = None
        c = self.chain.by_us if self.chain else None
        my_act = me.get("act") or ""
        # "Change during an attack = Change Combo" (tutorial). The game lets
        # a partner be called out of any attack unless the script turned
        # state.SetCallSidekick off (the cinematic Tag Combo does), so there
        # is no cancel window to hit - only the reach limit and the victim
        # still being in hitstun when the partner arrives.
        in_attack = my_act.startswith("Attack") and not my_act.startswith(self.no_tag_from)
        reach = self.tag_reach_pu if me.get("awake") else self.tag_reach
        if my_hp <= self.save_hp and in_hp >= my_hp + 0.15:
            # About to lose this fighter. A KO costs far more than handing the
            # partner an awkward spot, so no safe window is required - only
            # that we are not mid-combo on the receiving end (checked above).
            reason = "save"
        elif c is not None and c.chars < 3 and en_cls == "damage" \
                and _dist(me) <= reach and my_cls not in ("damage", "guard", "assist"):
            from .roster import base_name
            char = move_char(me)
            tgt = self.segment_target(char)
            seg, stall = c.segment_hits, c.stalled(now)
            fresh = (self.last_hit_t is not None and now - self.last_hit_t <= self.hit_fresh) \
                or bool(me.get("hit_confirm"))
            # A tag point the book learned from human play: this action, this
            # far in, with the chain usually continuing afterwards.
            prior = (self.tag_prior.get(char) or {}).get(my_act)
            n_in = me.get("act_n") if isinstance(me.get("act_n"), (int, float)) else 0
            at_point = bool(prior) and n_in >= (prior.get("act_n") or [0])[0]
            heavy = getattr(c, "down", 0.0) >= self.down_handoff and seg >= 1
            if in_attack and (seg >= tgt or heavy) and (fresh or stall >= 0.3):
                reason = "change_combo"       # this fighter's segment is done
            elif in_attack and at_point and seg >= 1 and (fresh or stall >= 0.3):
                reason = "change_combo"       # a move players hand off from
            elif seg >= 1 and stall >= self.rescue_stall:
                # The launch is running out and nothing of ours is about to
                # connect: the partner's entry attack lands 0.1-1.3 s from
                # now, still inside the hitstun a launcher gives.
                reason = "rescue"
        elif c is None and self.chain is None and en_stunned and attacking \
                and self.hits >= self.min_hits:
            reason = "change_combo"           # no chain tracker: old rule
        # Health-driven changes are checked even when the combo branch above
        # was entered and declined (a stunned opponent is the safe window).
        if reason is None and my_hp <= self.retreat_hp and in_hp > my_hp + 0.1 and (
                en_stunned or far or my_cls == "guard"):
            reason = "retreat"
        elif reason is None and in_hp - my_hp >= self.rebalance_gap and (en_stunned or far):
            reason = "rebalance"
        if not reason:
            return False
        # The newcomer gets its turn: the combo's last segment is still the
        # outgoing fighter's, so 'rescue' fired before the newcomer had
        # pressed anything (a re-tag 0.3 s after landing, partner bounced
        # back out). Only a fighter about to be KO'd leaves sooner.
        if reason != "save" and now - getattr(self, "point_t", -1e9) < self.NEW_POINT_S:
            return False
        if reason in ("change_combo", "rescue") and self.tagbook is not None and len(cands) > 1:
            # Continuing a combo: bring in whoever has historically carried
            # it furthest from this fighter (tag book), not just the
            # healthiest. A partner close to KO pays for it.
            out = move_char(me)
            gnd = en.get("ground")
            air = None if gnd is None else not gnd
            best = None
            for hp_c, cand, trg in cands:
                sc, n = self.tagbook.score(out, move_char(cand), air)
                if hp_c < 0.3:
                    sc -= 2.0
                if best is None or sc > best[0]:
                    best = (sc, hp_c, cand, trg, n)
            if best[2] is not incoming:
                self.tagbook.picks["book"] += 1
                in_hp, incoming, trig = best[1], best[2], best[3]
            else:
                self.tagbook.picks["health"] += 1
        if reason in ("change_combo", "rescue"):
            gnd = en.get("ground")
            self.handoff = {"t": now, "in": move_char(incoming),
                            "key": incoming.get("key"),
                            "air": None if gnd is None else not gnd}
        self.partner = trig
        self.reason = reason
        self._target = incoming.get("key")
        return True

    def opener(self, me, available, now=None):
        """The tag book's first attack for the fighter that just came in on a
        Change Combo, or None (only within the first second of the handoff,
        while the combo is still ours)."""
        h = self.handoff
        now = time.monotonic() if now is None else now
        if not h or self.tagbook is None or now - h["t"] > 1.2:
            return None
        if self.chain is not None and self.chain.by_us is None:
            return None
        if (me or {}).get("key") != h["key"]:
            return None
        act = self.tagbook.opener(h["in"], h["air"], available)
        if act:
            self.handoff = None           # one opener per handoff
        return act

    # ---- post-KO partner pick ----

    def _pick_score(self, cand, en):
        """Higher is the better fighter to bring in after a KO: health first,
        then meter (a Rising-ready partner comes in with a free comeback
        tool), then any matchup knowledge the caller supplied."""
        s = _hp(cand) or 0.0
        if cand.get("awk_full") or cand.get("awk_pow"):
            s += 0.12
        if cand.get("awake"):
            s += 0.08
        if cand.get("ok_pu1"):
            s += 0.04
        if self.matchup is not None:
            try:
                s += float(self.matchup(cand, en) or 0.0)
            except Exception:
                pass
        return s

    def death_pick(self, teams, now=None):
        """The trigger to HOLD while the post-KO partner select is open, or
        None. Call every tick after observe(); hold the side it names and
        release when it stops naming one (`held` is what was last named)."""
        now = clock.now() if now is None else now
        d = self._death
        if not d:
            self.held = None
            return None
        me = teams.get("me") or {}
        if me.get("key") != d["key"]:
            # The pointer moved before observe() saw it, or it never
            # belonged to us: nothing to hold.
            self.held = None
            return None
        if now - d["t0"] < 0.2:
            return None                   # KO freeze: let the roster settle
        if d["side"] is None:
            en = teams.get("enemy") or {}
            cands = [(self._pick_score(c, en), c) for c in self._bench(teams)
                     if _hp(c) is not None and _hp(c) > 0.001]
            if not cands:
                return None
            cands.sort(key=lambda sc: -sc[0])
            best = cands[0][1]
            # Partner cooldown does not apply to a forced change, so blocked
            # triggers are not skipped here.
            d["side"] = self._trigger_for(d["key"], best.get("key"))
            d["target"] = best.get("key")
            self.pick_name = best.get("id") or best.get("key")
            self.partner = d["side"]
            self.reason = "death_pick"
            self.stats["death_pick"] += 1
        self.held = d["side"]
        return d["side"]

    def tagged(self, teams=None, now=None):
        now = clock.now() if now is None else now
        self.chain_len = self.chain_len + 1 if self.since_tag <= 40 else 1
        self.since_tag = 0
        self.hits = 0
        self.stats["pressed"] += 1
        if self.reason:
            self.stats[self.reason] = self.stats.get(self.reason, 0) + 1
        point = ((teams or {}).get("me") or {}).get("key")
        self._pending = (self.partner, point, now, getattr(self, "_target", None))

    def summary(self):
        s = self.stats
        return (f"tags: pressed {s['pressed']}, swapped {s['swapped']}, "
                f"no effect {s['failed']} (partner cooldown est. {self.cd:.1f}s); "
                f"change combos {s['change_combo']} (+{s['rescue']} rescues), "
                f"retreats {s['retreat']}, saves {s['save']}, "
                f"rebalances {s['rebalance']}; KO picks {s['death_pick']} "
                f"({s['death_pick_swapped']} resolved); segment targets "
                f"{ {k: self.segment_target(k) for k in self.seg_hist} }")


# ------------------------------------------------------------------- escape

class EscapePolicy:
    """Spends the emergency dodge (R3 while being hit) - and learns when.

    One stock at the start of a match, one more each time one of our fighters
    goes down. The old rule (4 hits or 12% damage) spent it on the first
    stray combo and then had nothing for the one that mattered.

    The decision is a price. What escaping saves is the damage this combo is
    still expected to do (models/threat.json: by attacker and hits landed so
    far, mined from every recording), scaled to THIS opponent from how their
    combos have gone this match - a pro's routes run longer than the CPU's -
    times the chance an escape comes out from the hit state we are in (a
    wall bounce or an invisible grab mostly refuses it; that is learned
    online too, models/escape.json). Damage that would leave us low counts
    more, a combo that would KO us counts most. Against that: what the stock
    is worth - more when it is the last one and the fighter is healthy, less
    on the last fighter, who will never earn another.
    """

    PATH = Path("models") / "escape.json"
    VERIFY_S = 0.7            # an escape that has not started by then did not
    NO_STOCK_AFTER = 2        # this many duds in a row: assume no stock left

    def __init__(self, budget=1, min_hits_taken=2, cooldown=60, low_hp=0.4,
                 min_damage=0.12, path=PATH, learn=True):
        self.chain = None                 # shared ChainTracker, if any
        self.budget = budget
        self.min_hits_taken = max(1, int(min_hits_taken or 2))
        self.cooldown = cooldown          # decisions after an escape
        self.low_hp = low_hp
        self.min_damage = min_damage
        self.path = Path(path) if path else None
        self.learn = learn
        self.since = 10 ** 6
        self.taken = 0
        self.used = 0
        self.env = Envelope()
        self._last_act = None
        self._dead = set()
        self.ok = {}                      # hit state -> [tries, came out]
        self.scale = 1.0                  # this opponent's combos vs the table
        self._scale_n = 0
        self._pending = None              # an escape waiting to be confirmed
        self._duds = 0
        self._combo = None                # the open combo on us, for scaling
        self._pred = None                 # its predicted total at hit 2
        self._escaped = False
        self.stats = {"pressed": 0, "ok": 0, "dud": 0, "skipped_state": 0, "model_votes": 0}
        self.last_reason = ""
        self._load()

    # ---- persistence ----
    def _load(self):
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.ok = {k: list(v) for k, v in (raw.get("ok") or {}).items()}
            except (OSError, ValueError, AttributeError):
                self.ok = {}

    def save(self):
        if not (self.path and self.learn):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.path, json.dumps({"version": 1, "ok": self.ok}, indent=1))
        except OSError:
            pass

    def p_ok(self, act):
        """Chance an escape pressed in `act` comes out: the mined rate as a
        prior, this machine's own presses on top."""
        from . import threat
        prior = threat.get().escape_ok(act) if act else None
        prior = 0.5 if prior is None else prior
        n, k = self.ok.get(act or "?", (0, 0))
        return (k + 4.0 * prior) / (n + 4.0)

    # ---- live ----
    def observe(self, teams, snap=None, now=None):
        now = clock.now() if now is None else now
        self.since += 1
        me = teams.get("me") or {}
        mk = me.get("key")
        hit = self.env.delta(mk, "ev_hit", me.get("ev_hit")) > 0
        esc = self.env.delta(mk, "ev_esc", me.get("ev_esc")) > 0
        a = (mk, me.get("act"))
        if _cls(me) == "damage" and a != self._last_act:
            hit = True
        self._last_act = a
        if hit:
            self.taken += 1
        elif not pinned(me):
            self.taken = 0            # the combo ended

        for rec in (teams.get("mine") or []):
            key = rec.get("key")
            h = rec.get("hp")
            dead = rec.get("dead") or (isinstance(h, (int, float)) and h <= 0.0001)
            if key and dead and key not in self._dead:
                self._dead.add(key)
                self.budget += 1
                self._duds = 0            # a KO earns a stock: try again

        # Did the last press come out?
        p = self._pending
        if p is not None:
            came = esc or me.get("act") == "AttackDamageEscape" or _cls(me) == "escape"
            if came or now - p["t"] > self.VERIFY_S or mk != p["key"]:
                self._pending = None
                arm = self.ok.setdefault(p["act"] or "?", [0, 0])
                arm[0] += 1
                if came:
                    arm[1] += 1
                    self._duds = 0
                    self.stats["ok"] += 1
                else:
                    # Nothing happened: the stock was not spent. Two duds in
                    # a row from states that usually work = there is no stock.
                    self.used = max(0, self.used - 1)
                    self._duds += 1
                    self.stats["dud"] += 1
                    if self._duds >= self.NO_STOCK_AFTER:
                        self.used = self.budget
                self.save()

        # This opponent's combos against the table: every combo on us we sat
        # through, actual damage / what the table predicted at its 2nd hit.
        on = self.chain.on_us if self.chain else None
        if on is not self._combo:
            c = self._combo
            if c is not None and self._pred is not None and not self._escaped:
                r = max(0.3, min(3.0, c.damage / max(self._pred, 0.02)))
                self._scale_n += 1
                k = max(0.25, 1.0 / self._scale_n)
                self.scale = (1 - k) * self.scale + k * r
            self._combo = on
            self._pred = None
            self._escaped = False
        if on is not None and on.hits >= 2 and self._pred is None:
            rem = self._remaining(on, 2)
            if rem is not None:
                self._pred = on.damage + rem

    def _remaining(self, on, hits=None):
        from . import threat
        th = threat.get()
        if not th:
            return None
        return th.combo_remaining(on.attacker or "*", on.hits if hits is None else hits,
                                  int(bool(on.pu)))

    # The action head's own "Escape" pick is a vote, never a press - and
    # outside a combo that would KO us, the stock is spent only on a vote.
    # The pro escaped 0-1 times a match (game counter, 6 matches; ~1/match
    # over 74 vs_human matches) while the price alone spent all 3 every
    # match. Since FEATURE_VERSION 16 the head learns Escape from human sides
    # only and holds (Wait) in hitstun: on the pro matches it votes in 4 of
    # 42 hit episodes, ~30 ticks into long combos (v15: 42 of 42, on the
    # first hit). A pre-16 policy votes on nearly every hit, so with one of
    # those this gate is a no-op and the price decides alone, as before.
    NEED_VOTE = True

    def should_escape(self, teams, now=None, model_wants=False):
        """`model_wants`: the action head picked Escape this tick."""
        now = clock.now() if now is None else now
        if model_wants:
            self.stats["model_votes"] += 1
        if self.budget - self.used <= 0 or self.since < self.cooldown or self._pending:
            return False
        me = teams.get("me") or {}
        if not pinned(me):
            return False
        hp = _hp(me)
        if hp is None or hp <= 0.001 or me.get("dead"):
            return False                  # already KO'd: nothing to save
        if "ok_esc" in me and not me.get("ok_esc"):
            return False
        act = me.get("act")
        p = self.p_ok(act)
        if p < 0.3:
            # This hit state mostly refuses it (a knockdown, a wall bounce):
            # wait for the next hit, which usually is a better one.
            self.stats["skipped_state"] += 1
            return False
        on = self.chain.on_us if self.chain else None
        hits = on.hits if on else self.taken
        dmg = on.damage if on else 0.0
        rem = self._remaining(on) if on else None
        if rem is None:
            # No estimate (no open combo on us, or no table): the old
            # thresholds. Low on health any combo may be the KO; above that,
            # the vote gate applies here too.
            self.last_reason = (f"{act} hits={hits} hp={hp:.2f} (no combo estimate)"
                                + (" model" if model_wants else ""))
            if hp <= self.low_hp:
                return hits >= 2 or dmg >= 0.06
            if self.NEED_VOTE and not model_wants:
                return False
            return hits >= self.min_hits_taken + 2 or dmg >= self.min_damage
        rem *= self.scale
        lethal = rem >= hp - 0.02
        if hits < self.min_hits_taken and not lethal:
            return False
        if self.NEED_VOTE and not lethal and not model_wants:
            return False                  # not a combo a human would escape
        saved = p * min(rem, hp)
        value = saved * D.taken_weight(max(0.0, hp - rem))
        if lethal:
            value += p * 0.35             # the fighter itself
        value += p * 0.03                 # the attacker is slowed: a punish
        left = self.budget - self.used
        alive = sum(1 for r in (teams.get("mine") or [])
                    if (_hp(r) or 0.0) > 0.001 and not r.get("dead"))
        price = 0.11 if left <= 1 else 0.07
        if alive <= 1:
            price *= 0.6                  # the last fighter earns no more stocks
        if hp >= 0.85 and hits < 4:
            price += 0.04                 # healthy and early in the combo
        self.last_reason = (f"{act} p={p:.2f} rem={rem:.2f}x{self.scale:.2f} "
                            f"hp={hp:.2f} value={value:.3f} price={price:.3f}"
                            + (" model" if model_wants else ""))
        return value >= price

    def spend(self, teams=None, now=None):
        now = clock.now() if now is None else now
        me = (teams or {}).get("me") or {}
        self.used += 1
        self.since = 0
        self.taken = 0
        self.stats["pressed"] += 1
        self._pending = {"t": now, "act": me.get("act"), "key": me.get("key")}
        self._escaped = True


# ---------------------------------------------------------------- resources

class ResourcePolicy:
    """Spends Rising and Plus Ultra, and follows a 2-bar Plus Ultra with a
    Plus Ultra Combo (press a trigger during the animation).

    Neither gauge can be read: the game keeps them in C++ (no Lua getter -
    checked live; `awk_full` is which Rising animation plays, not the
    gauge). So both are pressed and then CHECKED:

      Rising   R3 is Rising only from a free state - pressed while being hit
               it is the emergency dodge and spends a stock. In the
               recordings 117 R3 presses from Idle produced 0 Risings (the
               gauge was not full) and 84 more were made from DamageStrong.
               Now: free states only, verified within 0.8 s, and on a dud
               the next try waits 3 -> 6 -> 12 -> 20 s.
      Plus Ultra  Guard+Q2. Mid-attack the press is dropped unless a cancel
               point is open: 25% of the old presses came out, most of the
               rest turned into a Q2. Now the press repeats every ~0.15 s
               for up to 1 s while the reason holds, stops the moment the
               Plus Ultra starts, and states it never comes out from
               (Tag Combo cinematic, another Plus Ultra) are skipped.
    """

    RISING_FREE = {"idle", "move", "guard", "dodge", "recovered", "other"}
    RISING_VERIFY_S = 0.8
    RISING_BACKOFF = (3.0, 6.0, 12.0, 20.0)
    RISING_AFTER_OK = 25.0
    PU_RETRY_S = 1.0
    PU_REPRESS_S = 0.15
    PU_BACKOFF_S = 2.0

    # Plus Ultras that are a pickup buff, not an attack (game data,
    # DB/Character/Player/PlNN). Momo's level-1 Plus Ultra throws a Lucky
    # Bag: a marker follows HER for 1.5 s, the bag drops there and sits for
    # 12 s, and the first fighter to touch it gets the YaoyorozuLuckyBag
    # status - Momo or her partner level 3 (attack x1.4, +30 hp, 15 s), the
    # enemy only level 1 (x1.2, +20 hp, 10 s); one pickup removes it for
    # everyone. It does no damage itself (only the awakened Unlucky bag
    # does), so the finisher/punish rules never fire it. It is thrown in
    # open neutral with room to spare, and then she stands still so the
    # bag lands on her.
    PU_BUFF = {"Yaoyorozu": {"min_dist": 1100.0, "stay_s": 3.2, "every_s": 18.0}}
    BUFF_FREE = {"idle", "move", "guard", "recovered", "other"}
    BUFF_CLOSE = 450.0            # enemy this close: fight, stop waiting

    def __init__(self, use_rising=True, use_pu=True, cooldown=90,
                 rising_action_cooldown=30, pu_combo=True):
        self.chain = None                 # shared ChainTracker, if any
        self.tagger = None                # for "can we still tag?" and targets
        self.use_rising = use_rising
        self.use_pu = use_pu
        self.cooldown = cooldown
        self.rising_action_cooldown = rising_action_cooldown
        self.pu_combo = pu_combo
        self.since = 10 ** 6
        self.since_ra = 10 ** 6
        self._pu_at = None            # when a 2-bar Plus Ultra was fired
        self._pu_try = None           # {"t0", "last", "why"} while trying
        self._pu_block = 0.0
        self._stay_until = 0.0        # stand still: our buff bag is landing
        self._buff_next = 0.0         # earliest next buff Plus Ultra
        self.vetoed = 0               # model Plus Ultra picks refused (gate_model_pu)
        self._rising_next = 0.0       # earliest next Rising press
        self._rising_fails = 0
        self._rising_pending = None
        self.env = Envelope()
        self.spent = {"rising": 0, "pu": 0, "rising_action": 0, "pu_combo": 0}
        self.came = {"rising": 0, "pu": 0}

    @staticmethod
    def _started(me, what):
        cls = _cls(me)
        if what == "pu":
            return cls == "pu" or (me.get("act") or "").startswith("AttackPlusUltra")
        return bool(me.get("awake")) or (me.get("act") or "") == "AttackAwaking"

    def decide(self, teams, now=None):
        """'pu', 'pu_combo', 'rising', 'rising_action', or None."""
        now = clock.now() if now is None else now
        self.since += 1
        self.since_ra += 1
        me = teams.get("me") or {}
        en = teams.get("enemy") or {}
        en_cls = _cls(en)
        pressured = (en_cls == "damage") if en_cls is not None else bool(
            en.get("stun") or en.get("kb") or en.get("grab"))

        if self._pu_at is not None:
            dt = now - self._pu_at
            if 0.5 <= dt <= 1.6:
                self._pu_at = None
                return "pu_combo"
            if dt > 1.6:
                self._pu_at = None

        # Rising: confirm the last press.
        rp = self._rising_pending
        if rp is not None:
            if self._started(me, "rising"):
                self._rising_pending = None
                self._rising_fails = 0
                self._rising_next = now + self.RISING_AFTER_OK
                self.came["rising"] += 1
            elif now - rp > self.RISING_VERIFY_S:
                self._rising_pending = None
                i = min(self._rising_fails, len(self.RISING_BACKOFF) - 1)
                self._rising_next = now + self.RISING_BACKOFF[i]
                self._rising_fails += 1

        # Plus Ultra: keep pressing while the reason holds, until it starts.
        tr = self._pu_try
        if tr is not None:
            if self._started(me, "pu"):
                self._pu_try = None
                self.came["pu"] += 1
                self.since = 0
                if tr["why"] == "buff":
                    cfg = self._buff_cfg(me) or {}
                    self._stay_until = now + cfg.get("stay_s", 3.0)
                    self._buff_next = now + cfg.get("every_s", 18.0)
                    return None           # no Plus Ultra Combo off a bag
                if self.chain is not None:
                    self.chain.note_pu()
                if self.pu_combo:
                    # The 2-bar flag (ok_pu2) never reads 1, so the bars are
                    # unknown: try the Plus Ultra Combo after every Plus
                    # Ultra - with one bar the trigger does nothing.
                    self._pu_at = now
            elif now - tr["t0"] > self.PU_RETRY_S or not me.get("ok_pu1") \
                    or (tr["why"] == "finisher" and en_cls != "damage") \
                    or (tr["why"] == "buff" and not self._buff_ok(me, en, now)):
                self._pu_try = None
                self._pu_block = now + self.PU_BACKOFF_S
            elif now - tr["last"] >= self.PU_REPRESS_S and self._pu_state_ok(me):
                tr["last"] = now
                return "pu"
            else:
                return None

        if self.since >= self.cooldown and now >= self._pu_block and self.use_pu \
                and me.get("ok_pu1") and self._pu_state_ok(me):
            why = None
            if self._pu_finisher(me, en, now):
                why = "finisher"
            elif self.chain is None and pressured:
                why = "finisher"                  # no chain tracker: old rule
            elif self._pu_punish(me, en):
                why = "punish"
            elif self._buff_ok(me, en, now):
                why = "buff"
            if why:
                self._pu_try = {"t0": now, "last": now, "why": why}
                return "pu"

        if (self.use_rising and not me.get("awake") and self._rising_pending is None
                and now >= self._rising_next and self._rising_ok(me, en)):
            self._rising_pending = now
            return "rising"

        if (self.use_rising and me.get("awake") and pressured
                and self.since_ra >= self.rising_action_cooldown):
            return "rising_action"
        return None

    def _pu_state_ok(self, me):
        """Not from a state a Plus Ultra never comes out of (mined)."""
        from . import threat
        act = me.get("act")
        if act in ("AttackTagCombo", "AttackTagComboConfirm", "SidekickReturn") \
                or (act or "").startswith("AttackPlusUltra") or pinned(me):
            return False
        p = threat.get().res_ok("pu", act) if act else None
        return p is None or p >= 0.06

    def _buff_cfg(self, me):
        return self.PU_BUFF.get(move_char(me) or "")

    def _buff_ok(self, me, en, now):
        """A pickup-buff Plus Ultra: thrown from a free state with the enemy
        far enough away (or still flying from a knockback) that they can
        neither punish the animation nor beat us to the bag."""
        cfg = self._buff_cfg(me)
        if not cfg or now < self._buff_next or me.get("st_lucky"):
            return False
        if _cls(me) not in self.BUFF_FREE or pinned(me):
            return False
        d, ec = _dist(me), _cls(en)
        if d >= 1e8:
            return False
        if ec == "damage" and en.get("kb") and d >= 0.6 * cfg["min_dist"]:
            return True
        return d >= cfg["min_dist"] and ec not in OFFENCE

    def staying(self, teams, now=None):
        """True while our buff bag is coming down: hold still under it. Ends
        early once the buff is on us or the enemy closes in."""
        now = time.monotonic() if now is None else now
        if now >= self._stay_until:
            return False
        me = (teams or {}).get("me") or {}
        if me.get("st_lucky") or _dist(me) < self.BUFF_CLOSE:
            self._stay_until = 0.0
            return False
        return _cls(me) in self.BUFF_FREE | {"pu"}

    def _pu_punish(self, me, en):
        """The opponent is frozen in hit-slow and we are not (our just guard,
        counter or escape did that): a Plus Ultra from inside its reach is a
        free hit."""
        if slow_edge(me, en) <= 0:
            return False
        from . import movedata
        md = movedata.get().move(me.get("key"), "AttackPlusUltra1") or {}
        reach = max(250.0, float(md.get("reach") or 0.0))
        return _dist(me) <= reach

    def _rising_ok(self, me, en):
        """R3 is a Rising only from a free state; pressed while being hit it
        is the emergency dodge."""
        cls = _cls(me)
        if cls is None or pinned(me) or me.get("act") in (None, "None"):
            return False
        if cls not in self.RISING_FREE:
            # Mid-attack it works from some moves (mined) - only those.
            from . import threat
            p = threat.get().res_ok("rising", me.get("act"))
            if p is None or p < 0.3:
                return False
        # Not into an attack that is about to land: a hit on the same frame
        # turns the press into an escape.
        if _cls(en) in OFFENCE and _dist(me) < 450:
            return False
        return _dist(me) < 1500           # Rising is for fighting, not posing

    MODEL_PU = ("PlusUltra1", "PlusUltra2")

    def gate_model_pu(self, action, teams, now=None):
        """The model's own Plus Ultra pick, vetoed unless one of the rules
        here would fire it too (chain finisher, hit-slow punish, buff). In
        the 2026-11-10 self-play 13 of 18 Plus Ultras went out in neutral
        from the model's pick, spending the meter the third fighter of a
        chain needs to finish it. Returns the action, or None for no press."""
        if action not in self.MODEL_PU or not self.use_pu:
            return action
        now = time.monotonic() if now is None else now
        me, en = (teams or {}).get("me") or {}, (teams or {}).get("enemy") or {}
        if not me or not en:
            return None
        if self._pu_finisher(me, en, now) or self._pu_punish(me, en)                 or self._buff_ok(me, en, now):
            return action
        self.vetoed += 1
        return None

    def _pu_finisher(self, me, en, now):
        """Plus Ultra as the END of a combo chain, not the start of one.

        The aim is three fighters' segments in one chain with a Plus Ultra on
        the last - that is where it takes a stock early. So it fires when:
          * the chain already has three fighters, or no partner can be called
            to extend it, and the current segment is done or about to drop;
          * or the opponent is low enough that it finishes them;
          * or a very long chain is running (it will not get better).
        """
        c = self.chain.by_us if self.chain else None
        if c is None or _cls(en) != "damage":
            return False
        from .roster import base_name
        tg = self.tagger
        target = tg.segment_target(move_char(me)) if tg else 5
        teams = getattr(tg, "_teams", None) if tg else None
        can_tag = bool(teams and tg._candidates(teams, now))
        segment_done = c.segment_hits >= target or (
            c.segment_hits >= 2 and c.stalled(now) >= 0.4) or (
            c.segment_hits >= 1 and getattr(c, "down", 0.0) >= 85.0)
        en_hp = _hp(en)
        if en_hp is not None and en_hp <= 0.22 and c.hits >= 2:
            return True
        if (c.chars >= 3 or not can_tag) and segment_done and c.hits >= 4:
            return True
        return c.hits >= 18

    def spend(self, what, teams=None, now=None):
        # Plus Ultra / Rising are confirmed in decide() when they START; a
        # press alone proves nothing.
        if what == "rising_action":
            self.since_ra = 0
        self.spent[what] = self.spent.get(what, 0) + 1

    def summary(self):
        return (f"resources: pressed {self.spent}; came out {self.came}; "
                f"model Plus Ultras held back {self.vetoed}; "
                f"rising fails in a row {self._rising_fails}")


RESOURCE_INPUT = {
    "pu": (GUARD, QUIRKTEC2),
    "rising": RISING_ENTER,
    "rising_action": RISING_ACTION,
    "pu_combo": (),                   # a trigger, applied by the caller
}

ESCAPE_INPUT = RISING_ENTER


def press_escape(pad, policy, hold_frames=4, teams=None):
    pad.hold(*ESCAPE_INPUT)
    pad.tick(hold_frames)
    pad.release(*ESCAPE_INPUT)
    policy.spend(teams)


def press_resource(pad, policy, what, hold_frames=4, teams=None, side="left"):
    if what == "pu_combo":
        pad.trigger(side, 1.0)
        pad.tick(hold_frames)
        pad.trigger(side, 0.0)
    else:
        buttons = RESOURCE_INPUT[what]
        pad.hold(*buttons)
        pad.tick(hold_frames)
        pad.release(*buttons)
    policy.spend(what, teams)


def press_tag(pad, policy, hold_frames=5, teams=None):
    """Pull the trigger that calls in the chosen partner."""
    side = policy.partner
    pad.trigger(side, 1.0)
    pad.tick(hold_frames)
    pad.trigger(side, 0.0)
    policy.tagged(teams)


def hold_tag(pad, side, hold_frames=3):
    """Keep a trigger pulled across ticks (the post-KO partner pick: the game
    reads a HELD LT/RT while the select window is open). Not released here -
    call release_tag when TagPolicy.death_pick stops naming a side."""
    pad.trigger(side, 1.0)
    pad.tick(hold_frames)


def release_tag(pad, side):
    pad.trigger(side, 0.0)
    pad.tick(1)


def press_defense(pad, plan):
    """Apply a DefensePolicy decision: (label, buttons, stick, hold)."""
    _label, buttons, stick, hold = plan
    if stick is not None:
        pad.stick(stick)
    if buttons:
        pad.hold(*buttons)
    pad.tick(hold)
    if buttons:
        pad.release(*buttons)


# Actions whose direction is part of the decision, not the movement head's:
# the stick is overridden - degrees from straight at the opponent.
FORCED_STICK = {"DashForward": 0.0, "DashBack": 180.0}


def plan_press(action, stick, me=None, en=None):
    """Resolve a decision into (buttons, stick, action) WITHOUT touching a pad.
    `me` (the fighter's record) turns opponent-relative directions into the
    camera-relative stick; without it they fall back to Player 1's view."""
    if not action or action in UNMAPPED:
        return (), stick, None
    if action not in ACTION_MAP:
        _unknown_actions.observe(action)
        return (), stick, None
    buttons, needs_stick = ACTION_MAP[action]
    if action in FORCED_STICK:
        return buttons, toward(me or {}, FORCED_STICK[action], en=en), action
    if needs_stick and abs(stick[0]) + abs(stick[1]) < 0.3:
        # A lever-input move with no direction held becomes the plain version,
        # which is a different move. Nudge toward the opponent (or sideways
        # for a dodge, which forward would turn into a lunge).
        stick = toward(me or {}, 90.0 if action == "Dodge" else 0.0, en=en)
    return buttons, stick, action


def press(pad, action, stick, hold_frames=3, me=None, en=None):
    """Apply one decision to the pad. Returns what was actually pressed."""
    buttons, stick, name = plan_press(action, stick, me, en)
    pad.stick(stick)
    if name is None:
        pad.tick(1)
        return None
    pad.hold(*buttons)
    pad.tick(ACTION_HOLD_OVERRIDE.get(name, hold_frames))
    pad.release(*buttons)
    return name

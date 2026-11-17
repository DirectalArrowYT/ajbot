"""Turn recorded matches into training data.

A tick holds every live character plus what our fighter was doing. Two things
have to be predicted, and they are different kinds of problem:

  action    - which discrete move to start (classification)
  movement  - where to push the stick (regression: direction + magnitude)

Movement is not a minor extra. In a recorded match there were 337 action
changes against 626 significant stick-direction changes, so most decisions a
player makes are movement decisions. `GetCommandId` never reports them, which
is why they have to come from the lever fields.

Framings:

  frames      - every tick. The label is the action currently executing, which
                mostly repeats the previous tick, so a model trained on this
                learns persistence and scores ~98% while knowing nothing.
  transitions - only ticks where the action CHANGES, labelled with the new
                action. Sparse, but it is the decision actually made.

Labels come from two places. `cmd` (GetCommandId) covers attacks, but it never
reports defence. Recordings made since the action wrappers went in also carry
`act` - the action the game is actually running - and a switch INTO a guard,
just guard, counter attack or dodge is labelled from that, so those decisions
can finally be learned instead of only driven by rules.

Every sample also gets a WEIGHT from what followed it (see `rewards`): damage
dealt and taken, and above all just guards, counters, guard successes and
change combos. Training on weighted samples is reward-weighted behaviour
cloning - the model still imitates, but it imitates the decisions that paid
off far more than the ones that did not.
"""
import glob as _glob
import json

import numpy as np

from . import scrub
from .sampler import act_class, ACT_CLASS_NAMES

MAX_AGE = 800          # older records are stale states from a previous match
BENCH_AGE = 3000       # benched team-mates stop updating; past this they are
                       # leftovers from an earlier match
IDLE = {"None", None}

# Per-character scalars. Movement/geometry first, then status flags.
ME_FIELDS = ["hp", "gdist", "fall", "ground", "wall", "stun", "grab", "kb",
             "dodge", "lvl", "awake", "spdh", "spdv", "grav", "tgnd", "twal",
             "angleup", "rside", "lscale", "lon",
             # resources, status and guard state
             "awk_full", "awk_pow", "awk_str", "sk_tag", "sk_pu", "sk_resc",
             "atkcount", "uniq_idx",
             "ok_melee", "ok_exmelee", "ok_u1", "ok_u2", "ok_exu1", "ok_exu2",
             "ok_ub", "ok_pu1", "ok_pu2", "ok_spu", "ok_awk",
             "erased", "st_stun", "st_burn", "st_freeze", "st_elec",
             "autoguard", "jg_off", "juststep", "guardpow", "ok_esc",
             "grabbing", "armor_hit", "dmg_pow", "proj_n", "hit_confirm",
             "kb_dist", "kb_time", "dmg_addtime",
             "atk_blocked", "push_avail", "charge_release", "char_mode"]
ENEMY_FIELDS = ["hp", "gdist", "ground", "stun", "grab", "kb", "angleup",
                "spdh", "spdv", "awake",
                "erased", "autoguard", "juststep", "guardpow",
                "lscale", "lon", "ok_pu1", "ok_u1", "ok_u2", "atkcount",
                # what the opponent can do next matters as much as what we can
                "ok_melee", "ok_ub", "ok_pu2", "ok_awk", "awk_full", "lvl",
                "grabbing", "armor_hit", "proj_n", "hit_confirm",
                "kb_dist", "kb_time", "dmg_addtime",
                "atk_blocked", "push_avail", "charge_release", "char_mode"]

# Fields worth squashing so one huge value cannot dominate a linear model.
SCALE = {"spdh": 2000.0, "spdv": 2000.0, "grav": 2000.0, "gdist": 200.0,
         "fall": 200.0, "angleup": 180.0, "lscale": 100.0, "lvl": 3.0,
         "kb_dist": 300.0}

# Action classes that are defensive decisions, labelled from `act` because
# GetCommandId never reports them. act -> label.
DEFENCE_LABELS = {
    "JustGuard": "JustGuard", "JustGuardStance": "JustGuard",
    "Guard": "Guard", "AirGuard": "Guard",
    "AttackGuardCancel": "CounterAttack",
    "Dodge": "Dodge", "AirDodge": "Dodge",
    # The emergency escape out of hitstun/combos (act_class "escape",
    # ev_esc/ok_esc in sampler.py). Missing here meant it was tracked as a
    # feature and rewarded when used, but could never appear as a label -
    # label_of() only ever emitted it for the OTHER entries in this dict, so
    # the action head had no way to learn "use the escape" as a choice at
    # all, no matter how often real players actually used it.
    "AttackDamageEscape": "Escape",
}

# Movement decisions, labelled from `act` the same way defence is. The game's
# `cmd` only ever names attack commands, so a jump or a dash never showed up
# as a decision: the action head had no Jump/Dash to learn, combo routes could
# not contain them, and a dash-back-then-punish could not exist. The *Begin
# act marks the frame the fighter committed to the move. "Jump" folds in the
# dash-jump and air-jump (they are the Jump button pressed in another state).
MOVE_LABELS = {
    "DashBegin": "Dash", "ParkourDashBegin": "Dash", "ParkourAirDashBegin": "Dash",
    # The homing air dash (dash pressed in the air with a target): seen going
    # into a new TQ2 at speed ~2800, and inside Gran Torino's Melee string.
    "AirChaseBegin": "Dash",
    "JumpBegin": "Jump", "AirJumpBegin": "Jump", "DashJumpBegin": "Jump",
    "ParkourJumpBegin": "Jump", "ParkourDashJumpBegin": "Jump",
}
# A dash is split into forward/back by whether it closes or opens the distance
# to the opponent over the next DASH_LOOK_S seconds (`dist`, in game units; a
# dash covers roughly 300 of them in that time). Anything in between stays a
# plain "Dash" - a sidestep, or one the opponent cancelled out.
DASH_LOOK_S = 0.30
DASH_DIR_MIN = 100.0
DIRECTED_DASH = ("DashForward", "DashBack")

# Follow-up presses inside a quirk move that use a DIFFERENT button from the
# move itself. The game reports every follow-up as the same `cmd` again (ev_push
# rising), which the pad would replay as the quirk button - wrong for these.
# character -> {cmd: label}; the label maps to its button in agent.ACTION_MAP.
# Bakugo's TQ2 is the lever move ExUnique2 (tilt + B); each follow-up is one of
# four different face-button presses (blast / unguardable / circle-around /
# fast step - see `incl` in sampler.py), so they are all one "Follow" decision
# here until recordings carry `incl` and the button for each type is known.
FOLLOW_UP = {
    "Bakugo": {"ExUnique2": "ExUnique2Follow"},
}
# Bakugo's TQ2 (tilt + B) follow-ups. The sampler records the latched choice as
# `incl` (Pl02Bakugo.GetInclusionType) - it stays set after the follow-up, so
# only a change from another value counts. Game type -> the controller button:
#   2 = Y     unblockable            (recording with the presses in order)
#   3 = A     armor, rise-and-descend (same; type 3's body has the super-armor)
#   5 = Dash  the "fast step"        (recording with the dash: incl=5 as the
#             fighter dashes; the body steps in the stick direction)
#   1 = X     blast / the jab follow-up (inferred: the most frequent value)
#   4 = B     TQ2 pressed again      (inferred by elimination: the script
#             dispatches 1, 2, 3, 5 only, so a 4 runs no body and just ends the
#             move - "B does nothing", and the end of the move re-enables TQ2
#             (SetReenableActionAttack when it began in the air). Never seen.)
# Rising (Awaking.IsStrongerUniqueAttack) raises the follow-up limit from 1 to 2.
# Recordings made before `incl` existed can only say "a follow-up happened".
INCL_BUTTON = {1: "X", 2: "Y", 3: "A", 4: "B", 5: "Dash"}

# Ticks of history (same fighter streak only) folded into each sample, so the
# model can see a trend - closing/opening distance, an opponent who was mid
# attack a moment ago and is not now (a punish window) - rather than only the
# frozen instant. Per-tick classification has no other way to learn spacing
# or timing: those are properties of a short window, not of one frame.
HISTORY_K = 5

# Bumped whenever labelling or feature construction changes in a way that
# ME_FIELDS alone would not reveal, so the cache rebuilds.
FEATURE_VERSION = 20     # 15: act_n in bot-pump units, slow-mo block, Wait
                         # 16: Wait also sampled in hitstun
                         # 17: game input-window block (movewindows)
                         # 18: route-progress block (routefeat)
                         # 19: wall block (wallfeat); movement head also
                         #     samples held sticks (HOLD_EVERY_S)
                         # 20: Wait also sampled mid-move (WAIT_MOVE_*)

# `act_n` counts sampler pumps since the act began, not time: 15-21 a second
# in the bot's own runs, 20 in the old human runs and ~46 in the m3-m6 human
# runs (measured 2026-11-09). The same count meant a third of the time in a
# fresh human run, so "how far into the move" disagreed between the data the
# policy learns from and the bot it drives. Every recording is rescaled to
# this rate on load (see _normalize_act_n); live play runs at it already.
BOT_ACT_RATE = 16.7

# Features appended AFTER the original layout (see _extra_feats), so a policy
# trained before they existed still runs: the agent drops the tail it does not
# know. 8 reach + 2 commitment + 8 geometry/threat + opponent model,
# recent-sequence and game frame-data values (oppmodel.N_FEATS), then the
# slow-motion block (_slow_feats), then the game's input windows
# (movewindows.features: which follow-ups the game would take right now).
from .oppmodel import N_FEATS as _OPP_N
from .movewindows import N_FEATS as _WIN_N
from .routefeat import N_FEATS as _ROUTE_N
from .wallfeat import N_FEATS as _WALL_N
SLOW_N = 7
EXTRA_N = 18 + _OPP_N + SLOW_N + _WIN_N + _ROUTE_N + _WALL_N

# "Wait": not pressing anything new. Recordings only hold the moments a
# button WAS pressed (0.3-0.7 a second), while live play asks the policy 15
# times a second, so the model had never seen a moment where the answer is
# "nothing yet" and always named some move. One Wait sample is taken every
# WAIT_EVERY_S while the fighter is free and its next real decision is at
# least WAIT_GAP_S away.
WAIT = "Wait"
WAIT_EVERY_S = 1.0      # 0.5 made Wait 23-35% of every match's samples
WAIT_GAP_S = 0.4
_WAIT_CLASSES = ("idle", "move", "other")
# The same in hitstun, on its own clock. A labelled tick there is almost
# always an escape, so without these the head learned P(Escape | being hit)
# as near 1 and voted Escape on 10-22% of live ticks (2026-11-09 smoke test)
# - live play asks every tick, not only when something was pressed. 0.5 s
# tripled Wait in a bot match (36 -> 122); one a second is plenty against
# ~0-3 escapes a match.
WAIT_HIT_EVERY_S = 1.0
_WAIT_HIT_CLASSES = ("damage",)
# And while the fighter's OWN move plays out. Both rules above skip attack
# states, so between two presses of a string every sample was a press and the
# head learned "mid-move: press the next one now": Shigaraki (Trench) bots
# pressed Q2 every 8 frames where people wait ~37 (2026-11-16: 83% of bot
# Q2->Q2 gaps under 20 frames vs 19% of human ones; 92% grounded, every one
# whiffed). seq.me_since_action already tells the head how long the move has
# run; these give it the "not yet" half. FEATURE_VERSION 20.
# At most ONE per press (0.25 s apart made Wait 77% of a human match's
# samples), 0.1 s into the move, with the next press 0.2 s or more away.
WAIT_MOVE_AFTER_S = 0.1
WAIT_MOVE_GAP_S = 0.2
_WAIT_MOVE_CLASSES = ("combo", "attack")


def wrap180(a):
    return (a + 180.0) % 360.0 - 180.0


def _own_cam_angle(rec):
    tb, vb = (rec or {}).get("tbear"), (rec or {}).get("vbear")
    if isinstance(tb, (int, float)) and isinstance(vb, (int, float)):
        return wrap180(tb - vb)
    return None


def tcam_of(rec, en=None):
    """Stick angle (degrees, 0 = stick up, + = right) that points at the
    opponent. Both players' sticks (and their recorded `lcam`) are relative
    to the ONE battle camera, which sits behind Player 1 - verified live: on
    Player 2, stick up ran 1544 -> 1943 units away and stick down closed in.

    Player 1 (enemy flag 0; the flag is relative to Player 1): its own
    tbear - vbear. Player 2: GetViewDirection there reads Player 2's own
    controller, not the camera its stick follows, so the angle comes from the
    opponent's (Player 1's) record turned around by 180. Older recordings
    without the bearings fall back to 0 / 180 by side."""
    rec = rec or {}
    if not rec.get("enemy"):
        own = _own_cam_angle(rec)
        if own is not None:
            return own
        return float(rec.get("_frame", 0.0))
    p1 = _own_cam_angle(en) if en and not en.get("enemy") else None
    if p1 is not None:
        return wrap180(p1 + 180.0)
    return wrap180(180.0 + float(rec.get("_frame", 0.0)))


def calibrate_frame(ticks):
    """Older recordings have no bearings, and the side rule (camera behind
    Player 1) does not hold for all of them: in some sessions pushing the
    rule's 'toward' measurably opened the distance. Measure it instead -
    compare how the distance changed while the stick pointed the rule's
    'toward' vs 'away' - and turn the frame 180 for the whole recording when
    'away' is what closed in. Writes `_frame` onto every record; no-op when
    the recording has real bearings. Idempotent."""
    if not ticks or ticks[0].get("_calibrated"):
        return
    ticks[0]["_calibrated"] = True
    for t in ticks[:400]:
        if any("tbear" in r for r in (t.get("chars") or {}).values()):
            return
    toward, away = [], []
    for i in range(0, len(ticks) - 6, 2):
        me, en = _rec(ticks[i], "me"), _rec(ticks[i], "enemy")
        if not me or not en:
            continue
        lc, ls = me.get("lcam"), me.get("lscale") or 0
        me2 = _same(ticks[i + 6], key_of(ticks[i], "me"))
        if not isinstance(lc, (int, float)) or ls < 60 or not me2:
            continue
        d0, d1 = me.get("dist"), me2.get("dist")
        if not isinstance(d0, (int, float)) or not isinstance(d1, (int, float)) or d0 > 3000:
            continue
        r = wrap180(lc - (180.0 if me.get("enemy") else 0.0))
        if abs(r) < 45:
            toward.append(d1 - d0)
        elif abs(r) > 135:
            away.append(d1 - d0)
    flip = (len(toward) >= 20 and len(away) >= 20
            and float(np.mean(toward)) > float(np.mean(away)) + 20.0)
    if flip:
        for t in ticks:
            for r in (t.get("chars") or {}).values():
                r["_frame"] = 180.0


def load(pattern="runs/*.jsonl"):
    """Ticks of the matching recordings, stabilized. Reads each recording's
    compact digest (ajbot.tickcache), parsing the JSON only the first time."""
    from . import tickcache
    ticks = []
    for path in sorted(_glob.glob(pattern)):
        ticks.extend(tickcache.raw_ticks(path))
    return stabilize(ticks)


# ---------------------------------------------------------------- sides ----

def _flag(rec):
    return 1 if rec.get("enemy") else 0


def resolve(tick):
    """(me_key, enemy_key, mine_keys, theirs_keys) for a tick, repaired.

    Recordings made without --pick trusted a rule that pinned any fighter with
    a live lever as ours. The CPU drives a lever too, so its fighters were
    pinned onto our side one by one: five random runs sit at 90-99% of ticks
    with no opponent, and `me` pointed at the CPU for long stretches. Those
    ticks are recoverable, because two things in the raw record still hold -
    the enemy flag splits the fighters into two teams, and the CPU's point
    character reports ai=1, which says which of the two teams is the CPU's.

    Healthy ticks are returned as recorded.
    """
    cached = tick.get("_res")
    if cached is not None:
        return cached
    chars = tick.get("chars", {})
    me, en = tick.get("me"), tick.get("enemy")
    mine = list(tick.get("mine") or [])
    theirs = list(tick.get("theirs") or [])

    mrec = chars.get(me) if me else None
    broken = (not en) or (bool(mrec and mrec.get("ai"))
                          and not tick.get("selfplay"))
    if broken:
        fresh = {k: r for k, r in chars.items()
                 if r.get("age", 1e9) <= 60 and r.get("active")}
        cpu = [_flag(r) for r in fresh.values() if r.get("ai")]
        if cpu:
            cpu_flag = 1 if sum(cpu) * 2 >= len(cpu) else 0
            ours = [(k, r) for k, r in fresh.items()
                    if _flag(r) != cpu_flag and not r.get("ai")]
            foes = [(k, r) for k, r in fresh.items() if _flag(r) == cpu_flag]
            rank_me = lambda kr: (1 if kr[1].get("sk") else 0,
                                  0 if kr[1].get("lon") else 1,
                                  kr[1].get("age", 1e9))
            rank_en = lambda kr: (0 if kr[1].get("ai") else 1,
                                  1 if kr[1].get("sk") else 0,
                                  kr[1].get("age", 1e9))
            if ours:
                me = min(ours, key=rank_me)[0]
            if foes:
                en = min(foes, key=rank_en)[0]
            live = {k: r for k, r in chars.items()
                    if r.get("age", 1e9) <= BENCH_AGE}
            mine = [k for k, r in live.items() if _flag(r) != cpu_flag]
            theirs = [k for k, r in live.items() if _flag(r) == cpu_flag]

    if not mine and not theirs:
        # Older recordings carry no team lists; the enemy flag is the same
        # rule sampler.teams falls back to.
        for k, r in chars.items():
            if r.get("age", 1e9) <= BENCH_AGE:
                (theirs if r.get("enemy") else mine).append(k)

    out = (me, en, mine, theirs)
    tick["_res"] = out
    return out


# Acts that are not play: the intro, the result screen's poses.
DEMO_ACTS = {None, "None", "AppearanceDemo", "WinDemo"}
PLAY_START_ACT_N = 100


def play_start(ticks):
    """Index of the first tick where the fight is really on: someone on point
    has just begun a real act. Before it a bot recording holds the previous
    match's lingering fighters (victory pose, act_n in the hundreds) and the
    intro / countdown, where every press does nothing - samples from there
    taught 'press Melee' for no reason."""
    for i, t in enumerate(ticks):
        for which in ("me", "enemy"):
            r = _rec(t, which)
            if (r and r.get("act") not in DEMO_ACTS
                    and (r.get("act_n") or 1e9) < PLAY_START_ACT_N):
                return i
    return len(ticks)


def stabilize(ticks):
    """Pin `me` and `enemy` to the fighters actually on point, in place.

    The recorded pointers are "the freshest active record on each side", and
    several characters are active at once (assists, transformations, a second
    lua_State), so the pointer flickers: 371 flips in one 7-minute human match.
    Worse, starting an action is what makes a state freshest, so flips land
    exactly on action changes - 224 of that match's 317 "decisions" were one
    fighter's ongoing move credited as a new decision by a team-mate.

    The controlled fighter is the one whose lever is live (only it reads a
    stick), carried forward through neutral stick. The opponent's point is its
    ai=1 fighter when the CPU plays, otherwise the recorded pointer held until
    a different fighter has been freshest for several ticks running.
    """
    if not ticks or "_stable" in ticks[0]:
        return ticks
    cur_me = cur_en = None
    pend_en, pend_n = None, 0
    for t in ticks:
        chars = t.get("chars", {})
        me, en, mine, theirs = resolve(t)
        side = set(mine) | ({me} if me else set())
        live = None
        best = 0.0
        for k in side:
            r = chars.get(k) or {}
            if r.get("age", 1e9) > 60 or r.get("ai") or r.get("sk"):
                continue
            s = float(r.get("lscale") or 0.0) if r.get("lon") else 0.0
            if s > best:
                live, best = k, s
        if live:
            cur_me = live
        elif cur_me is None or (chars.get(cur_me) or {}).get("age", 1e9) > MAX_AGE:
            cur_me = me
        # opponent
        cpu = [k for k in (set(theirs) | ({en} if en else set()))
               if (chars.get(k) or {}).get("ai")
               and (chars.get(k) or {}).get("age", 1e9) <= 60]
        if cpu:
            if cur_en not in cpu:
                cur_en = min(cpu, key=lambda k: (
                    1 if (chars.get(k) or {}).get("sk") else 0,
                    0 if k == en else 1,
                    (chars.get(k) or {}).get("age", 1e9)))
        elif en != cur_en:
            if en == pend_en:
                pend_n += 1
            else:
                pend_en, pend_n = en, 1
            if cur_en is None or pend_n >= 8 or \
                    (chars.get(cur_en) or {}).get("age", 1e9) > MAX_AGE:
                cur_en = en
        t["_res"] = (cur_me, cur_en, mine, theirs)
    _normalize_act_n(ticks)
    ticks[0]["_stable"] = True
    return ticks


def act_rate(ticks):
    """Sampler pumps per second in a recording: the median rate at which a
    fighter's act_n climbs while its act stays the same. None when there is
    too little to tell (recordings older than act_n)."""
    rates = []
    for a, b in zip(ticks, ticks[1:]):
        ta, tb = a.get("t"), b.get("t")
        if not isinstance(ta, (int, float)) or not isinstance(tb, (int, float)):
            continue
        dt = tb - ta
        if not 0.01 < dt < 0.5:
            continue
        ca, cb = a.get("chars") or {}, b.get("chars") or {}
        for k, ra in ca.items():
            rb = cb.get(k)
            if not rb or not ra.get("act") or ra.get("act") != rb.get("act"):
                continue
            na, nb = ra.get("act_n"), rb.get("act_n")
            if isinstance(na, (int, float)) and isinstance(nb, (int, float)) and nb > na:
                rates.append((nb - na) / dt)
    if len(rates) < 50:
        return None
    return float(np.median(rates))


def _normalize_act_n(ticks):
    """Rescale every act_n in a recording to BOT_ACT_RATE pumps a second, in
    place. Runs once per load, from stabilize()."""
    rate = act_rate(ticks)
    if not rate:
        return
    k = BOT_ACT_RATE / rate
    if abs(k - 1.0) < 0.03:
        return
    for t in ticks:
        for r in (t.get("chars") or {}).values():
            for f in ("act_n", "mont_n"):   # mont_n: same pumps (movewindows)
                n = r.get(f)
                if isinstance(n, (int, float)):
                    r[f] = n * k


def swap_sides(ticks):
    """The same match seen from the other side: `me` becomes the opponent and
    the teams trade places, so a human's play against the bot can be learned
    from like a recording of that human (labels come from its own `cmd` and
    `act`, which the sampler records for every fighter). Returns shallow
    copies; the records themselves are shared and not changed."""
    stabilize(ticks)
    calibrate_frame(ticks)
    out = []
    for t in ticks:
        me, en, mine, theirs = resolve(t)
        c = dict(t)
        c["_res"] = (en, me, list(theirs), list(mine))
        c.pop("selfplay", None)
        out.append(c)
    return out


def _rec(tick, which):
    """The `me` or `enemy` record for a tick, or {} if stale/absent.

    Older recordings stored these pointers as the character *id*, which is '?'
    for the 22 characters with no Lua namespace - so the pointer matched no
    key whenever you were playing one of them. Those are recovered here by
    looking up the unnamed record on the matching side.
    """
    chars = tick.get("chars", {})
    me, en, _, _ = resolve(tick)
    cid = me if which == "me" else en
    rec = chars.get(cid) if cid else None

    if rec is None and cid == "?":
        want_enemy = which == "enemy"
        for r in chars.values():
            if (r.get("id") == "?" and r.get("active")
                    and bool(r.get("enemy")) == want_enemy):
                rec = r
                break

    if not rec or rec.get("age", 0) > MAX_AGE:
        return {}
    return rec


def _same(tick, key):
    """`key`'s record in another tick, or {} if absent or stale."""
    rec = (tick.get("chars") or {}).get(key) if key else None
    if not rec or rec.get("age", 0) > MAX_AGE:
        return {}
    return rec


def key_of(tick, which):
    me, en, _, _ = resolve(tick)
    return me if which == "me" else en


def _bench(tick, side):
    """Health of the team-mates not currently on point, best first."""
    chars = tick.get("chars", {})
    me, en, mine, theirs = resolve(tick)
    point, keys = (me, mine) if side == "mine" else (en, theirs)
    hps = []
    for k in keys:
        if k == point:
            continue
        r = chars.get(k) or {}
        if r.get("age", 1e9) > BENCH_AGE:
            continue
        h = r.get("hp")
        if isinstance(h, (int, float)):
            hps.append(float(h))
    hps.sort(reverse=True)
    return hps


# ---------------------------------------------------------------- labels ---

def label_of(prev, cur):
    """The decision taken between two consecutive `me` records, or None.

    A switch into a defensive action (from `act`) wins over `cmd`, because
    GetCommandId never reports defence at all.
    """
    if not cur:
        return None
    a0, a1 = (prev or {}).get("act"), cur.get("act")
    if a1 in DEFENCE_LABELS:
        started = (a1 != a0) or (cur.get("act_n", 0) < (prev or {}).get("act_n", 0))
        if started:
            return DEFENCE_LABELS[a1]
    if a1 in MOVE_LABELS:
        if (a1 != a0) or (cur.get("act_n", 0) < (prev or {}).get("act_n", 0)):
            return MOVE_LABELS[a1]
    c0, c1 = (prev or {}).get("cmd"), cur.get("cmd")
    if c1 is not None and c1 != c0:
        return c1
    # Follow-up press within an unchanging command - e.g. a melee/target-combo
    # string, where GetCommandId stays "ECommandId::Melee" across every stage.
    # ev_push (Attack.CheckAnimChangeCustomizeCommand("Push"), see sampler.py)
    # increments once per registered follow-up press, so a rising count on an
    # otherwise-unchanged cmd is itself a real decision the player made.
    # Missing on older recordings, where both sides default to 0 and this
    # never fires - unchanged behaviour for that data.
    p0 = (prev or {}).get("ev_push", 0) or 0
    p1 = cur.get("ev_push", 0) or 0
    if c1 is not None and c1 == c0:
        # A recorded follow-up choice (`incl`, newer recordings only) names the
        # button pressed - a fact. Older recordings carry no such field, and
        # nothing is guessed for them: a same-command follow-up stays a repeat of
        # that command, and ajbot.scrub decides whether such a sample is usable.
        from .roster import base_name
        follow = FOLLOW_UP.get(base_name(cur.get("id")) or "", {}).get(c1)
        inc, pinc = cur.get("incl"), (prev or {}).get("incl")
        if follow and inc in INCL_BUTTON and inc != pinc:
            return follow + INCL_BUTTON[inc]
        if p1 > p0:
            return c1
    return None


def refine_dash(label, ticks, i, key):
    """'Dash' -> 'DashForward' / 'DashBack' from the distance trend after tick i."""
    if label != "Dash" or not key:
        return label
    r0 = _same(ticks[i], key)
    d0, t0 = r0.get("dist"), ticks[i].get("t")
    if d0 is None or not isinstance(t0, (int, float)):
        return label
    for j in range(i + 1, min(i + 60, len(ticks))):
        tj = ticks[j].get("t")
        if not isinstance(tj, (int, float)) or tj - t0 < DASH_LOOK_S:
            continue
        rj = _same(ticks[j], key)
        dj = rj.get("dist") if rj else None
        if dj is None:
            return label
        if dj - d0 <= -DASH_DIR_MIN:
            return "DashForward"
        if dj - d0 >= DASH_DIR_MIN:
            return "DashBack"
        return label
    return label


def vocabularies(ticks):
    """(actions, characters) as ordered lists, most frequent first."""
    acts, chars = vocab_counts(ticks)
    order = lambda d: [k for k, _ in sorted(d.items(), key=lambda kv: -kv[1])]
    return order(acts), order(chars)


def vocab_counts(ticks):
    """({label: count}, {character: count}) for one recording.

    Labels are counted the way the action head sees them (label_of), so the
    defensive labels derived from `act` enter the vocabulary too.
    """
    stabilize(ticks)
    acts, chars = {}, {}
    prev = None
    for i, t in enumerate(ticks):
        me = _rec(t, "me")
        lab = label_of(prev, me)
        if lab == "Dash":
            lab = refine_dash(lab, ticks, i, key_of(t, "me"))
        if lab:
            acts[lab] = acts.get(lab, 0) + 1
        prev = me
        for which in ("me", "enemy"):
            r = _rec(t, which)
            if r.get("cmd"):
                acts.setdefault(r["cmd"], 0)
            cid = key_of(t, which)
            if cid:
                cid = cid.split("#", 1)[0]      # a mirror's second copy is the same character
                chars[cid] = chars.get(cid, 0) + 1
    return acts, chars


# -------------------------------------------------------------- features ---

def _num(rec, field):
    v = rec.get(field)
    if not isinstance(v, (int, float)):
        return 0.0
    return float(v) / SCALE.get(field, 1.0)


# Act classes that mean "committed to an attack, not free to react yet" -
# shared between the history features (has the opponent been attacking
# lately) and the whiff-punish reward (did they just stop).
ATTACK_CLASSES = ("attack", "combo", "unblock", "pu")


def _history_feats(history):
    """3 numbers per tick of `history` (oldest first, already truncated to
    HISTORY_K and to one fighter's streak): distance, and whether the
    opponent was mid attack / guarding then. Missing ticks (the streak is
    younger than HISTORY_K, or there is no history at all - live play's
    first decisions) pad with zeros, distinct from a real point-blank 0.0
    distance because real ticks also carry a fourth "present" flag.
    """
    feats = []
    pad = HISTORY_K - len(history or [])
    feats += [0.0, 0.0, 0.0, 0.0] * max(0, pad)
    for prev_t in (history or [])[-HISTORY_K:]:
        pme, pen = _rec(prev_t, "me"), _rec(prev_t, "enemy")
        dist = pme.get("dist")
        cls = act_class(pen.get("act")) if "act" in pen else None
        feats += [
            float(dist) / 1000.0 if isinstance(dist, (int, float)) else 0.0,
            1.0 if cls in ATTACK_CLASSES else 0.0,
            1.0 if cls == "guard" else 0.0,
            1.0,        # this history slot is real, not padding
        ]
    return feats


def featurize(tick, actions, chars, prev_cmd=None, history=None, opp=None, route=None,
              wall=None):
    """Feature vector for one tick. `prev_cmd` is the last command that
    differed from the current one - the combo so far, one step back.
    `history` is up to HISTORY_K previous ticks of the SAME fighter's streak,
    oldest first - see _history_feats."""
    me, en = _rec(tick, "me"), _rec(tick, "enemy")
    aidx = {a: i for i, a in enumerate(actions)}
    cidx = {c: i for i, c in enumerate(chars)}

    feats = [_num(me, f) for f in ME_FIELDS]
    feats += [_num(en, f) for f in ENEMY_FIELDS]

    # Stick direction as sin/cos: -179 and +179 degrees are neighbours, and a
    # raw angle would put them at opposite ends of the range. Both frames are
    # given - character-relative (lang) and camera-relative (lcam) - since the
    # target is camera-relative but the tactical meaning is character-relative.
    for field in ("lang", "lcam"):
        v = me.get(field)
        rad = np.deg2rad(v if isinstance(v, (int, float)) else 0.0)
        feats += [float(np.sin(rad)), float(np.cos(rad))]

    dist = me.get("dist", 0.0) if isinstance(me.get("dist"), (int, float)) else 0.0
    dist2 = me.get("dist2", 0.0) if isinstance(me.get("dist2"), (int, float)) else 0.0
    feats += [dist / 1000.0, min(dist, 300.0) / 300.0, dist2 / 1000.0,
              1.0 if en else 0.0]

    # What each fighter is DOING, from the action wrappers: in hitstun,
    # guarding, mid target-combo, charging an unblockable... This is the
    # single biggest gap the model had - the opponent's cmd is None in most
    # recordings, and `stun`/`kb` never fire - so it could not tell an
    # opponent winding up from one standing still.
    has_act = 1.0 if ("act" in me or "act" in en) else 0.0
    for rec in (me, en):
        cls = act_class(rec.get("act")) if "act" in rec else None
        feats += [1.0 if cls == c else 0.0 for c in ACT_CLASS_NAMES]
        n = rec.get("act_n")
        feats.append(min(float(n), 60.0) / 60.0 if isinstance(n, (int, float)) else 0.0)
    feats.append(has_act)

    # Team context: who is left on the bench and in what shape. Tagging and
    # "bank the hurt character" both depend on it, and it was invisible.
    for side in ("mine", "theirs"):
        b = _bench(tick, side)
        feats += [b[0] if len(b) > 0 else 0.0, b[1] if len(b) > 1 else 0.0,
                  sum(1 for h in b if h > 0.001) / 2.0]
    my_hp = me.get("hp") if isinstance(me.get("hp"), (int, float)) else 0.0
    en_hp = en.get("hp") if isinstance(en.get("hp"), (int, float)) else 0.0
    feats.append(float(my_hp) - float(en_hp))

    def onehot(vocab_idx, key, n):
        v = [0.0] * n
        if key in vocab_idx:
            v[vocab_idx[key]] = 1.0
        return v

    feats += onehot(aidx, me.get("cmd"), len(actions))
    feats += onehot(aidx, en.get("cmd"), len(actions))
    feats += onehot(aidx, prev_cmd, len(actions))
    feats += onehot(cidx, (key_of(tick, "me") or "").split("#", 1)[0], len(chars))
    feats += onehot(cidx, (key_of(tick, "enemy") or "").split("#", 1)[0], len(chars))
    feats += _history_feats(history)
    feats += _extra_feats(me, en)
    feats += _geometry_feats(me, en)
    from .oppmodel import N_FEATS
    feats += list(opp) if opp is not None else [0.0] * N_FEATS
    feats += _slow_feats(me, en)
    from .movewindows import features as _win_feats
    feats += _win_feats(me, en)
    feats += list(route) if route is not None else [0.0] * _ROUTE_N
    feats += list(wall) if wall is not None else [0.0] * _WALL_N
    return np.array(feats, dtype=np.float32)


def _slow(rec):
    """How slowed a fighter is: 1 - its custom time dilation (0.9 during the
    ~1 s strong-hit slow-mo, where tdil reads 0.1), 0 when not read."""
    d = rec.get("tdil")
    if not isinstance(d, (int, float)) or d <= 0:
        return 0.0
    return float(min(max(1.0 - d, 0.0), 1.0))


def _slow_feats(me, en):
    """The slow-motion moments (just guard, counter, clash, escape, Tag Combo
    confirm): who is slowed, and so who has the free punish. Recorded since
    2026-11-09 (hslow/tdil); older recordings read 0 throughout."""
    ms, es = _slow(me), _slow(en)
    return [_num(me, "hslow"), ms, _num(en, "hslow"), es, es - ms,
            _num(me, "ok_extreme"), _num(en, "ok_extreme")]


def _geometry_feats(me, en=None):
    """Opponent-relative geometry. lever_rel is where the stick points
    relative to the opponent (0 = straight at them, 180 = straight away) - the
    camera-relative lcam above means the opposite thing on each player side.
    tbear is where the opponent is relative to our facing (are we turned
    toward them)."""
    lc = me.get("lcam")
    lever = wrap180(lc - tcam_of(me, en)) if isinstance(lc, (int, float)) else 0.0
    tb = me.get("tbear")
    has = isinstance(tb, (int, float))
    tb = float(tb) if has else 0.0
    # The opponent's own bearing to its target (us): is it turned toward us,
    # i.e. can what it is doing actually reach us.
    etb = (en or {}).get("tbear")
    etb = float(etb) if isinstance(etb, (int, float)) else 0.0
    return [float(np.sin(np.deg2rad(lever))), float(np.cos(np.deg2rad(lever))),
            float(np.sin(np.deg2rad(tb))), float(np.cos(np.deg2rad(tb))), 1.0 if has else 0.0,
            float(np.sin(np.deg2rad(etb))), float(np.cos(np.deg2rad(etb))),
            _threat(me, en)]


def _threat(me, en):
    """How likely the opponent's CURRENT move is to hit us from here: the move
    table's hit rate for that move at this distance, while it is attacking;
    0 when it is not. The model saw the opponent's act class and distance
    separately, never how dangerous that pair is for that character."""
    a = (en or {}).get("act") or ""
    if not a.startswith("Attack") or act_class(a) not in ATTACK_CLASSES:
        return 0.0
    from . import moveinfo
    from .roster import base_name
    mi = moveinfo.get()
    if not mi:
        return 0.0
    label = a[len("Attack"):]
    for suffix in ("Air", "_0", "_1", "_2"):
        if label.endswith(suffix):
            label = label[: -len(suffix)]
    p = mi.p_hit(base_name(en.get("key")) or en.get("key"), label, me.get("dist"))
    return float(p) if p is not None else 0.0


def _extra_feats(me, en):
    """Move knowledge from ajbot.moveinfo, appended to the feature vector.

    reach[8]  how likely each main attack is to connect from the CURRENT
              distance, for this character - so "Q2 from across the stage" is
              visible as 0.1 rather than being a distance the model must
              memorise per character.
    busy      how far through its usual run each side's current act is
              (0 = just started, 1 = at its usual end, >1 = overrunning). A
              long-tailed move reads as busy for a long time: that is the
              window an opponent can be punished in, or that we cannot act in.
    """
    from . import moveinfo
    from .roster import base_name
    mi = moveinfo.get()
    if not mi:
        return [0.0] * (len(moveinfo.REACH_LABELS) + 2)
    mc = base_name(me.get("key")) or me.get("key")
    ec = base_name(en.get("key")) or en.get("key")
    dist = me.get("dist")
    out = mi.reach_feats(mc, dist)
    out.append(mi.busy(mc, me.get("act"), me.get("act_n")))
    out.append(mi.busy(ec, en.get("act"), en.get("act_n")))
    return out


def feature_names(actions, chars):
    names = [f"me.{f}" for f in ME_FIELDS] + [f"en.{f}" for f in ENEMY_FIELDS]
    names += ["me.lang_sin", "me.lang_cos", "me.lcam_sin", "me.lcam_cos"]
    names += ["dist_k", "dist_near", "dist2_k", "enemy_live"]
    for who in ("me", "en"):
        names += [f"{who}.act={c}" for c in ACT_CLASS_NAMES]
        names.append(f"{who}.act_n")
    names.append("has_act")
    for who in ("mine", "theirs"):
        names += [f"{who}.bench1_hp", f"{who}.bench2_hp", f"{who}.bench_alive"]
    names.append("hp_lead")
    names += [f"me.cmd={a}" for a in actions]
    names += [f"en.cmd={a}" for a in actions]
    names += [f"me.prev={a}" for a in actions]
    names += [f"me.char={c}" for c in chars]
    names += [f"en.char={c}" for c in chars]
    for j in range(HISTORY_K):
        names += [f"hist{j}.dist_k", f"hist{j}.en_committed",
                  f"hist{j}.en_guarding", f"hist{j}.present"]
    from . import moveinfo
    names += [f"reach.{l}" for l in moveinfo.REACH_LABELS] + ["me.busy", "en.busy"]
    names += ["me.lever_rel_sin", "me.lever_rel_cos", "me.tbear_sin", "me.tbear_cos", "me.has_bearing",
              "en.tbear_sin", "en.tbear_cos", "en.threat"]
    from .oppmodel import FEATURE_NAMES
    names += FEATURE_NAMES
    names += ["me.hslow", "me.slow", "en.hslow", "en.slow", "slow_edge",
              "me.ok_extreme", "en.ok_extreme"]
    from .movewindows import FEATURE_NAMES as WIN_NAMES
    names += WIN_NAMES
    from .routefeat import FEATURE_NAMES as ROUTE_NAMES
    names += ROUTE_NAMES
    from .wallfeat import FEATURE_NAMES as WALL_NAMES
    names += WALL_NAMES
    return names


def movement_persistence_cols(n_features=None):
    """Column indices of (lever_rel_sin, lever_rel_cos, lscale) - the 'keep
    pushing where you are pushing' baseline for the movement head, in the same
    opponent-relative frame as its target. The geometry block is the last
    eight columns before the opponent model, the slow-mo and window blocks."""
    if n_features is None:
        ns = len(ME_FIELDS) + len(ENEMY_FIELDS) + 2
        return ns, ns + 1, ME_FIELDS.index("lscale")
    from .oppmodel import N_FEATS
    tail = N_FEATS + SLOW_N + _WIN_N + _ROUTE_N + _WALL_N
    return n_features - 8 - tail, n_features - 7 - tail, ME_FIELDS.index("lscale")


# --------------------------------------------------------------- rewards ---

# Reward per event, in units of "health bars": dealing a full bar is +10.
# Just guard and counter are deliberately large - they are the highest-leverage
# plays in the game and far rarer than a hit, so at hit-sized rewards they
# would never register in the weighting.
REWARD = {
    "dealt": 10.0,        # per unit of opponent health removed
    "taken": -10.0,       # per unit of our health lost
    "ev_jg": 3.0,         # just guard
    "ev_js": 2.0,         # just step (the step-dodge equivalent)
    "ctr_hit": 3.0,       # our counter attack followed by the opponent in hitstun
    "ev_ctr": 0.3,        # any counter attempt, a small nudge to try it
    "ev_gok": 1.0,        # a guarded hit
    # The cinematic Tag Combo finisher (AttackTagCombo/Confirm, +120 PU in the
    # game's ActionParam). Before 2026-11-09 this counter watched a name that
    # never begins and read 0 everywhere; see sampler.py.
    "ev_tagc": 1.5,
    # A Change Combo (partner tagged in while the victim was in hitstun),
    # detected by ChainTracker from the partner's SidekickEnter/SwitchWait -
    # the game gives it -30 down value and +10 combo scaling, so it is the
    # mechanic that turns a 5-hit string into a 20-hit chain.
    "change_combo": 3.0,
    "ev_swap": 0.4,       # any successful change
    "ev_gbrk_taken": -2.0,    # we got guard broken (stamina break)
    # Caught by a red (an unguardable: the Y Unblockable, or any move the game
    # marks DamageParam Unguardable). Guarding one is a loss, and the hit
    # itself is cheap (~7% health), so `taken` alone taught the bots to stand
    # and guard them. The answers are to hit first, dodge or get out of the
    # way. Paid once per red that connects, on top of the damage.
    "caught_red": -4.0,
    # The counter trick (2026-11-13, pinned down with the user): a Counter
    # that lands goes into the long Counter Crash animation (our Tag Combo
    # Confirm, ~0.85 s), and the fighter cancels out of it straight into
    # its own attack instead of landing/falling out of it. Paid once per
    # cancel; the combo it opens is paid by dealt/combo_dmg as usual.
    "counter_trick": 4.0,
    # Counter -> Rising action (2026-11-13, found by the bots, confirmed by
    # the user on Deku Shoot Style): in Rising, the counter attack cancels
    # into the Rising/Extreme action (RB+A). Paid only when a real combo
    # follows it (RISE_COMBO_HITS hits on them, each within RISE_GAP_S of the
    # last), at the cancel tick; a cancel that whiffs or stops gets nothing.
    "counter_rising": 3.0,
    "ev_unbrk_taken": -1.5,   # we got hit into a non-tech-able knockdown
    "ev_esc": 0.3,        # used the emergency dodge
    # Resources the bot sat on (the 2026-11-09 pro match: Plus Ultra and
    # Rising barely used). Small nudges for using them at all; a Plus Ultra
    # that lands is paid far more by `dealt` and chain_pu.
    "ev_pu": 0.5,         # a Plus Ultra started
    "rising": 1.0,        # Rising (awakening) switched on
    "enemy_jg": -1.5,     # the opponent just-guarded us
    # Keeping the whole team alive is worth more than any single exchange: a
    # lost fighter is a lost tag partner, a lost Change Combo route and one
    # less stock. The largest single term on purpose.
    "our_death": -25.0,
    "enemy_death": 10.0,
    # Damage taken while already low costs more - the same hit that is a
    # trade at full health is the one that ends a fighter at 20%. Multiplier
    # on "taken", ramping from 1x at DANGER_HP to (1 + this) at 0 health.
    "low_hp_extra": 2.0,
    # Combo chains, counted by the game's rule (the victim never left
    # hitstun - see ajbot.chain). Each hit is worth more the deeper into the
    # combo it lands, so ten hits in one combo far outweigh five two-hit
    # combos: hit n earns combo_hit * min(n, 20).
    "combo_hit": 0.0,         # hit COUNT no longer pays: only damage does (combo_dmg)
    # Raised 2026-11-15 (user): a Change Combo at the end of a string is how
    # a team builds PU fast and pushes a combo to 1200-2400, so tagging to
    # extend is worth clearly more than ending on a lone string.
    "chain_2nd": 2.5,         # a second fighter continued the combo
    "chain_3rd": 5.0,         # a third fighter continued it
    "chain_pu": 5.0,          # Plus Ultra landed at the end of a 2-3 fighter chain
    "combo_on_us": -0.04,     # per hit of a combo on us, same depth scaling
    # Landing a hit while the opponent is NOT mid-attack but WAS within the
    # last WHIFF_PUNISH_WINDOW ticks: their attack just ended - whiffed,
    # recovering, or blocked - and the decision that capitalised on it is
    # timing, not just "I was next to them". This is the one lever that
    # rewards punish timing specifically, on top of the flat `dealt` reward
    # every hit already gets.
    "whiff_punish": 2.0,
    # Spacing has no clean "correct distance" in this data - no hitboxes, no
    # frame data - so this rewards the REACTION rather than a fixed target:
    # closing in while the opponent is not attacking (safe aggression), or
    # opening up while they are (getting out of range). Kept small - a nudge
    # on top of whatever the hit/guard/combo rewards already say, not a
    # replacement for them.
    "spacing_aggro": 0.15,
    "spacing_defense": 0.15,
    # Backing away from an opponent who is not attacking, from mid range.
    # Two bots that both retreat never fight; this makes retreating without a
    # reason cost something, so closing in is what pays.
    "flee": -0.12,
    # Combos are scored by the total damage the GAME counts, not by hits.
    # hp is 0..1 per character; a character has ~2500-3000 hp in the game's
    # own numbers (a few outliers at 4000), so 3000 is used and a "good"
    # combo of 1200-2400 is 0.4-0.8 of a bar here. combo_dmg is the bonus
    # for a combo of good_lo damage; the curve steepens through the good range
    # and flattens beyond good_hi (see combo_curve).
    "hp_units": 3000.0,
    # Per-character totals that differ from the average (roster names). Add
    # more as they are learned.
    "hp_units_by_char": {"FatGum": 4000.0, "Bakugo": 2500.0, "Mirio": 2500.0},
    "good_lo": 1200.0,
    "good_hi": 2400.0,
    "combo_dmg": 8.0,
    "combo_dmg_taken": 3.0,
    # The whole combo, paid once more at its first hit when it ends
    # (combo_open x combo_curve(total)): HORIZON_S only lets the opener see
    # 1.5 s of a combo that can run 4-6 s across three fighters, so without
    # this the decision that STARTED a 2400 combo was paid like a jab.
    "combo_open": 3.0,
    # Meter built by our combos. There is no gauge getter, only "usable now"
    # flags, so a PU level (ok_pu1/ok_pu2/ok_spu) or Rising (awk_full)
    # becoming available while our combo is landing pays once; a flag that
    # was already on within METER_FRESH_S is a state flicker, not a new level.
    "pu_built": 1.5,
    "rise_built": 1.5,
    # A hit on an opponent who has done nothing for free_lookback_s (no
    # attack, guard, dodge or movement) is a free hit, not a read: it only
    # counts free_hit as much. Without this a bot standing still against
    # another bot that stands still "earned" a huge reward for every combo.
    "free_hit": 0.35,
    "free_lookback_s": 1.2,
    # Starting an attack that connects with nothing within its own hit window
    # (moveinfo.window: ~1 s for a jab, 3-5 s for a Plus Ultra cinematic)
    # costs this, scaled by how unlikely it was to succeed from that distance
    # (moveinfo.worth: hits, setups that led to damage, safe pressure) - so
    # Q2 from across the stage is expensive and Q2 at point blank is nearly
    # free.
    "whiff_cost": 0.8,
    # Combo routes (ajbot.routes: hand-written + mined from human play by
    # ajbot.routemine). Each step that continues a known route at least
    # route_min steps deep while the combo is landing (the opponent lost
    # health within route_land_s) pays route_step; the step that finishes a
    # route pays route_done on top. So the policy is paid for learning the
    # long player routes, not only for whatever damage a jab trade made.
    "route_step": 0.4,
    "route_done": 3.0,
    "route_min": 3,
    "route_land_s": 1.0,
    # Standing idle while the opponent is stuck in hitstun within reach: the
    # combo is there to continue and we let them go (a jab, then nothing).
    # Per second, charged on the idle ticks themselves (a ~0.4 s drop costs
    # about the follow-up hit it threw away). Bots ended 12% of
    # their hitstun windows idle (2026-11-11), players almost never.
    "stun_idle": -3.0,
    "stun_reach": 400.0,
    # A dropped combo: the opponent we were hitting leaves hitstun straight
    # back into free play (idle, moving, attacking - not an escape, guard or
    # KO) before the combo reached good_lo. Scaled by how far short it fell,
    # so a full combo that ends costs nothing. Rising sprints and
    # jab-then-wander ended this way ~10x as often for bots as for players.
    # stun_drop_tech is the same for a tech (DamageRecover*) out of a short
    # string - smaller, since some techs cannot be stopped.
    "stun_drop": -1.5,
    "stun_drop_tech": -0.5,
    # A move whose own hit lands after HORIZON_S (cannon shots, PU
    # cinematics) is credited this at the press: the damage reward itself
    # arrives too late for the return to reach back to that decision.
    "late_hit": 0.8,
}
DANGER_HP = 0.4
METER_FRESH_S = 6.0
METER_FLAGS = ("ok_pu1", "ok_pu2", "ok_spu", "awk_full")
DEATH_LEAD_S = 4.0
WHIFF_PUNISH_WINDOW = 45     # ticks (~3s at 15Hz) after an attack ends
SPACING_MIN_DELTA = 15.0     # smaller distance changes are noise, not a choice


def taken_weight(hp):
    """How much one unit of damage costs at health `hp` (see low_hp_extra)."""
    if not isinstance(hp, (int, float)):
        return 1.0
    return 1.0 + REWARD["low_hp_extra"] * max(0.0, DANGER_HP - hp) / DANGER_HP
HORIZON_S = 1.5           # how far ahead a decision is credited with outcomes


def combo_curve(units):
    """Bonus shape for a combo that has dealt `units` damage (game units)."""
    lo, hi = REWARD["good_lo"], REWARD["good_hi"]
    u = units / lo
    top = hi / lo
    # Steeper than linear all the way to good_hi (2026-11-15): two 600
    # strings are worth well under one 1200 combo, and a 2400 three-fighter
    # chain is worth 3.5x a 1200 one, not 2.5x.
    if u <= 1.0:
        return u ** 1.8
    if u <= top:
        return 1.0 + (u - 1.0) * 2.5
    return 1.0 + (top - 1.0) * 2.5 + (u - top) * 0.75


_AGENT_ACTS = ("Run", "Dash", "Jump", "Walk", "Step", "Dodge", "Parkour", "AirChase")
_AGENT_CLASSES = ("combo", "attack", "unblock", "pu", "counter", "escape",
                  "assist", "rising", "guard")


def hp_units_of(key):
    """Total health of a character in the game's own numbers."""
    from .roster import base_name
    return REWARD["hp_units_by_char"].get(base_name(key) or key, REWARD["hp_units"])


def has_agency(rec):
    """True when this fighter is visibly doing something - attacking, guarding,
    dodging or moving - rather than standing there."""
    a = rec.get("act") or ""
    if act_class(a) in _AGENT_CLASSES or a.startswith(_AGENT_ACTS):
        return True
    ls = rec.get("lscale")
    return isinstance(ls, (int, float)) and ls > 15


RED_GRACE_S = 0.25         # a hit this long after the red ended still counts
CRASH_TDIL = 0.15          # both fighters this slowed + them hit = Counter Crash
TRICK_LEAD_S = 0.6         # Tag Combo this soon after the crash = the Counter Crash
CTR_LEAD_S = 0.5           # counter attack this soon after our COUNTER pick
RISE_COMBO_HITS = 2        # hits after the counter->Rising cancel = a combo
RISE_GAP_S = 1.0           # combo over: no new hit on them for this long


def is_red(rec):
    """Is this fighter mid an unguardable ("red") attack? The Unblockable
    itself, or a special the game's DamageParam marks Unguardable (grabs,
    armoured rushes, some Plus Ultras and Rising moves; models/movedata.json).
    Normal-attack strings are left out: one unguardable hit somewhere in a
    string (Stain's) would otherwise make the whole jab string a red."""
    act = (rec or {}).get("act")
    if not isinstance(act, str):
        return False
    cls = act_class(act)
    if cls == "unblock":
        return True
    if cls not in ("attack", "pu", "rising"):
        return False
    try:
        from . import movedata
        md = movedata.get().move(rec.get("key") or rec.get("id"), act)
    except Exception:
        return False
    return bool(md and md.get("ub"))


def rewards(ticks):
    """Immediate reward per tick, from OUR side's point of view.

    Health is read per character on the monotone envelope (combos.Health),
    because several characters own two live lua_States that the recorder
    alternates between, which reads as a stream of fake hits on raw deltas.
    """
    from .combos import Health
    from .sampler import Envelope
    stabilize(ticks)
    health = Health()
    env = Envelope()
    r = np.zeros(len(ticks), dtype=np.float32)
    ctr_pending = {}          # our key -> ticks left to see the counter land
    from .chain import ChainTracker
    chain = ChainTracker()
    seen_hits = {}            # id(combo) -> hits already rewarded
    seen_chars = {}           # id(combo) -> fighters already rewarded
    seen_tags = {}            # id(combo) -> Change Combos already rewarded
    down = set()              # fighters currently KO'd
    alive = set()             # fighters seen healthy, so a KO is a real one
    deaths = []               # (tick, reward)
    last_attack_tick = {}     # their key -> last tick index seen attacking
    last_dist = {}            # our key -> distance last tick we were on point
    last_me = None            # so a tag-in never diffs against a stale reading
    prev_now = None           # previous tick's time (stun_idle is per second)
    spell = None              # {"k", "hp0", "ours"}: the opponent's current hitstun
    from . import moveinfo
    mi = moveinfo.get()
    last_agency = {}          # fighter key -> time it last did something
    combo_st = {}             # victim -> {"dmg", "t", "factor", "i0"} for our combos
    combos_done = []          # finished combo_st entries (combo_open)
    meter_off = set()         # (our key, flag) seen off while free to use it
    taken_st = {}             # our victim -> {"dmg", "t"}
    decisions = []            # (tick, char, label, dist) attack starts by us
    dealt_ticks = []          # ticks where the opponent lost health
    prev_me_rec = None
    prev_cls = {}             # fighter key -> act class last tick (Rising starts)
    red = None                # {"k", "t", "hit"}: the opponent's current red
    crash_t = -1e9            # last Counter Crash slow-mo (us countering them)
    prev_me_act = None
    clash = None              # fighter key in our Counter Crash animation
    ctr_pick = (None, -1e9)   # (fighter, time) of our last COUNTER pick
    ctr_atk = None            # fighter key in a counter attack (AttackExMelee)
    rise = None               # {"k", "i", "t", "hp", "hits", "drop"}: cancel waiting on a combo
    from .roster import base_name, move_char
    for i, t in enumerate(ticks):
        chars = t.get("chars", {})
        now_t = t.get("t", i / 15.0)
        me, en, mine, theirs = resolve(t)
        mine_s, theirs_s = set(mine), set(theirs)
        if me:
            mine_s.add(me)
        if en:
            theirs_s.add(en)
        val = 0.0
        enemy_hit = False

        if me:
            dist = (chars.get(me) or {}).get("dist")
            prev_dist = last_dist.get(me) if me == last_me else None
            if isinstance(dist, (int, float)) and prev_dist is not None:
                en_cls = act_class((chars.get(en) or {}).get("act")) \
                    if en and "act" in (chars.get(en) or {}) else None
                delta = dist - prev_dist
                if abs(delta) >= SPACING_MIN_DELTA:
                    if en_cls in ATTACK_CLASSES and delta > 0:
                        val += REWARD["spacing_defense"]
                    elif en_cls not in ATTACK_CLASSES and delta < 0:
                        val += REWARD["spacing_aggro"]
                    elif (en_cls not in ATTACK_CLASSES and delta > 0
                          and dist > 250.0 and en_cls != "damage"):
                        val += REWARD["flee"]
            if isinstance(dist, (int, float)):
                last_dist[me] = dist
        last_me = me
        if me and en and prev_now is not None:
            mr, er = chars.get(me) or {}, chars.get(en) or {}
            d = mr.get("dist")
            if (act_class(mr.get("act")) == "idle" and act_class(er.get("act")) == "damage"
                    and isinstance(d, (int, float)) and d <= REWARD["stun_reach"]
                    and (er.get("hp") or 0) > 0):
                val += REWARD["stun_idle"] * min(0.25, max(0.0, now_t - prev_now))
        prev_now = now_t
        # The counter trick: Counter Crash (both slowed, them in hitstun),
        # its long animation (our Tag Combo Confirm), and our fighter
        # leaving it straight into an attack of its own.
        if me and en:
            mr, er = chars.get(me) or {}, chars.get(en) or {}
            md_, ed_ = mr.get("tdil"), er.get("tdil")
            if (isinstance(md_, (int, float)) and isinstance(ed_, (int, float))
                    and 0 < md_ < CRASH_TDIL and 0 < ed_ < CRASH_TDIL
                    and act_class(er.get("act")) == "damage"):
                crash_t = now_t
            a = mr.get("act")
            if (a == "AttackTagComboConfirm" and prev_me_act != a
                    and now_t - crash_t <= TRICK_LEAD_S):
                clash = me
                crash_t = -1e9    # once per counter
            elif clash is not None and a != "AttackTagComboConfirm":
                if me == clash and str(a).startswith("Attack"):
                    val += REWARD["counter_trick"]
                clash = None
            # Counter attack cancelled into the Rising action.
            if t.get("defense") == "COUNTER":
                ctr_pick = (me, now_t)
            if a == "AttackExMelee" and prev_me_act != a:
                ctr_atk = me if (ctr_pick[0] == me
                                 and now_t - ctr_pick[1] <= CTR_LEAD_S) else None
            elif a == "AttackExtremeAction" and prev_me_act == "AttackExMelee" \
                    and ctr_atk == me:
                h0 = er.get("hp")
                if isinstance(h0, (int, float)):
                    rise = {"k": en, "i": i, "t": now_t, "hp": h0, "hits": 0, "drop": False}
                ctr_atk = None
            elif a != "AttackExMelee":
                ctr_atk = None
            prev_me_act = a
        if rise is not None:
            h = (chars.get(rise["k"]) or {}).get("hp")
            if isinstance(h, (int, float)) and h < rise["hp"] - 0.003:
                if not rise["drop"]:
                    rise["hits"] += 1
                rise["drop"], rise["t"], rise["hp"] = True, now_t, h
            else:
                rise["drop"] = False
            if now_t - rise["t"] > RISE_GAP_S:
                if rise["hits"] >= RISE_COMBO_HITS:
                    r[rise["i"]] += REWARD["counter_rising"]
                rise = None
        if en and is_red(chars.get(en)):
            if red is None or red["k"] != en or now_t - red["t"] > RED_GRACE_S:
                red = {"k": en, "t": now_t, "hit": False}
            else:
                red["t"] = now_t
        if en:
            er = chars.get(en) or {}
            ecls = act_class(er.get("act")) if "act" in er else None
            ehp = er.get("hp")
            if ecls == "damage" and isinstance(ehp, (int, float)):
                if spell is None or spell["k"] != en:
                    spell = {"k": en, "hp0": ehp, "ours": False}
                if me and act_class((chars.get(me) or {}).get("act")) in ATTACK_CLASSES:
                    spell["ours"] = True
            elif spell is not None and spell["k"] == en and ecls is not None:
                lost = max(0.0, spell["hp0"] - (ehp if isinstance(ehp, (int, float)) else 0.0))
                short = max(0.0, 1.0 - lost * hp_units_of(en) / REWARD["good_lo"])
                mcls = act_class((chars.get(me) or {}).get("act")) if me else None
                if (spell["ours"] and short > 0 and isinstance(ehp, (int, float))
                        and ehp > 0.001 and mcls != "damage"):
                    if ecls == "recovered":
                        val += REWARD["stun_drop_tech"] * short
                    elif ecls not in ("escape", "guard", "rising", "pu", "assist"):
                        val += REWARD["stun_drop"] * short
                spell = None
        me_rec = _rec(t, "me")
        if me_rec:
            lab = label_of(prev_me_rec, me_rec)
            if (lab and lab not in moveinfo.NON_ATTACK
                    and not lab.startswith("ExUnique2Follow")):
                decisions.append((i, move_char(me_rec), lab, me_rec.get("dist")))
        prev_me_rec = me_rec
        for k, rec in chars.items():
            if k in theirs_s and has_agency(rec):
                last_agency[k] = now_t
            cls_k = act_class(rec.get("act")) if "act" in rec else None
            attacking = cls_k in ATTACK_CLASSES
            if attacking:
                last_attack_tick[k] = i
            if (k in mine_s and cls_k == "rising" and prev_cls.get(k, "rising") != "rising"
                    and rec.get("age", 1e9) <= 60):
                val += REWARD["rising"]
            if cls_k is not None:
                prev_cls[k] = cls_k
            if k in mine_s:
                # A meter level reached during our combo. The flags also drop
                # while a move is unusable for state reasons (mid-attack,
                # hitstun), so "off" only counts when seen in free play.
                for f in METER_FLAGS:
                    v = rec.get(f)
                    if v == 0 and cls_k not in ATTACK_CLASSES and cls_k != "damage":
                        meter_off.add((k, f))
                    elif v == 1 and (k, f) in meter_off:
                        meter_off.discard((k, f))
                        if any(now_t - s["t"] <= 2.0 for s in combo_st.values()):
                            val += REWARD["rise_built" if f == "awk_full" else "pu_built"]
            hp = rec.get("hp")
            if isinstance(hp, (int, float)):
                d = health.drop(k, hp)
                if d > 0:
                    if k in theirs_s:
                        st = combo_st.get(k)
                        if st is None or now_t - st["t"] > 1.5:
                            if st is not None:
                                combos_done.append(st)
                            la = last_agency.get(k)
                            free = la is not None and now_t - la > REWARD["free_lookback_s"]
                            st = {"dmg": 0.0, "t": now_t, "i0": i, "hu": hp_units_of(k),
                                  "factor": REWARD["free_hit"] if free else 1.0}
                            combo_st[k] = st
                        hu = hp_units_of(k)
                        before_u = st["dmg"] * hu
                        st["dmg"] += d
                        st["t"] = now_t
                        after_u = st["dmg"] * hu
                        dealt_ticks.append(i)
                        val += st["factor"] * (
                            REWARD["dealt"] * d + REWARD["combo_dmg"] *
                            (combo_curve(after_u) - combo_curve(before_u)))
                        # Their attack just ended (whiffed, recovering, or
                        # blocked) and we still landed something on them.
                        last = last_attack_tick.get(k)
                        if not attacking and last is not None and i - last <= WHIFF_PUNISH_WINDOW:
                            val += REWARD["whiff_punish"]
                    elif k in mine_s:
                        val += REWARD["taken"] * d * taken_weight(hp)
                        if (red is not None and not red["hit"]
                                and now_t - red["t"] <= RED_GRACE_S):
                            val += REWARD["caught_red"]
                            red["hit"] = True
                        st = taken_st.get(k)
                        if st is None or now_t - st["t"] > 1.5:
                            st = {"dmg": 0.0, "t": now_t}
                            taken_st[k] = st
                        hu = hp_units_of(k)
                        b0 = st["dmg"] * hu
                        st["dmg"] += d
                        st["t"] = now_t
                        val -= REWARD["combo_dmg_taken"] * (
                            combo_curve(st["dmg"] * hu) - combo_curve(b0))
                # A death: health reaching zero (or the dead flag), once per
                # fighter until it is healthy again (training-mode refills).
                gone = bool(rec.get("dead")) or hp <= 0.001
                if gone and k not in down and k in alive:
                    down.add(k)
                    if k in mine_s:
                        deaths.append((i, REWARD["our_death"]))
                    elif k in theirs_s:
                        deaths.append((i, REWARD["enemy_death"]))
                elif not gone and hp > 0.2:
                    down.discard(k)
                    alive.add(k)
            if "ev_hit" not in rec:
                continue           # recorded before the action wrappers
            d = lambda f: env.delta(k, f, rec.get(f))
            if k in mine_s:
                for f in ("ev_jg", "ev_js", "ev_gok", "ev_tagc",
                          "ev_swap", "ev_esc", "ev_pu"):
                    val += REWARD[f] * d(f)
                val += REWARD["ev_gbrk_taken"] * d("ev_gbrk")
                val += REWARD["ev_unbrk_taken"] * d("ev_unbrk")
                if d("ev_ctr"):
                    val += REWARD["ev_ctr"]
                    ctr_pending[k] = 20
                d("ev_hit")
            elif k in theirs_s:
                val += REWARD["enemy_jg"] * d("ev_jg")
                if d("ev_hit"):
                    enemy_hit = True
        if enemy_hit and ctr_pending:
            val += REWARD["ctr_hit"]
            ctr_pending.clear()
        for k in list(ctr_pending):
            ctr_pending[k] -= 1
            if ctr_pending[k] <= 0:
                del ctr_pending[k]
        # Combo chains. Per-hit weight is scaled by the attacker's dmg_pow at
        # the moment of the hit (damage.GetDamagePower, see sampler.py) - a
        # move-intrinsic power rating, not the same thing as the real HP loss
        # `dealt`/`taken` already reward, so this doesn't double-count damage.
        # It teaches landing a heavier finisher within a combo is worth more
        # than padding hit-count with weak jabs. Missing on older recordings,
        # where it defaults to 1.0 - identical to the old flat weighting.
        chain.update(chars, mine_s, theirs_s, me, en, t.get("t", i / 15.0))
        for c, sign in ((chain.by_us, 1.0), (chain.on_us, -1.0)):
            if c is None:
                continue
            cid = id(c)
            before = seen_hits.get(cid, 0)
            attacker = me if sign > 0 else en
            pw = (chars.get(attacker) or {}).get("dmg_pow") if attacker else None
            pow_mult = pw if isinstance(pw, (int, float)) and pw > 0 else 1.0
            for n in range(before + 1, c.hits + 1):
                w = REWARD["combo_hit"] if sign > 0 else -REWARD["combo_on_us"]
                val += sign * w * min(n, 20) * pow_mult
            seen_hits[cid] = c.hits
            if sign > 0:
                had = seen_chars.get(cid, 0)
                if c.chars >= 2 > had:
                    val += REWARD["chain_2nd"]
                if c.chars >= 3 > had:
                    val += REWARD["chain_3rd"]
                seen_chars[cid] = max(had, c.chars)
                # A Change Combo into our combo (a partner tagged in while the
                # victim was still in hitstun - ChainTracker reads it off the
                # partner's SidekickEnter/SwitchWait, so it works on every
                # recording with `act`). The old ev_tagc term never fired: that
                # counter watched the cinematic Tag Combo action, not Changes.
                tagged = seen_tags.get(cid, 0)
                if c.change_combos > tagged:
                    val += REWARD["change_combo"] * (c.change_combos - tagged)
                    seen_tags[cid] = c.change_combos
                if c.chars >= 2 and me and env.delta(("pu", me), "ev_pu",
                                                      (chars.get(me) or {}).get("ev_pu")):
                    val += REWARD["chain_pu"]
        r[i] = val

    if rise is not None and rise["hits"] >= RISE_COMBO_HITS:
        r[rise["i"]] += REWARD["counter_rising"]

    # The whole combo, credited to its first hit (see combo_open).
    for st in combos_done + list(combo_st.values()):
        r[st["i0"]] += REWARD["combo_open"] * st["factor"] * combo_curve(st["dmg"] * st["hu"])

    # Attack starts that connected with nothing. Charged to the decision tick
    # itself, scaled by how unlikely the move was to reach from that distance.
    if decisions:
        import bisect
        ts_all = [t.get("t", j / 15.0) for j, t in enumerate(ticks)]
        decision_ticks = [d[0] for d in decisions]
        for i, char, lab, dist in decisions:
            win = mi.window(char, lab) if mi else moveinfo.HIT_WINDOW_S
            j = bisect.bisect_left(dealt_ticks, i + 1)
            if j < len(dealt_ticks):
                lat = ts_all[dealt_ticks[j]] - ts_all[i]
                k = bisect.bisect_left(decision_ticks, i + 1)
                own = k >= len(decision_ticks) or decision_ticks[k] >= dealt_ticks[j]
                if lat <= win:
                    if own and lat > HORIZON_S:
                        r[i] += REWARD["late_hit"]
                    continue                   # it connected
            p = mi.worth(char, lab, dist) if mi else None
            r[i] -= REWARD["whiff_cost"] * (0.5 if p is None else 1.0 - min(1.0, p))

    # A KO is the end of a sequence of mistakes, not of the final hit alone:
    # spread it over the DEATH_LEAD_S seconds before, so the decisions that
    # stayed in instead of tagging out or escaping carry the cost.
    ts = [t.get("t", j / 15.0) for j, t in enumerate(ticks)]
    for i, value in deaths:
        j = i
        while j > 0 and ts[i] - ts[j - 1] <= DEATH_LEAD_S:
            j -= 1
        span = i - j + 1
        r[j:i + 1] += value / span
    _route_reward(ticks, r, dealt_ticks, ts)
    return r


_ROUTES = None


def route_book():
    """{character: [step tuple]}: the routes the follower plays (authored +
    the active data folder's mined routes), as the label stream shows them -
    a Wait step presses nothing and has no label, so it is left out."""
    global _ROUTES
    if _ROUTES is None:
        from . import routes as R
        book = {}
        for char, items in ((R.load_all() or {}).get("chars") or {}).items():
            seqs = [tuple(x for x in it.get("steps") or () if x != R.WAIT)
                    for it in items if not it.get("disabled")]
            seqs = [q for q in seqs if len(q) >= REWARD["route_min"]]
            if seqs:
                book[char] = seqs
        _ROUTES = book
    return _ROUTES


_FEATURE_BOOK = _ROUTE_INDEX = None


def feature_book():
    """{character: [step tuple]} for the route-progress features
    (ajbot.routefeat): the reward's route book plus every library combo the
    drill landed at least once. Stored in the trained policy, so live play
    reads the same book."""
    global _FEATURE_BOOK
    if _FEATURE_BOOK is None:
        from . import combodb
        from . import routes as R
        book = {c: list(v) for c, v in route_book().items()}
        try:
            lib = combodb.routes_for_bot(landed=True)
        except (OSError, ValueError):
            lib = {}
        for char, items in lib.items():
            have = set(book.get(char, ()))
            for it in items:
                q = tuple(x for x in it.get("steps") or () if x != R.WAIT)
                if len(q) >= 2 and q not in have:
                    book.setdefault(char, []).append(q)
                    have.add(q)
        _FEATURE_BOOK = book
    return _FEATURE_BOOK


def route_index():
    global _ROUTE_INDEX
    if _ROUTE_INDEX is None:
        from .routefeat import Index
        _ROUTE_INDEX = Index(feature_book())
    return _ROUTE_INDEX


def route_fingerprint():
    """Changes when the route books do (part of the feature cache key)."""
    import hashlib
    blob = json.dumps([{c: [list(q) for q in v] for c, v in sorted(b.items())}
                       for b in (route_book(), feature_book())])
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _route_reward(ticks, r, dealt_ticks, ts):
    book = route_book()
    if not book:
        return
    import bisect
    from .roster import move_char
    noise = {None, "None", "Guard", "JustGuard"}
    gap = 2.5                              # RouteFollower's own engagement gap
    land = REWARD["route_land_s"]
    dealt_t = [ts[j] for j in dealt_ticks]
    hist, hist_key, prev = [], None, None
    for i, t in enumerate(ticks):
        me = _rec(t, "me")
        lab = label_of(prev, me) if (prev and me and prev.get("key") == me.get("key")) else None
        prev = me
        if lab in noise:
            continue
        key, now = me.get("key"), ts[i]
        if key != hist_key or (hist and now - hist[-1][0] > gap):
            hist, hist_key = [], key
        hist.append((now, lab))
        del hist[:-32]
        seqs = book.get(move_char(me))
        if not seqs or len(hist) < REWARD["route_min"]:
            continue
        toks = [x for _, x in hist]
        depth = done = 0
        for q in seqs:
            for k in range(min(len(q), len(toks)), REWARD["route_min"] - 1, -1):
                if tuple(toks[-k:]) == q[:k]:
                    depth = max(depth, k)
                    done = done or k == len(q)
                    break
        if depth < REWARD["route_min"]:
            continue
        j = bisect.bisect_left(dealt_t, now - land)
        if j < len(dealt_t) and dealt_t[j] <= now + land:
            r[i] += REWARD["route_step"] + (REWARD["route_done"] if done else 0.0)


def returns(ticks, r=None, horizon_s=HORIZON_S):
    """Discounted sum of reward over the next `horizon_s` seconds, per tick.

    Seconds rather than ticks: recordings run anywhere from 10 to 60 Hz.
    """
    if r is None:
        r = rewards(ticks)
    ts = np.array([t.get("t", i / 15.0) for i, t in enumerate(ticks)], dtype=np.float64)
    g = np.zeros(len(ticks), dtype=np.float32)
    j = 0
    acc = 0.0
    # Two-pointer window sum with linear decay approximated by a plain window;
    # decay inside 1.5 s matters far less than the window existing at all.
    for i in range(len(ticks)):
        if j < i:
            j = i
            acc = 0.0
        while j < len(ticks) and ts[j] - ts[i] <= horizon_s:
            acc += r[j]
            j += 1
        g[i] = acc
        acc -= r[i]
    return g


# Advantage, not raw return. A KO spreads -25 over the 4 s before it, so every
# choice in that stretch - the just guard that nearly saved it included - had
# a return low enough to clip its weight to the floor (~0.1). What a decision
# is credited with should be how it did against the other choices made in the
# same spot, so a local baseline (the mean return over +-BASELINE_S around
# it) is subtracted. ADV_LAMBDA < 1 keeps part of the raw return, so a choice
# inside a long good stretch still counts for more than one in a bad stretch.
BASELINE_S = 2.0
ADV_LAMBDA = 0.75


def advantage(ticks, g, half_s=BASELINE_S, lam=ADV_LAMBDA):
    """g minus lam x its centred moving mean over +-half_s seconds."""
    if len(g) == 0 or lam <= 0:
        return g
    ts = np.array([t.get("t", i / 15.0) for i, t in enumerate(ticks)], dtype=np.float64)
    c = np.concatenate([[0.0], np.cumsum(g, dtype=np.float64)])
    lo = np.searchsorted(ts, ts - half_s, side="left")
    hi = np.searchsorted(ts, ts + half_s, side="right")
    base = (c[hi] - c[lo]) / np.maximum(hi - lo, 1)
    return (g - lam * base).astype(np.float32)


def weights_from_returns(g, lo=0.1, hi=8.0, temp=3.0, shift=0.0):
    """Sample weights from returns: exp(G / temp), clipped.

    Exponentiated advantage (as in AWR) keeps every sample in play - a bad
    decision still teaches what the situation looked like - while letting a
    just guard or a big combo count several times over.
    """
    w = np.exp(np.clip((g - shift) / temp, -10, 10))
    return np.clip(w, lo, hi).astype(np.float32)


# ------------------------------------------------------------ datasets ----

# Self-play (both sides our own bot) is graded harder than anything else: a
# decision has to have earned STRICT_SHIFT of return before it counts as
# average, and the floor is lower, so what the bots did by drifting into each
# other's blind spots is nearly ignored while real punishes and combos stay.
STRICT = {"lo": 0.03, "temp": 2.0, "shift": 1.0}


def build_weighted(ticks, actions=None, chars=None, mode="transitions",
                   drop_idle=True, keep=None, boost=None, boost_mult=1.0,
                   strict=False, return_g=False, return_outcomes=False,
                   drop_labels=()):
    """(X, y, w) for the action head. See `build`.

    `drop_labels` are labels skipped entirely (train.py drops the bot's own
    Escape presses: those come from the hand-written EscapePolicy, so
    learning them would only teach the head to echo that rule).

    `return_g` replaces w with the raw discounted return at each sample - the
    target the critic (train.py critic_head) learns: how well things went in
    the HORIZON_S after this action was chosen in this situation.

    `boost` is a set of tick indices (typically Picker.keep()'s combo
    stretches) whose sample weight is scaled by `boost_mult`. Unlike `keep`
    (which DISCARDS everything outside the set), boost keeps the whole
    recording and just counts the marked ticks for more - so a scarce human
    recording's combo-execution moments can matter more in training without
    losing the rest of its defensive/spacing signal (see train.py's
    --execution-boost).
    """
    if actions is None or chars is None:
        actions, chars = vocabularies(ticks)
    stabilize(ticks)
    idx = {a: i for i, a in enumerate(actions)}
    calibrate_frame(ticks)
    g = returns(ticks)
    w_all = weights_from_returns(advantage(ticks, g), **(STRICT if strict else {}))

    # Wait samples need to know when the fighter's next real decision is.
    want_wait = mode == "transitions" and WAIT in idx
    next_dec = _next_decision_times(ticks) if want_wait else None
    last_wait = last_hit_wait = -1e9
    last_press, move_waited = -1e9, True

    X, y, w, G, I = [], [], [], [], []
    prev_cmd, last_cmd, last_key, key_start = None, None, None, 0
    from .oppmodel import OpponentModel
    om = OpponentModel()
    from .routefeat import RouteTrack
    rt = RouteTrack(route_index())
    from .wallfeat import WallTrack
    wt = WallTrack()
    start = play_start(ticks)
    for i, t in enumerate(ticks[:-1]):
        if i < start:
            continue
        me = _rec(t, "me")
        key = key_of(t, "me")
        om.update(me, _rec(t, "enemy"), t.get("t", i / 15.0))
        rt.observe(me, _rec(t, "enemy"), t.get("t", i / 15.0))
        wt.observe(me, _rec(t, "enemy"), t.get("t", i / 15.0))
        if key != last_key:
            prev_cmd, last_cmd, last_key, key_start = None, None, key, i
            move_waited = True            # a tag: the last press was the partner's
        cur = me.get("cmd") if me else None
        if cur != last_cmd:
            prev_cmd, last_cmd = last_cmd, cur
        if keep is not None and i not in keep:
            continue
        if not me:
            continue
        # The SAME fighter's next record, not whoever the pointer names next:
        # across a tag the pointer moves, and reading it would credit the
        # incoming partner's move to the outgoing fighter.
        nxt = _same(ticks[i + 1], key)
        if not nxt:
            continue

        if mode == "transitions":
            label = label_of(me, nxt)
            if label is None:
                now = t.get("t", i / 15.0)
                cls = act_class(me.get("act"))
                quiet = (want_wait and next_dec[i] - now >= WAIT_GAP_S
                         and me.get("act") not in DEMO_ACTS)
                if quiet and cls in _WAIT_CLASSES and now - last_wait >= WAIT_EVERY_S:
                    label, last_wait = WAIT, now
                elif (quiet and cls in _WAIT_HIT_CLASSES
                        and now - last_hit_wait >= WAIT_HIT_EVERY_S):
                    label, last_hit_wait = WAIT, now
                elif (want_wait and not move_waited and cls in _WAIT_MOVE_CLASSES
                        and now - last_press >= WAIT_MOVE_AFTER_S
                        and next_dec[i] - now >= WAIT_MOVE_GAP_S
                        and me.get("act") not in DEMO_ACTS):
                    label, move_waited = WAIT, True
                else:
                    continue
            else:
                last_press, move_waited = t.get("t", i / 15.0), False
                label = refine_dash(label, ticks, i, key)
                if not scrub.keep(me, nxt, label):
                    continue
                if label in drop_labels:
                    continue
        else:
            label = me.get("cmd")
        if drop_idle and label in IDLE:
            continue
        if label not in idx:
            continue
        history = ticks[max(key_start, i - HISTORY_K):i]
        X.append(featurize(t, actions, chars, prev_cmd, history, om.feats(), rt.feats(me), wt.feats()))
        y.append(idx[label])
        wv = w_all[i] * boost_mult if boost is not None and i in boost else w_all[i]
        w.append(wv)
        G.append(g[i])
        I.append(i)

    if not X:
        return (np.zeros((0, 0), np.float32), np.zeros((0,), np.int64),
                np.zeros((0,), np.float32))
    if return_outcomes:
        return np.stack(X), np.array(y, dtype=np.int64), decision_outcomes(ticks, I)
    return (np.stack(X), np.array(y, dtype=np.int64),
            np.array(G if return_g else w, dtype=np.float32))


def _next_decision_times(ticks):
    """Per tick, the time of the next tick (this one included) at which `me`
    makes a labelled decision; +inf when there is none."""
    n = len(ticks)
    out = np.full(n, np.inf)
    nxt_t = np.inf
    for i in range(n - 2, -1, -1):
        t = ticks[i]
        key = key_of(t, "me")
        me = _rec(t, "me")
        if me and key:
            nx = _same(ticks[i + 1], key)
            if nx and label_of(me, nx) is not None:
                nxt_t = t.get("t", i / 15.0)
        out[i] = nxt_t
    return out


OUTCOME_HIT_S = 1.0      # an attack that has not connected by then missed
OUTCOME_HURT_S = 1.5     # damage taken this soon after a choice is charged to it
OUTCOME_LONG_S = 6.0     # the longer view: setups, punishes, whole combos
OUTCOME_LONG_TAU = 3.0   # discount time constant over that window


def decision_outcomes(ticks, idx):
    """[hit, dealt, taken, dealt_long, taken_long] per decision tick in `idx`:
    whether the opponent lost health within OUTCOME_HIT_S, how much it lost
    within OUTCOME_HURT_S, how much WE lost within OUTCOME_HURT_S, and both
    damages over OUTCOME_LONG_S discounted by exp(-dt / OUTCOME_LONG_TAU) -
    the part of a choice's value that shows up later (a setup, a punish
    taken two seconds on). The short ones are far less noisy per decision
    than a summed shaped return, because each is caused (mostly) by the
    choice itself; the long ones add the planning signal."""
    from .combos import Health
    ts = [t.get("t", j / 15.0) for j, t in enumerate(ticks)]
    n = len(ticks)
    # per tick: total health lost so far by each side (monotone envelope)
    health = Health()
    lost_mine = np.zeros(n)
    lost_theirs = np.zeros(n)
    am = at = 0.0
    for j, t in enumerate(ticks):
        _, _, mine, theirs = resolve(t)
        ms, th = set(mine), set(theirs)
        for k, rec in (t.get("chars") or {}).items():
            hp = rec.get("hp")
            if not isinstance(hp, (int, float)):
                continue
            d = health.drop(k, hp)
            if d > 0:
                if k in th:
                    at += d
                elif k in ms:
                    am += d
        lost_mine[j], lost_theirs[j] = am, at
    out = np.zeros((len(idx), 5), dtype=np.float32)
    dm = np.diff(lost_mine, prepend=lost_mine[:1])
    dt_ = np.diff(lost_theirs, prepend=lost_theirs[:1])
    for r, i in enumerate(idx):
        j_hit = j_hurt = i
        while j_hurt + 1 < n and ts[j_hurt + 1] - ts[i] <= OUTCOME_HURT_S:
            j_hurt += 1
            if ts[j_hurt] - ts[i] <= OUTCOME_HIT_S:
                j_hit = j_hurt
        out[r, 0] = 1.0 if lost_theirs[j_hit] - lost_theirs[i] > 0.004 else 0.0
        out[r, 1] = lost_theirs[j_hurt] - lost_theirs[i]
        out[r, 2] = lost_mine[j_hurt] - lost_mine[i]
        j = i + 1
        while j < n and ts[j] - ts[i] <= OUTCOME_LONG_S:
            w = np.exp(-(ts[j] - ts[i]) / OUTCOME_LONG_TAU)
            out[r, 3] += w * dt_[j]
            out[r, 4] += w * dm[j]
            j += 1
    return out


def build(ticks, actions=None, chars=None, mode="transitions", drop_idle=True,
          keep=None):
    """(X, y, actions, chars) for the action head.

    `keep` limits training to a set of tick indices - see ajbot.combos, which
    selects the stretches where something actually landed. The whole recording
    is still passed in, because a label reads the NEXT tick and the tick before
    a kept one supplies its features.
    """
    if actions is None or chars is None:
        actions, chars = vocabularies(ticks)
    X, y, _ = build_weighted(ticks, actions, chars, mode, drop_idle, keep)
    return X, y, actions, chars


# The movement head used to learn only from moments the stick CHANGED, and for
# people half of those are letting go (a turn, a pause before a press) while
# they hold it fully 96% of the time in neutral: the head learned "let go"
# and the bot stood or walked 21% of neutral vs 4% for people (2026-11-16).
# A held stick is now also sampled once per this many seconds.
HOLD_EVERY_S = 0.25


def build_movement_weighted(ticks, actions=None, chars=None, min_change=20.0,
                            keep=None, boost=None, boost_mult=1.0, strict=False):
    """(X, Y, w) for the movement head. See `build_movement`. `boost` - see
    build_weighted()."""
    if actions is None or chars is None:
        actions, chars = vocabularies(ticks)
    stabilize(ticks)
    calibrate_frame(ticks)
    w_all = weights_from_returns(advantage(ticks, returns(ticks)),
                                 **(STRICT if strict else {}))

    X, Y, w = [], [], []
    last_sample = -1e9
    prev_cmd, last_cmd, last_key, key_start = None, None, None, 0
    from .oppmodel import OpponentModel
    om = OpponentModel()
    from .routefeat import RouteTrack
    rt = RouteTrack(route_index())
    from .wallfeat import WallTrack
    wt = WallTrack()
    start = play_start(ticks)
    for i, t in enumerate(ticks[:-1]):
        if i < start:
            continue
        me = _rec(t, "me")
        key = key_of(t, "me")
        om.update(me, _rec(t, "enemy"), t.get("t", i / 15.0))
        rt.observe(me, _rec(t, "enemy"), t.get("t", i / 15.0))
        wt.observe(me, _rec(t, "enemy"), t.get("t", i / 15.0))
        if key != last_key:
            prev_cmd, last_cmd, last_key, key_start = None, None, key, i
        cur = me.get("cmd") if me else None
        if cur != last_cmd:
            prev_cmd, last_cmd = last_cmd, cur
        if keep is not None and i not in keep:
            continue
        nxt = _same(ticks[i + 1], key)
        if not me or not nxt or key_of(ticks[i + 1], "me") != key:
            continue           # the stick after a tag belongs to the partner
        a0, a1 = me.get("lcam"), nxt.get("lcam")
        s0, s1 = me.get("lscale"), nxt.get("lscale")
        if not all(isinstance(v, (int, float)) for v in (a0, a1, s0, s1)):
            continue
        moved = abs(a1 - a0) > min_change or abs(s1 - s0) > min_change
        now = t.get("t", i / 15.0)
        if not moved and now - last_sample < HOLD_EVERY_S:
            continue           # a held stick: one sample per HOLD_EVERY_S
        last_sample = now
        # Target in the opponent's frame, not the camera's: the same camera
        # angle means 'toward' on Player 1's side and 'away' on Player 2's.
        r = np.deg2rad(wrap180(a1 - tcam_of(nxt, _rec(ticks[i + 1], "enemy"))))
        history = ticks[max(key_start, i - HISTORY_K):i]
        X.append(featurize(t, actions, chars, prev_cmd, history, om.feats(), rt.feats(me), wt.feats()))
        Y.append([np.sin(r), np.cos(r), s1 / 100.0])
        wv = w_all[i] * boost_mult if boost is not None and i in boost else w_all[i]
        w.append(wv)

    if not X:
        return (np.zeros((0, 0), np.float32), np.zeros((0, 3), np.float32),
                np.zeros((0,), np.float32))
    return np.stack(X), np.array(Y, dtype=np.float32), np.array(w, dtype=np.float32)


def build_movement(ticks, actions=None, chars=None, min_change=20.0, keep=None):
    """(X, Y, actions, chars) for the movement head.

    Y is [sin(lcam), cos(lcam), lscale/100] at the NEXT tick.

    The target is `lcam` (stick angle relative to the CAMERA), not `lang`
    (relative to the character's facing), because the physical stick is
    camera-relative - that is what a controller actually pushes. The two differ
    by 78 degrees on average, so training on lang and driving a pad with it
    would aim the bot almost perpendicular to where it meant to go.

    Only ticks where the stick meaningfully changes are kept, for the same
    reason transitions are used for actions: holding a direction is not a new
    decision.
    """
    if actions is None or chars is None:
        actions, chars = vocabularies(ticks)
    X, Y, _ = build_movement_weighted(ticks, actions, chars, min_change, keep)
    return X, Y, actions, chars


def split(X, y, frac=0.2):
    """Chronological holdout. A random split leaks: neighbouring ticks are
    nearly identical, so the same moment lands on both sides."""
    cut = int(len(X) * (1 - frac))
    return X[:cut], y[:cut], X[cut:], y[cut:]

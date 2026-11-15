"""Combo chains, by the game's own rule: the hits have not stopped.

The on-screen combo counter keeps counting while the victim never recovers
between hits - it stays in a Damage* action from one hit to the next. The Lua
API has no getter for that counter, but it exposes both halves of the rule:
each fighter's current action (`act`, Damage* while in hitstun) and a hit
event counter (`ev_hit`, one per BeginDamage). So a combo here is exactly the
game's combo: it opens on a hit and closes when the victim leaves hitstun.

A combo is also split into SEGMENTS, one per character of ours that took part
- a Change Combo hands the chain to the next fighter, and the goal is three
fighters' worth of combo in one chain with a Plus Ultra at the end.

Recordings made before the action wrappers carry no `act`; for those a combo
continues while hits keep landing within BREAK_S of each other, which is the
measured knee in the inter-hit gaps (see ajbot.combos).
"""
import time

from . import clock
from .combos import Health, BREAK_S
from .sampler import Envelope, act_class

GRACE_S = 0.35        # out of hitstun this long = recovered (tick jitter)
STALL_S = 2.5         # in hitstun but no hit this long = knocked down, over


def _base(key):
    from .roster import base_name
    return base_name(key) or key


# The partner's first action on a Change (ActionSidekick.lua): SidekickEnter,
# then SidekickSwitchWait once the switch type is set. Either one starting on a
# benched fighter while the victim is still in hitstun is a Change Combo in
# the tutorial's sense (a Change during an attack); the game's own
# ActionParam.json gives it -30 down value and +10 combo scaling, which is
# exactly why it extends a combo. AttackTagCombo* is a different mechanic (the
# cinematic grab finisher) and is NOT what is detected here.
TAG_IN_ACTS = ("SidekickSwitchWait", "SidekickEnter")
# How long after a Change Combo the newcomer's first attack still counts as
# its opener (the entry attack lands 0.1-1.3 s after the swap).
OPENER_S = 1.5
CHANGE_DOWN = 30.0    # down value a Change Combo removes (Common/ActionParam.json)


class Combo:
    __slots__ = ("victim", "start", "last_hit", "hits", "damage", "segments",
                 "out_since", "pu", "tags", "tag_points", "change_combos", "down")

    def __init__(self, victim, now):
        self.victim = victim
        self.start = self.last_hit = now
        self.hits = 0
        self.damage = 0.0
        self.segments = []            # [[attacker_base, hits, damage], ...]
        self.out_since = None
        self.pu = False               # a Plus Ultra landed in this combo
        self.tags = 0
        # Change Combos INTO this combo (a partner entered while the victim was
        # in hitstun): what the outgoing fighter was doing when it happened.
        # Each is {"char", "act", "act_n", "stall", "hits_at", "t"}; the hits
        # the chain went on to land after it are hits - hits_at.
        self.tag_points = []
        self.change_combos = 0
        # Estimated down value on the victim (game data: each hit adds its
        # DamageParam ForcedBlowValue, a Change Combo takes 30 off, and at 100
        # the victim's ForcedBlow gauge blows them out of the combo). Per-move
        # medians, so it is a soft signal: in the friend matches the chance a
        # hit was the last one went 0.09-0.14 below 100 -> 0.17-0.34 above.
        self.down = 0.0

    @property
    def chars(self):
        return len({s[0] for s in self.segments})

    @property
    def segment_hits(self):
        return self.segments[-1][1] if self.segments else 0

    @property
    def attacker(self):
        return self.segments[-1][0] if self.segments else None

    def stalled(self, now):
        return now - self.last_hit

    def as_dict(self, now=None):
        return {"hits": self.hits, "damage": round(self.damage, 4),
                "chars": self.chars, "segments": [list(s) for s in self.segments],
                "pu": self.pu, "duration": round((self.last_hit - self.start), 2),
                "change_combos": self.change_combos, "down": round(self.down, 1),
                "tag_points": [dict(p, hits_after=self.hits - p["hits_at"])
                               for p in self.tag_points]}


class ChainTracker:
    """Live or offline. Feed one tick at a time with update()."""

    def __init__(self):
        self.env = Envelope()
        self.health = Health()
        self.open = {}                # victim key -> Combo
        self.finished = []            # closed combos, oldest first
        self.by_us = None             # the open combo WE are doing (on their point)
        self.on_us = None             # the open combo being done TO our point
        self.last_act = {}            # key -> (act, act_n) last tick
        self.point = {"mine": None, "theirs": None}   # last tick's point fighters
        self.last_tag_in = None       # (time, side, incoming key, outgoing key)

    def _tag_ins(self, chars, mine, theirs, me, enemy, now):
        """Change Combos this tick: a benched partner starting SidekickEnter /
        SidekickSwitchWait while the other side's point is in a combo."""
        for k, r in chars.items():
            if r.get("age", 1e9) > 60 or "act" not in r:
                continue
            act = r.get("act")
            prev = self.last_act.get(k)
            self.last_act[k] = (act, r.get("act_n"))
            if act not in TAG_IN_ACTS or (prev and prev[0] in TAG_IN_ACTS):
                continue
            if k in mine:
                side, point, victim = "mine", me, enemy
            elif k in theirs:
                side, point, victim = "theirs", enemy, me
            else:
                continue
            # The outgoing fighter is whoever held the point before this tick;
            # the pointer may or may not have moved to the newcomer yet.
            out = self.point.get(side)
            if out in (None, k):
                out = point if point != k else None
            c = self.open.get(victim) if victim else None
            self.last_tag_in = (now, side, k, out)
            if c is None:
                continue
            victim_rec = chars.get(victim) or {}
            if "act" in victim_rec and act_class(victim_rec.get("act")) != "damage":
                continue                  # a plain Change, the combo had ended
            oa = self.last_act.get(out) if out else None
            c.change_combos += 1
            c.down = max(0.0, c.down - CHANGE_DOWN)
            # Who came in, whether the victim was airborne, and (filled in by
            # update() once it starts) the newcomer's first attack: what the
            # tag book (ajbot.tagbook) learns partner choice and openers from.
            gnd = victim_rec.get("ground")
            c.tag_points.append({"char": _base(out) if out else "?",
                                 "act": oa[0] if oa else None,
                                 "act_n": oa[1] if oa else None,
                                 "stall": round(now - c.last_hit, 3),
                                 "hits_at": c.hits, "t": now,
                                 "in": _base(k), "in_key": k,
                                 "air": None if gnd is None else not gnd,
                                 "opener": None, "dmg_at": round(c.damage, 4)})

    def update(self, chars, mine, theirs, me=None, enemy=None, now=None):
        now = clock.now() if now is None else now
        mine, theirs = set(mine or ()), set(theirs or ())
        if me:
            mine.add(me)
        if enemy:
            theirs.add(enemy)
        self._tag_ins(chars, mine, theirs, me, enemy, now)
        # The newcomer's first attack after a Change Combo (its opener).
        for c in self.open.values():
            for p in c.tag_points:
                if p.get("opener") is None and p.get("in_key") and now - p["t"] <= OPENER_S:
                    r = chars.get(p["in_key"]) or {}
                    a, cmd = r.get("act") or "", r.get("cmd")
                    # The command (the bot's action vocabulary), read once
                    # the newcomer's first attack has started.
                    if a.startswith("Attack") and cmd not in (None, "None"):
                        p["opener"] = cmd
        closed = []
        for k, r in chars.items():
            if r.get("age", 1e9) > 60:
                continue
            ours = k in mine
            if not ours and k not in theirs:
                continue
            hit = self.env.delta(k, "ev_hit", r.get("ev_hit")) > 0
            hp = r.get("hp")
            dmg = self.health.drop(k, hp) if isinstance(hp, (int, float)) else 0.0
            if "ev_hit" not in r and dmg > 0:
                hit = True            # older recordings: a health drop is a hit
            c = self.open.get(k)
            if "act" in r:
                in_stun = act_class(r.get("act")) == "damage"
            else:
                in_stun = c is not None and now - c.last_hit <= BREAK_S

            if hit:
                if c is None:
                    c = self.open[k] = Combo(k, now)
                c.hits += 1
                c.damage += dmg
                c.last_hit = now
                c.out_since = None
                att_key = enemy if ours else me
                attacker = _base(att_key) if att_key else "?"
                if att_key:
                    from . import movedata
                    c.down += movedata.get().fb(att_key, (chars.get(att_key) or {}).get("act"))
                if not c.segments or c.segments[-1][0] != attacker:
                    if c.segments:
                        c.tags += 1
                    c.segments.append([attacker, 0, 0.0])
                c.segments[-1][1] += 1
                c.segments[-1][2] = round(c.segments[-1][2] + dmg, 4)
            elif c is not None:
                c.damage += dmg
                if dmg > 0 and in_stun:
                    # Damage the hit counter misses (the last hit of Mirio's
                    # q4 lands ~3 s in, ev_hit unchanged) still keeps the
                    # combo going, or STALL_S ends it before the follow-up.
                    c.last_hit = now
                if not in_stun:
                    c.out_since = c.out_since or now
                    if now - c.out_since >= GRACE_S:
                        closed.append(k)
                elif now - c.last_hit >= STALL_S:
                    closed.append(k)
        for k in closed:
            c = self.open.pop(k)
            self.finished.append(c)
        # Current combos on the two point fighters.
        self.by_us = self.open.get(enemy) if enemy else None
        if self.by_us is None:
            self.by_us = next((c for k, c in self.open.items() if k in theirs), None)
        self.on_us = self.open.get(me) if me else None
        if me:
            self.point["mine"] = me
        if enemy:
            self.point["theirs"] = enemy
        return closed

    def note_pu(self):
        if self.by_us is not None:
            self.by_us.pu = True

    def drain(self):
        out, self.finished = self.finished, []
        return out


def summarize(combos):
    """Per-match summary of OUR combos (2+ hits), for stats/matches.jsonl."""
    real = [c for c in combos if c.hits >= 2]
    if not real:
        return {"combos": 0}
    return {
        "combos": len(real),
        "mean_hits": round(sum(c.hits for c in real) / len(real), 2),
        "max_hits": max(c.hits for c in real),
        "max_damage": round(max(c.damage for c in real), 3),
        "chains_2": sum(1 for c in real if c.chars >= 2),
        "chains_3": sum(1 for c in real if c.chars >= 3),
        "pu_finishers": sum(1 for c in real if c.pu and c.chars >= 2),
        # Change Combos (a partner tagged in mid-combo), and how many of them
        # the chain actually went on from - the measure of tag timing.
        "change_combos": sum(c.change_combos for c in real),
        "change_combos_kept": sum(1 for c in real for p in c.tag_points
                                  if c.hits > p["hits_at"]),
    }

"""Skill matching: the bot plays down (or up) to the person it is fighting.

One number, the level (0.1 .. 1.0, 1 = full strength), sets every handicap:

  reaction   the bot decides from what the game looked like LAG_MAX_S *
             (1 - level) ago, so it guards, punishes and escapes late
  pressure   it skips that share of its attack decisions (walks instead)
  defence    each opponent attack gets one roll: guard / counter at all?
  escape     each time it is comboed, one roll: emergency dodge allowed?
  resources  Rising / Plus Ultra / tagging chances pass at a reduced rate
  routes     the combo book and learned openers only near full strength

With `adapt` the level follows the fight: the damage both sides deal is
compared every time ADAPT_DMG of health has changed hands, and each match
result nudges it too. The target is a little under even (TARGET): close
fights the person wins slightly more often than not. The level is kept in
stats/skill.json, so the next session starts where the last one ended.

Matches played below full strength are flagged ("skill" on every recorded
tick): the bot's side of them is deliberately worse and is not trained on;
the person's side is (train.py).
"""
import json
import random
import time
from collections import deque
from pathlib import Path

PATH = Path("stats") / "skill.json"
LO, HI = 0.1, 1.0
START = 0.5
LAG_MAX_S = 0.30        # reaction delay at level 0
SKIP_MAX = 0.45         # share of attack decisions skipped at level 0
TARGET = 0.45           # bot's share of the damage dealt that it aims for
ADAPT_DMG = 0.6         # health bars exchanged between adjustments
ADAPT_GAIN = 0.35       # level change per unit of damage-share error
MATCH_STEP = 0.05       # nudge per match result
ROUTES_MIN = 0.75       # book / routes / openers from this level up


class SkillMatch:
    def __init__(self, level=None, adapt=True, path=PATH, log=print):
        self.path = Path(path)
        self.adapt = adapt
        self.log = log
        saved = self._load()
        self.level = _clip(level if level is not None else saved.get("level", START))
        self.snaps = deque()
        self.rolls = {}
        self.hp = None           # (mine, theirs) team health last tick
        self.dealt = [0.0, 0.0]  # since the last adjustment: (bot, opponent)
        self.match_dealt = [0.0, 0.0]
        self.en_move = 0         # counts the opponent's act changes (one roll each)
        self.en_last = None
        self.log(f"skill match: level {self.level:.2f}"
                 + (" (adapts to you)" if adapt else " (fixed)"))

    # ------------------------------------------------------------- handicaps
    @property
    def lag(self):
        return LAG_MAX_S * (1.0 - self.level)

    def view(self, snap, now=None):
        """The snapshot the bot gets to decide from: the game as it was
        `lag` seconds ago (the newest one at full strength)."""
        now = time.perf_counter() if now is None else now
        self.snaps.append((now, snap))
        cut = now - self.lag
        while len(self.snaps) > 1 and self.snaps[1][0] <= cut:
            self.snaps.popleft()
        return self.snaps[0][1] if self.snaps[0][0] <= cut else snap

    def roll(self, kind, key, p):
        """One yes/no per (kind, key) - the same answer every tick for one
        opponent attack, one combo taken, one window."""
        k = (kind, key)
        if k not in self.rolls:
            if len(self.rolls) > 400:
                self.rolls.clear()
            self.rolls[k] = random.random() < p
        return self.rolls[k]

    def attack_ok(self):
        return random.random() >= SKIP_MAX * (1.0 - self.level)

    def defend_ok(self, en):
        """Answer this opponent attack at all?"""
        en = en or {}
        return self.roll("def", (en.get("key"), self.en_move), 0.35 + 0.65 * self.level)

    def escape_ok(self, me):
        me = me or {}
        return self.roll("esc", (me.get("key"), int(time.time() / 4)), self.level)

    def extra_ok(self, kind):
        """Resources and tags: one roll per 3 s window each."""
        return self.roll(kind, int(time.time() / 3), 0.3 + 0.7 * self.level)

    @property
    def routes_ok(self):
        return self.level >= ROUTES_MIN

    # -------------------------------------------------------------- adapting
    def observe(self, teams):
        """Damage dealt each way this tick (team health going down), and
        which opponent move this is."""
        en = teams.get("enemy") or {}
        cur = (en.get("key"), en.get("act"), en.get("act_n"))
        last = self.en_last
        if (last is None or cur[:2] != last[:2]
                or (isinstance(cur[2], (int, float)) and isinstance(last[2], (int, float))
                    and cur[2] < last[2])):
            self.en_move += 1
        self.en_last = cur
        hp = tuple(sum(float(r.get("hp") or 0.0) for r in (teams.get(s) or [])
                       if isinstance(r.get("hp"), (int, float)))
                   for s in ("mine", "theirs"))
        if self.hp is not None:
            bot = max(0.0, self.hp[1] - hp[1])      # theirs went down: we hit
            opp = max(0.0, self.hp[0] - hp[0])
            if bot < 0.5 and opp < 0.5:            # not a roster change
                for i, v in enumerate((bot, opp)):
                    self.dealt[i] += v
                    self.match_dealt[i] += v
        self.hp = hp
        if self.adapt and sum(self.dealt) >= ADAPT_DMG:
            share = self.dealt[0] / sum(self.dealt)
            self._set(self.level - ADAPT_GAIN * (share - TARGET),
                      f"bot dealt {share:.0%} of the damage")
            self.dealt = [0.0, 0.0]

    def match_over(self, result):
        if self.adapt and result in ("win", "loss"):
            self._set(self.level + (-MATCH_STEP if result == "win" else MATCH_STEP),
                      f"bot {'won' if result == 'win' else 'lost'} the match")
        d = sum(self.match_dealt)
        out = {"level": round(self.level, 3), "adapt": self.adapt,
               "bot_damage_share": round(self.match_dealt[0] / d, 3) if d else None}
        self.match_dealt = [0.0, 0.0]
        self.hp = None
        self.snaps.clear()
        return out

    def _set(self, v, why):
        old, self.level = self.level, _clip(v)
        if abs(self.level - old) >= 0.005:
            self.log(f"  skill level {old:.2f} -> {self.level:.2f} ({why})")
            self._save()

    # ------------------------------------------------------------- storage
    def _load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"level": round(self.level, 3),
                                             "t": round(time.time())}), encoding="utf-8")
        except OSError:
            pass


def _clip(v):
    return max(LO, min(HI, float(v)))

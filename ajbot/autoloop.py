"""Endless bot-vs-bot: random teams, rematch, repeat - no one at the controls.

    Start at the 2-player vs character select screen with play2.py --auto.

    pick      each bot picks three random characters ITSELF (not the game's
              Random cell), then sets each one's row to Manual
    play      the match itself (play2.py's normal loop)
    return    when the match tracker sees the end, wait for the result screen,
              then Player 1 (who owns the menus) presses A -> Continue screen,
              DOWN x3 -> "Character Select", A
    ...and back to pick.

Why not the game's Random cell: the control-mode screen remembers Normal /
Manual per character per player, and each row only TOGGLES. Setting a row
correctly means knowing which character is in it, and a Random pick hides
that. Picking the random team ourselves means the bot always knows - and it
lets the picks favour characters that have the least training data so far.

Where the cursor sits when character select comes back is not known for
sure: it may stay on the last character picked ('stay') or reset to the
first fighter ('reset'). After every match the team that actually fought is
compared with the team that was picked; on a mismatch the loop switches
assumption and says so.

A watchdog notices when a match does not start after the menu sequence and
says so, so a desynced loop does not sit there silently all night.
"""
import json
import random
import time
from pathlib import Path

from . import menus
from .pad import CONFIRM, DPAD_DOWN
from .roster import DISPLAY_ORDER

PHASES = ("idle", "post_match", "waiting_match")


def data_minutes(path=Path("stats") / "matches.jsonl"):
    """Minutes on point per character across every recorded match."""
    out = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        for name, c in (m.get("per_char") or {}).items():
            out[name] = out.get(name, 0.0) + float(c.get("point_s") or 0.0) / 60.0
    return out


STATS_PATH = Path("stats") / "matches.jsonl"


def _records(path=STATS_PATH):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("result") in ("win", "loss"):
            out.append(m)
    return out


def _lower_bound(wins, n, z=1.0):
    """Wilson lower bound on the win rate: high only when the record is both
    good and long. 3-0 scores about 0.56, 30-10 about 0.68."""
    if n <= 0:
        return 0.0
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return (c - r) / d


def _per_char(recs):
    """name -> [games, wins, net damage sum, {opponent: [games, wins]}]."""
    agg = {}
    for m in recs:
        won = m["result"] == "win"
        opps = [o for o in (m.get("opponents_roster") or m.get("opponents") or [])]
        for name, c in (m.get("per_char") or {}).items():
            a = agg.setdefault(name, [0, 0, 0.0, {}])
            a[0] += 1
            a[1] += won
            a[2] += float(c.get("dealt") or 0.0) - float(c.get("taken") or 0.0)
            for o in set(opps):
                v = a[3].setdefault(o, [0, 0])
                v[0] += 1
                v[1] += won
    return agg


def best_team(path=STATS_PATH, size=3, min_games=5):
    """The `size` characters the bot is most CONFIDENT with: ranked by the
    lower bound of their win rate, so a long good record beats a short lucky
    one; net damage per match breaks near-ties."""
    from ajbot.roster import INDEX
    agg = _per_char(_records(path))

    def score(a):
        return _lower_bound(a[1], a[0]) + 0.02 * a[2] / a[0]

    ranked = sorted((n for n, a in agg.items() if a[0] >= min_games and n in INDEX),
                    key=lambda n: score(agg[n]), reverse=True)
    return ranked[:size]


def counter_team(opponents, path=STATS_PATH, size=3, min_games=5, prior=6.0):
    """The `size` characters with the best record AGAINST `opponents` (the
    team last fought). Each matchup's win rate is shrunk toward that
    character's overall confidence by `prior` games, so a matchup needs real
    data before it can outrank a generally strong pick. Returns
    (team, {name: (score, matchup games)})."""
    from ajbot.roster import INDEX
    agg = _per_char(_records(path))
    opps = [o for o in opponents or [] if o]
    why = {}
    for name, a in agg.items():
        if a[0] < min_games or name not in INDEX:
            continue
        base = _lower_bound(a[1], a[0])
        if not opps:
            why[name] = (base, 0)
            continue
        tot, games = 0.0, 0
        for o in opps:
            g, w = a[3].get(o, [0, 0])
            tot += (w + prior * base) / (g + prior)
            games += g
        why[name] = (tot / len(opps) + 0.02 * a[2] / a[0], games)
    ranked = sorted(why, key=lambda n: why[n][0], reverse=True)
    return ranked[:size], why


def last_online_opponents(path=STATS_PATH):
    """The opponent team of the most recent online match, or []."""
    for m in reversed(_records(path)):
        if m.get("online"):
            return list(m.get("opponents_roster") or m.get("opponents") or [])
    return []


class AutoLoop:
    def __init__(self, pads, p1_index=0, post_wait=18.0, select_wait=4.0,
                 continue_wait=4.0, return_downs=3, start="DekuFinal",
                 cursor_mode="stay", match_timeout=150.0, favour_new=True,
                 seed=None, mirror_chance=0.0, fixed=None):
        self.pads = [p for p in pads if p is not None]
        # pad index -> a team picked every match instead of a random one
        # (play2.py --auto --a X,Y,Z --b ...): the same matchup, endlessly.
        self.fixed = {id(pads[i]): list(t) for i, t in (fixed or {}).items()
                      if t and pads[i] is not None}
        self.p1 = pads[p1_index]
        self.post_wait = post_wait            # victory pose -> results screen
        self.continue_wait = continue_wait    # results A -> Continue screen
        self.select_wait = select_wait        # Continue A -> character select
        self.return_downs = return_downs
        self.start_char = start
        self.cursor_mode = cursor_mode        # 'stay' or 'reset' on return
        self.match_timeout = match_timeout
        self.favour_new = favour_new
        self.mirror_chance = mirror_chance    # odds a side may reuse the other's picks
        self.rng = random.Random(seed)
        self.navs = {id(p): menus.CharacterSelect(p, start=start, side=self.side(p))
                     for p in self.pads}
        self.teams = {}                       # side -> the team picked
        self.phase = "idle"
        self.t = time.monotonic()
        self.loops = 0
        self.mismatches = 0
        self.warned = False
        self.on_picked = None                 # called once both teams are picked

    def side(self, pad):
        return "1P" if pad is self.p1 else "2P"

    # ------------------------------------------------------------ choosing
    def choose_teams(self):
        """Three different characters per side, none shared between sides
        unless mirror_chance lets a side reuse the other's picks. Characters with little data are
        more likely to come up, so coverage evens out over a long run."""
        minutes = data_minutes() if self.favour_new else {}
        # Characters the bot cannot see are never picked. The low-data bias
        # below kept choosing exactly those, since their data never grows.
        from .roster import NO_NAMESPACE
        pool = [c for c in DISPLAY_ORDER if c not in NO_NAMESPACE]
        weights = [1.0 / (1.0 + minutes.get(c, 0.0)) for c in pool]
        sides = [self.side(p) for p in self.pads]
        teams = {self.side(p): list(self.fixed[id(p)]) for p in self.pads
                 if id(p) in self.fixed}
        for side in sides:
            if side in teams:
                continue
            # A side normally avoids the other side's characters; with
            # mirror_chance it is allowed to share them (bot vs bot mirrors).
            mirror = self.rng.random() < self.mirror_chance
            taken = set() if mirror else {c for t in teams.values() for c in t}
            team = []
            while len(team) < 3:
                c = self.rng.choices(pool, weights=weights)[0]
                if c not in team and c not in taken:
                    team.append(c)
            teams[side] = team
        if self.mirror_chance and len(sides) == 2 and self.rng.random() < self.mirror_chance:
            teams[sides[1]] = list(teams[sides[0]])   # full mirror: same team
        return teams

    # ------------------------------------------------------------ sequences
    def _tap(self, pad, button, gap=menus.MENU_GAP, hold=3):
        pad.tap(button, hold_frames=hold, gap_frames=gap)

    def pick_teams(self, first):
        self.teams = self.choose_teams()

        def one(pad):
            side = self.side(pad)
            nav = self.navs[id(pad)]
            if first or self.cursor_mode == "reset":
                nav.cursor = menus.G.position_of(self.start_char)
            team = self.teams[side]
            print(f"[auto] {side} picks {' / '.join(team)}", flush=True)
            nav.select_team(team)
            pad.idle(menus.SETTLE)
            menus.set_all_manual(pad, count=3, side=side, chars=team)
            pad.idle(30)

        # Both players pick their team and set control modes at the same time.
        menus.run_parallel([(lambda p=pad: one(p)) for pad in self.pads])
        self.phase = "waiting_match"
        self.t = time.monotonic()
        self.warned = False
        if self.on_picked:
            self.on_picked()

    def rematch(self):
        """Player 1: results -> Continue -> Rematch (the top option, where
        the cursor starts). Same teams and control modes, no picking."""
        print("[auto] match over - rematch (same teams)", flush=True)
        pad = self.p1
        self._tap(pad, CONFIRM, hold=6)
        time.sleep(self.continue_wait)
        self._tap(pad, CONFIRM, hold=6)
        self.loops += 1
        self.phase = "waiting_match"
        self.t = time.monotonic()
        self.warned = False
        if self.on_picked:
            self.on_picked()              # re-pin: the next battle is a new match

    def return_to_select(self):
        """Player 1: results -> Continue -> Character Select."""
        if self.fixed and len(self.fixed) == len(self.pads) and self.teams:
            return self.rematch()     # both teams fixed: nothing to re-pick
        print("[auto] match over - back to character select", flush=True)
        pad = self.p1
        self._tap(pad, CONFIRM, hold=6)
        time.sleep(self.continue_wait)
        # Spaced out: a Down sent while the Continue screen is still sliding
        # in is lost, and A then lands on Rematch or Stage Select instead.
        for _ in range(self.return_downs):
            self._tap(pad, DPAD_DOWN, gap=menus.MENU_GAP * 2)
        self._tap(pad, CONFIRM, hold=6)
        time.sleep(self.select_wait)
        self.loops += 1
        self.pick_teams(first=False)

    # ------------------------------------------------------------ driving
    def start(self):
        """At character select, before the first match."""
        self.pick_teams(first=True)

    def match_started(self):
        """A new match is live. Ignored while a return-to-menu is still due:
        only a match that follows our own menu sequence counts."""
        if self.phase == "post_match":
            return False
        self.phase = "idle"
        return True

    def pins(self, pad_is_p1):
        """(our team, their team) as picked, for pinning each bot's sides."""
        me = "1P" if pad_is_p1 else "2P"
        them = "2P" if me == "1P" else "1P"
        return list(self.teams.get(me) or []), list(self.teams.get(them) or [])

    def match_ended(self):
        self.phase = "post_match"
        self.t = time.monotonic()
        self.ended_at = time.time()       # wall clock, comparable with MatchTracker.t0

    def verify(self, pad_is_p1, fought):
        """Compare the team that fought (roster names) with the one picked.

        A mismatch means the cursor was not where the loop assumed when
        character select came back, so the assumption flips.
        """
        side = "1P" if pad_is_p1 else "2P"
        want = set(self.teams.get(side) or [])
        got = set(fought or [])
        # The roster also catches the previous match's fighters for the first
        # ticks of a new one (their records linger through the transition), so
        # extra names are not evidence of anything. A wrong cursor shows up as
        # a picked fighter who never appeared.
        if not want or not got or want <= got:
            return True
        self.mismatches += 1
        old = self.cursor_mode
        self.cursor_mode = "reset" if old == "stay" else "stay"
        print(f"[auto] !! {side} picked {sorted(want)} but fought with "
              f"{sorted(got)} - the cursor was not where it was assumed "
              f"('{old}'). Switching to '{self.cursor_mode}'. Control-mode "
              f"settings for these characters may be off for a match.",
              flush=True)
        return False

    def tick(self):
        """Call every loop iteration; runs a menu sequence when one is due."""
        now = time.monotonic()
        if self.phase == "post_match" and now - self.t >= self.post_wait:
            self.return_to_select()
        elif self.phase == "waiting_match" and now - self.t >= self.match_timeout \
                and not self.warned:
            self.warned = True
            print(f"[auto] !! no match started {self.match_timeout:.0f}s after "
                  f"the menu sequence - the loop is probably out of step with "
                  f"the screen. Get back to character select and it picks up "
                  f"again from the next match end, or restart play2.py --auto.",
                  flush=True)

    def status(self):
        return (f"auto loop: {self.loops} rematch(es), cursor '{self.cursor_mode}', "
                f"{self.mismatches} pick mismatch(es), phase {self.phase}")

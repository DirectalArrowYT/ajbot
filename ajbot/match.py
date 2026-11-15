"""Match boundaries and per-match statistics.

The game says when a match starts and ends through the action wrappers:

  AppearanceDemo   the intro pose at the start of a match
  WinDemo          the winners' victory pose at the end

so the recorders no longer need restarting between matches. A tracker watches
every tick, and when a match ends it produces one summary - who won, both
line-ups, damage per character, KOs, time on point, just guards, counters,
tags - appended to stats/matches.jsonl. That file is what the GUI's statistics
tab (and `python stats.py`) read.

Fallbacks, for when the victory pose is missed:
  * a whole side reaching 0 health is a KO win for the other side
  * the battle Lua going away (back to menus) for ABANDON_S ends the match as
    'abandoned' - it still counts for time played but not for win rate.

Training mode never ends a match (health refills), so a training session is
one long match whose result is 'none'.
"""
import json
import time
from pathlib import Path

from .combos import Health
from .sampler import Envelope, act_class

STATS_PATH = Path("stats") / "matches.jsonl"
ABANDON_S = 20.0
RECENT_ACT = 120      # sampler runs (~6 s): an action older than this is stale
KO_CONFIRM_S = 6.0    # a team KO must hold this long (Rising fakes one briefly)
BENCH_HOLD_MAX_S = 45.0   # ...or this long while a benched teammate looks healthy
# A restart that shows neither the intro pose nor a menu round-trip (an
# in-game Retry, or a character-select flow that never lets the battle Lua
# go stale) would otherwise leave the tracker stuck in "over" forever. Full
# health with nobody posing is a legitimate signal too - it was excluded
# outright because the result screen produces the same reading ~5 s after
# the victory pose - so it only counts once "over" has held far longer than
# that transient ever does.
STUCK_OVER_S = 15.0
EVENT_FIELDS = {
    "just_guards": "ev_jg", "just_steps": "ev_js", "counters": "ev_ctr",
    "guarded_hits": "ev_gok", "change_combos": "ev_tagc", "changes": "ev_swap",
    "escapes": "ev_esc", "dodges": "ev_dodge", "guard_breaks": "ev_gbrk",
    "plus_ultras": "ev_pu", "unblockables": "ev_ub", "hits_taken": "ev_hit",
}


def _short(cid):
    """'Pl02Bakugo' -> 'Bakugo'; anything unnamed stays as it is."""
    if isinstance(cid, str) and cid.startswith("Pl"):
        return cid.split("#", 1)[0][2:].lstrip("0123456789") or cid
    return cid


def _merge_forms(chars):
    """Per-character stats keyed by roster name, alternate forms folded into
    their base character (DabiFullBodyBurn's time counts as DabiFinal's)."""
    from .roster import base_name
    out = {}
    for k, c in chars.items():
        name = base_name(k) or _short(k)
        m = out.setdefault(name, {"dealt": 0.0, "taken": 0.0, "point_s": 0.0, "ko": False})
        m["dealt"] = round(m["dealt"] + c.get("dealt", 0.0), 3)
        m["taken"] = round(m["taken"] + c.get("taken", 0.0), 3)
        m["point_s"] = round(m["point_s"] + c.get("point_s", 0.0), 3)
        m["ko"] = m["ko"] or bool(c.get("ko"))
    return out


def _roster_names(keys):
    from .unknowns import roster_name
    out = []
    for k in keys:
        n = roster_name(k)
        if n and n not in out:
            out.append(n)
    return out


# Share of a match's ticks a fighter must be listed on a side to count as in
# it (see MatchTracker._roster); the previous match's fighters linger ~3%.
ROSTER_MIN_SHARE = 0.25
# A '#N' copy also has to act on this share of the match: a real mirror copy
# fights; the frozen twins act on none or a few ticks.
ROSTER_COPY_ACT_SHARE = 0.10


class MatchTracker:
    """Feed it every tick; it reports 'started' / 'ended' and builds summaries.

    `mode` says whose play this is: 'bot' (play.py vs CPU), 'selfplay'
    (play2.py, one tracker per side) or 'human' (record.py).
    """

    def __init__(self, mode="bot", side=None, model=None, path=STATS_PATH,
                 write=True):
        self.mode = mode
        self.side = side
        self.model = model
        self.path = Path(path)
        self.write_enabled = write
        self.state = "waiting"          # waiting -> live -> over -> waiting
        self.summary = None
        # Fighters per team when both line-ups are known (None = unknown):
        # a team KO then needs that many down (see update()).
        self.team_size = None
        self._menu_gap = False          # the battle Lua went away since the last match
        self._no_live_since = None
        self._over_since = None
        self._reset()

    # ---------------------------------------------------------------- state
    def _reset(self):
        self.t0 = None
        self.t_last = None
        self.health = Health()
        self.env = Envelope()
        self.mine = set()
        self.theirs = set()
        self.listed_n = {"mine": {}, "theirs": {}}   # side -> key -> ticks listed
        self.live_n = 0                 # live ticks this match
        self.acted_n = {}               # key -> ticks with a real action
        # Raw tallies per key, summed over the real roster only at the end
        # (_roster): totals over every key ever listed counted the previous
        # match's fighters and the frozen '#1' twins, whose health also drops.
        self.drops = {}                 # key -> health lost
        self.dealt_to = {}              # (our point key, their key) -> health
        self.ko_keys = set()
        self.ev_by = {}                 # key -> {event: count}
        self.hp = {}
        self.chars = {}                 # key -> per-character stats
        self.events = {k: 0 for k in EVENT_FIELDS}
        self.enemy_events = {k: 0 for k in EVENT_FIELDS}
        self.kos = self.kos_enemy = 0
        self.down = set()
        self.alive = set()
        self.point = None
        self._ko_since = {}
        self.winner = None
        self.reason = None
        self.recording = None

    def _char(self, key):
        return self.chars.setdefault(key, {"dealt": 0.0, "taken": 0.0,
                                           "point_s": 0.0, "ko": False})

    # ---------------------------------------------------------------- feed
    def update(self, chars, mine, theirs, me=None, now=None):
        """One tick. `chars` is key -> record (dedupe'd), `mine`/`theirs` are
        key lists, `me` is the key on point for our side.

        Returns 'started', 'ended' or None.
        """
        now = time.time() if now is None else now
        live = {k: r for k, r in chars.items() if r.get("age", 1e9) <= 60}
        # Menus and loading do not always make the hook fail - it can keep
        # returning the last samples, all going stale. Every fighter stale for
        # a couple of seconds is also a trip out of the battle.
        if not live:
            self._no_live_since = self._no_live_since or now
            if now - self._no_live_since >= 2.0 and self.state != "live":
                self._menu_gap = True
        else:
            self._no_live_since = None
        acts = {k: r.get("act") for k, r in live.items()}
        # An action only counts as HAPPENING NOW if it began recently: `act`
        # keeps its last value until the fighter does something else, so a
        # victory pose from the previous match lingers into the next one.
        recent = {k: a for k, a in acts.items()
                  if (live[k].get("act_n") or 0) <= RECENT_ACT}

        if self.state in ("waiting", "over"):
            # A match used to start only on the intro pose or on someone
            # fighting. Miss the intro and nobody fights - the bots press
            # nothing until a match is live - so both sides stood still until
            # a human touched the keyboard. Now a live battle with both teams
            # present IS a match, unless someone is mid victory pose.
            intro = any(a == "AppearanceDemo" for a in recent.values())
            posing = any(a == "WinDemo" for a in recent.values())
            both = [r for k, r in live.items() if k in set(mine) | set(theirs)]
            full = len(both) >= 2 and all((r.get("hp") or 0) >= 0.95 for r in both)
            if not mine or not theirs or not live:
                return None          # no live battle (menus, loading)
            if self.state == "over":
                # Next match: its intro, or the battle coming back after a trip
                # through the menus. NOT merely everyone back at full health:
                # the result screen restores health while the battle is still
                # loaded, and that read as a new match 5 s after the victory
                # pose - the auto loop then never ran its return-to-menu keys.
                # But neither signal fires for every restart path (an in-game
                # Retry, say), so once "over" has held far past that 5 s
                # transient, full health with nobody posing is trusted too -
                # better than being stuck waiting forever.
                stuck = (self._over_since is not None
                        and now - self._over_since >= STUCK_OVER_S)
                if not (intro or (self._menu_gap and not posing)
                       or (stuck and full and not posing)):
                    return None
            elif posing and not intro:
                return None          # joined on a result screen: wait
            self._menu_gap = False
            self._over_since = None
            self._reset()
            self.state = "live"
            self.t0 = now
            # Report the start. The recorders open the match's file, reset the
            # roster and count the match on this event - it used to fall
            # through and return None, so none of that ever happened and every
            # tick of every match was counted but never written.
            return "started"

        if self.state != "live":
            return None

        dt = 0.0 if self.t_last is None else max(0.0, min(1.0, now - self.t_last))
        self.t_last = now
        self.mine.update(mine)
        self.theirs.update(theirs)
        self.live_n += 1
        for side, keys in (("mine", mine), ("theirs", theirs)):
            n = self.listed_n[side]
            for k in set(keys):
                n[k] = n.get(k, 0) + 1
        for k, r in chars.items():
            if r.get("act") not in (None, "None"):
                self.acted_n[k] = self.acted_n.get(k, 0) + 1
        if me:
            self.point = me
            self._char(me)["point_s"] += dt

        # damage, KOs
        for k, r in chars.items():
            hp = r.get("hp")
            if not isinstance(hp, (int, float)):
                continue
            ours = k in self.mine
            if not ours and k not in self.theirs:
                continue
            self.hp[k] = hp
            d = self.health.drop(k, hp)
            if d > 0:
                self.drops[k] = self.drops.get(k, 0.0) + d
                if not ours and self.point:
                    pk = (self.point, k)
                    self.dealt_to[pk] = self.dealt_to.get(pk, 0.0) + d
            gone = bool(r.get("dead")) or hp <= 0.001
            if gone and k in self.alive and k not in self.down:
                self.down.add(k)
                self.ko_keys.add(k)
            elif hp > 0.2:
                self.alive.add(k)
                self.down.discard(k)
            elif hp > 0.001:
                self.down.discard(k)      # back from a momentary zero

            # events
            for name, field in EVENT_FIELDS.items():
                if field in r:
                    dv = self.env.delta(k, field, r.get(field))
                    if dv:
                        ev = self.ev_by.setdefault(k, {})
                        ev[name] = ev.get(name, 0) + int(dv)

        # the end
        winners = [k for k, a in recent.items() if a == "WinDemo"]
        if winners and now - (self.t0 if self.t0 is not None else now) >= 10.0:
            # Only fighters really in this match: every fighter has a frozen
            # '#1' twin listed on the OTHER side, and counting the winner's
            # twin made the loser log a win too (221 of 811 self-play
            # matches, 2026-11-10).
            rm, rt = self._roster(self.mine), self._roster(self.theirs)
            ours = sum(1 for k in winners if k in rm)
            theirs = sum(1 for k in winners if k in rt)
            if ours or theirs:
                if ours == theirs:
                    # Still a tie: whoever lost fewer fighters won.
                    ours = -sum(1 for k in rm if k in self.down)
                    theirs = -sum(1 for k in rt if k in self.down)
                return self._end("win" if ours > theirs else "loss", "victory pose", now)
        # Team KO, only as a fallback to the victory pose and only once it has
        # HELD: entering Rising (its activation / cutscene) reads 0 health on
        # a whole team for a moment, and taking that at face value ended a
        # live match as a KO. A real KO keeps everyone down and nobody on that
        # side does anything but lie there; the victory pose follows anyway.
        for side, keys, other in (("loss", self.mine, self.theirs),
                                  ("win", self.theirs, self.mine)):
            known = [k for k in self._roster(keys) if k in self.alive]
            # A healthy teammate still to come in holds the KO off; one that
            # comes in and fights counts as acting, which resets the hold.
            bench = self._bench_alive(keys, other, chars)
            acting = any(act_class((chars.get(k) or {}).get("act")) not in
                         ("damage", "idle", "other")
                         and (chars.get(k) or {}).get("age", 1e9) <= 60
                         for k in known + bench)
            # Known team size (both line-ups fixed): a team KO needs that many
            # fighters down. In a mirror match a benched fighter shares its
            # name with one fighting on the other side, so _roster and
            # _bench_alive both take it for a frozen twin - two KOs then
            # read as a team KO while the third fighter was still to come
            # in (2026-11-11, Bakugo/Todoroki/Mirio mirror ended at 2:08).
            short = (self.team_size is not None and
                     self._ko_count([k for k in keys if k in self.down]) < self.team_size)
            if (not short and len(known) >= 2 and all(k in self.down for k in known)
                    and not acting):
                since = self._ko_since.setdefault(side, now)
                # The bench hold is capped: should a key wrongly read as a
                # benched fighter sit there idle, the match still ends - the
                # auto loop only returns to the menus on an 'ended'.
                if now - since >= (BENCH_HOLD_MAX_S if bench else KO_CONFIRM_S):
                    return self._end(side, "team KO", now)
            else:
                self._ko_since.pop(side, None)
        return None

    def update_teams(self, snap_chars, teams, now=None):
        """Convenience for live play: `teams` from sampler.teams()."""
        mine = [r.get("key") for r in teams.get("mine") or [] if r.get("key")]
        theirs = [r.get("key") for r in teams.get("theirs") or [] if r.get("key")]
        me = (teams.get("me") or {}).get("key")
        return self.update(snap_chars, mine, theirs, me, now)

    def cancel_start(self, now=None):
        """Undo a 'started' that the caller knows is not a real match (the
        auto loop is still on the results screen). Back to 'over', so the
        real next match - after the menus - is the one that starts."""
        if self.state == "live":
            self.state = "over"
            self._over_since = time.time() if now is None else now
            self._menu_gap = False

    def left_battle(self):
        """Told from outside (the auto loop went through the menus): the next
        battle is a new match, whether or not its intro is seen."""
        if self.state != "live":
            self._menu_gap = True

    def stalled(self, since_s, now=None):
        """The battle Lua has been unreachable for `since_s` seconds."""
        if since_s >= 2.0 and self.state != "live":
            self._menu_gap = True       # menus / loading: the next battle is new
        if self.state == "live" and since_s >= ABANDON_S:
            return self._end("abandoned", "left the match", now or time.time())
        return None

    def close(self, now=None):
        """Stopping mid-match: record what there is, as 'stopped'."""
        if self.state == "live" and self.t0 is not None:
            return self._end("stopped", "run stopped", now or time.time())
        return None

    def _roster(self, keys):
        """The fighters actually in this match. The previous match's fighters
        stay listed for the first ~40 ticks of the next one, and every fighter
        can have a '#1' twin listed on the other side all match - a frozen
        copy whose act is always None (a real mirror copy acts). So a key
        counts when it was listed on THIS side for a fair share of the match
        and is not an idle '#' copy - unless that leaves nothing."""
        side = "mine" if keys is self.mine else "theirs"
        n = max(self.live_n, 1)
        listed = {k for k in keys if self.listed_n[side].get(k, 0) >= ROSTER_MIN_SHARE * n}
        # Twins: 'Iida' and 'Iida#1', on this side or across both. Either can
        # be the real fighter (side B's real ones are often the '#1's), so of
        # a twin group keep whoever acts; the frozen one barely does. (A
        # mirror match has both acting, and keeps both.)
        everyone = set(self.listed_n["mine"]) | set(self.listed_n["theirs"])
        groups = {}
        for k in listed:
            base = k.split("#", 1)[0]
            groups[base] = [j for j in everyone if j.split("#", 1)[0] == base]
        real = set()
        for g in groups.values():
            if len(g) == 1:
                k = g[0]
                if "#" not in k or self.acted_n.get(k, 0) >= ROSTER_COPY_ACT_SHARE * n:
                    real.add(k)
                continue
            act = [k for k in g if self.acted_n.get(k, 0) >= ROSTER_COPY_ACT_SHARE * n]
            keep = act or [max(g, key=lambda k: self.acted_n.get(k, 0))]
            real.update(k for k in keep if k in listed)
        return real or set(keys)

    def _bench_alive(self, keys, other, chars):
        """Teammates still waiting on the bench, healthy (a list, maybe empty). _roster() drops a
        '#N' key that has not acted on 10% of the match - right for the frozen
        twins, but side B's real fighters are often '#1's too, and one that
        has not been tagged in yet has acted on nothing. Taking the other two
        KOs as a team KO ended the match early (2026-11-07: 30 of 231 matches
        were such fragments, the rest of the real match recorded as a new one
        starting mid-fight). A frozen twin is stale (age in the thousands) or
        shares a base name with a fighter on the other side; a benched fighter
        is fresh, healthy and listed on this side all match."""
        from .roster import base_name
        side = "mine" if keys is self.mine else "theirs"
        n = max(self.live_n, 1)
        fresh = lambda k: (chars.get(k) or {}).get("age", 1e9) <= 60
        # Only the other side's fighters that are live right now: a frozen
        # twin can 'act' (it idles) and so pass _roster, but it is stale.
        other_bases = {k.split("#", 1)[0] for k in self._roster(other) if fresh(k)}
        # An alternate form of a KO'd fighter (AllForOneBoy of a downed
        # AwakeAFO) is the same character, not a benched one.
        down_chars = {base_name(k) or k.split("#", 1)[0] for k in self.down if k in keys}
        bench = []
        for k in keys:
            if k in self.down or self.listed_n[side].get(k, 0) < ROSTER_MIN_SHARE * n:
                continue
            r = chars.get(k) or {}
            if not fresh(k) or (r.get("hp") or 0) <= 0.2 or r.get("dead"):
                continue
            if k.split("#", 1)[0] in other_bases:
                continue
            if (base_name(k) or k.split("#", 1)[0]) in down_chars:
                continue
            bench.append(k)
        return bench

    @staticmethod
    def _ko_count(keys):
        """KOs as characters: a fighter's forms (AwakeAFO / AllForOneYouth)
        are one KO, and a team has at most 3."""
        from .roster import base_name
        return min(3, len({base_name(k) or k.split("#", 1)[0] for k in keys}))

    def _tally(self, rm, rt):
        """Damage, KOs and events from the raw per-key tallies, counting only
        the fighters really in the match on each side."""
        for k in rm:
            c = self._char(k)
            c["taken"] = self.drops.get(k, 0.0)
            c["ko"] = k in self.ko_keys
        for c in self.chars.values():
            c["dealt"] = 0.0
        for (p, k), d in self.dealt_to.items():
            if k in rt and p in rm:
                self._char(p)["dealt"] += d
        self.kos = self._ko_count(self.ko_keys & rm)
        self.kos_enemy = self._ko_count(self.ko_keys & rt)
        self.events = {k: 0 for k in EVENT_FIELDS}
        self.enemy_events = {k: 0 for k in EVENT_FIELDS}
        for keys, out in ((rm, self.events), (rt, self.enemy_events)):
            for k in keys:
                for name, v in (self.ev_by.get(k) or {}).items():
                    out[name] += v

    # ---------------------------------------------------------------- end
    def _end(self, result, reason, now):
        self.state = "over"
        self._over_since = now
        rm, rt = self._roster(self.mine), self._roster(self.theirs)
        # Backstop for the victory-pose read: a side whose whole team is down
        # while the other's is not did not win, whatever the pose said.
        # (Counted like kos_taken: forms of one fighter are one KO; teams are 3.)
        out_m = self._ko_count(self.ko_keys & set(rm)) >= 3
        out_t = self._ko_count(self.ko_keys & set(rt)) >= 3
        if result == "win" and out_m and not out_t:
            result, reason = "loss", reason + " (KO check)"
        elif result == "loss" and out_t and not out_m:
            result, reason = "win", reason + " (KO check)"
        self.winner = result
        self.reason = reason
        self._tally(rm, rt)
        mine = sorted(k for k in rm if k in self.alive or k in self.chars)
        theirs = sorted(k for k in rt if k in self.alive)
        self.summary = {
            "t": round(self.t0 or now, 3),
            "ended": round(now, 3),
            "duration_s": round(max(0.0, now - (self.t0 if self.t0 is not None else now)), 1),
            "mode": self.mode,
            "side": self.side,
            "model": self.model,
            "result": result,
            "reason": reason,
            "team": [_short(k) for k in mine],
            "opponents": [_short(k) for k in theirs],
            "team_ids": mine,
            "opponent_ids": theirs,
            # Roster names only - what the statistics group by. Transformation
            # forms (Chisaki2, DabiFullBodyBurn) and still-unnamed fighters
            # have no roster entry and are left out rather than counted as
            # extra characters.
            "team_roster": _roster_names(mine),
            "opponents_roster": _roster_names(theirs),
            "hp_left": {_short(k): round(self.hp.get(k, 0.0), 3) for k in mine},
            "hp_left_enemy": {_short(k): round(self.hp.get(k, 0.0), 3) for k in theirs},
            "kos_taken": self.kos,
            "kos_dealt": self.kos_enemy,
            # What the opponents lost / what we lost. Per-character "dealt"
            # below needs our point fighter identified at the moment of each
            # hit, so it can come up short; these totals cannot.
            "damage_dealt": round(sum(self.drops.get(k, 0.0) for k in rt), 3),
            "damage_taken": round(sum(self.drops.get(k, 0.0) for k in rm), 3),
            "per_char": _merge_forms({k: c for k, c in self.chars.items()
                                      if k in rm}),
            "events": dict(self.events),
            "enemy_events": dict(self.enemy_events),
        }
        return "ended"

    def commit(self, extra=None):
        """Append the finished match (plus any caller stats) to the stats file."""
        if not self.summary:
            return None
        s = dict(self.summary)
        if extra:
            s.update(extra)
        if self.recording:
            s["recording"] = str(self.recording)
        if self.write_enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(s) + "\n")
        self.summary = None
        return s


def describe(s):
    """One console line for a finished match."""
    mins, secs = divmod(int(s.get("duration_s", 0)), 60)
    res = str(s.get("result", "?")).upper()
    return (f"MATCH {res} ({s.get('reason')}) {mins}:{secs:02d}  "
            f"{'/'.join(s.get('team') or ['?'])} vs "
            f"{'/'.join(s.get('opponents') or ['?'])}  "
            f"dealt {s.get('damage_dealt', 0):.2f} taken {s.get('damage_taken', 0):.2f}  "
            f"KOs {s.get('kos_dealt', 0)}-{s.get('kos_taken', 0)}")

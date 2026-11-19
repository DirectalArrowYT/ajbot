"""Two trained bots, one match - the policy plays both sides.

    play2.py                                  # whatever teams are on screen
    play2.py --auto                           # endless: random teams, auto rematch
    play2.py --auto --a X,Y,Z --b X,Y,Z       # endless: the same teams every match
    play2.py --dry                            # decide and print, press nothing
    play2.py --a Bakugo,Dabi,Nomu --b DekuFinal,Todoroki,AllMight
    play2.py --a ... --b ... --pick           # drive character select too
    play2.py --a ... --b ... --model-b models/old.pkl   # new policy vs old

Why this exists: a run recorded by play.py labels exactly ONE character per
tick - whoever `me` happens to be - so a 3v3 session only ever teaches the
three fighters on our side, and only against a CPU. Driving both sides writes
two perspective files from the same wall-clock match, so every tick is
labelled twice and all six fighters produce training data at once.

Both bots share a single Hook and a single snapshot per tick: the pipe is one
request/response channel and is not safe to interleave, and one snapshot means
the two bots see exactly the same world.

Sides: with --a / --b both line-ups are pinned by name. Without them (the
default, like play.py) each bot works its side out live: the `enemy` flag is
relative to Player 1, so the bot on Player 1 starts out reading enemy=0 as its
own team and the bot on Player 2 the reverse, and each then confirms it by
checking that the fighter it thinks it controls moves where ITS stick pushed.
Works mid-battle and with any teams. Mirror picks (the same character on both
teams) share a Lua namespace, so those sides come from the enemy flag.

    !! Self-play data clones the bot's CURRENT behaviour, not good play. It is
    worth training on once runs can be filtered by outcome, which is why each
    file ends with a summary line recording the final health of both sides.

It keeps running across matches: each match gets its own pair of botruns/
files and one stats/matches.jsonl line per bot (mode 'selfplay'), and nobody
presses anything between matches.

Ctrl-C stops and releases both pads (so does creating --stop-file, which is
how the GUI stops it cleanly).
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path

from ajbot import Pad, sampler
from ajbot.pad import SLOT_NAMES, check_physical_slots
from ajbot.pad import tick_all
from ajbot.agent import (Agent, plan_press, TagPolicy, ResourcePolicy,
                         EscapePolicy, DefensePolicy, ACTION_MAP, UNMAPPED,
                         ACTION_HOLD_OVERRIDE,
                         RESOURCE_INPUT, ESCAPE_INPUT, update_chain)
from ajbot.chain import ChainTracker, summarize as chain_summary
from ajbot import combobook, datadir, tagbook
from ajbot import routes as routebook
from ajbot.hook import Hook, HookError
from ajbot.unknowns import UnknownLog, UnknownProjectileLog, UnknownActionLog
from ajbot.match import MatchTracker, describe
from ajbot.heartbeat import Heartbeat, wait_for_battle
from ajbot.autoloop import AutoLoop
from ajbot.roster import INDEX

COUNTDOWN = 5

# Hold lengths, matching the single-bot presses in ajbot.agent.
HOLD_TAG = 5
HOLD_RESOURCE = 4
HOLD_ESCAPE = 4

# One bot's resolved input for a tick. `trigger` is (side, value) for a tag,
# which is an analog trigger rather than a button. `keep` leaves the trigger
# where it was set instead of releasing it at the end of the tick - the
# post-KO partner pick is a HOLD across ticks (TagPolicy.death_pick), and a
# press/release flicker there is not a choice.
Move = collections.namedtuple("Move", "buttons stick trigger hold label keep",
                              defaults=(False,))


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", metavar="X,Y,Z", default=None,
                    help="team for bot A (the lower XInput slot); optional - "
                         "needed only with --pick")
    ap.add_argument("--b", metavar="X,Y,Z", default=None,
                    help="team for bot B; optional - needed only with --pick")
    ap.add_argument("--model", default="models/policy.pkl")
    ap.add_argument("--model-b", default=None,
                    help="different policy for bot B, to play one against "
                         "the other (default: the same policy as A)")
    ap.add_argument("--vigem", action="store_true",
                    help="use ViGEm virtual pads instead of the in-game pads "
                         "(Steam Input can hide ViGEm pads)")
    ap.add_argument("--lockstep", action="store_true",
                    help="drive ajhook's virtual pads and step the game frame "
                         "by frame (no uinput pads, no sleeping): the match runs "
                         "as fast as the game thread and the bots allow. Set the "
                         "engine speed high too (endless.py --speed 10)")
    ap.add_argument("--swap-every", type=int, default=0, metavar="N",
                    help="with --model-b: swap which side each policy plays "
                         "every N matches, so P1/P2 advantage cancels out "
                         "(0 = never)")
    ap.add_argument("--hz", type=float, default=10.0,
                    help="decisions per second. Measured ceiling with two "
                         "bots is ~10: each tick costs (hold + 1) frames of "
                         "press plus one policy evaluation per side")
    ap.add_argument("--hold", type=int, default=3, help="frames to hold a button")
    ap.add_argument("--every", type=int, default=3, help="sampler cadence, in pumps")
    ap.add_argument("--dry", action="store_true", help="decide only, do not press")
    ap.add_argument("--flip-x", action="store_true", help="mirror stick X")
    ap.add_argument("--deadzone", type=float, default=0.18)
    ap.add_argument("--record", metavar="PREFIX", default=None,
                    help="write PREFIX.a.jsonl / PREFIX.b.jsonl "
                         "(default botruns/<timestamp>)")
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--slots", action="store_true",
                    help="require the pads to be Player 1 and Player 2")
    ap.add_argument("--pick", action="store_true",
                    help="drive character select for both sides first")
    ap.add_argument("--pick-start", default="DekuFinal",
                    help="character both cursors start on")
    ap.add_argument("--no-tag", action="store_true", help="disable the tag policy")
    ap.add_argument("--tag-hits", type=int, default=2,
                    help="hits landed before a Change Combo is considered")
    ap.add_argument("--tag-cooldown", type=int, default=12,
                    help="decisions to wait between tags")
    ap.add_argument("--no-defense", action="store_true",
                    help="no just guard / guard / counter / dodge policy")
    ap.add_argument("--defense-file", default=None,
                    help="force one defence table for both sides (default: per "
                         "side, see Bot.tables)")
    ap.add_argument("--learn-dir", default="models/learn",
                    help="where the sparring learner's defence/escape tables live")
    ap.add_argument("--no-learn", action="store_true",
                    help="use the defence table as-is, no exploration")
    ap.add_argument("--epsilon", type=float, default=0.12)
    ap.add_argument("--no-resources", action="store_true",
                    help="do not spend Rising or Plus Ultra")
    ap.add_argument("--no-escape", action="store_true",
                    help="do not use the emergency dodge")
    ap.add_argument("--escapes", type=int, default=1,
                    help="dodges available at match start (+1 per main lost)")
    ap.add_argument("--escape-hits", type=int, default=2,
                    help="hits of a combo on us before the emergency dodge is "
                         "spent (fewer at low health)")
    ap.add_argument("--auto", action="store_true",
                    help="endless bot vs bot: start at the 2-player character "
                         "select; both bots pick Random x3, and after every "
                         "match Player 1 goes Continue -> Character Select and "
                         "they pick Random again")
    ap.add_argument("--auto-cursor", choices=["stay", "reset"], default="stay",
                    help="where the cursor is when character select comes "
                         "back: on the last character picked ('stay', the "
                         "default) or back on the first fighter ('reset'). "
                         "Corrected automatically if the picks come out wrong")
    ap.add_argument("--explore", type=float, default=0.06,
                    help="chance per decision that a bot tries a sensible "
                         "alternative to its top pick (discovery for self-play "
                         "data); 0 = pure policy")
    ap.add_argument("--learner", choices=["A", "B"], default=None,
                    help="this side's model keeps learning: explores and writes "
                         "its defence/escape tables in its own model folder "
                         "(no boss assist). Follows the model through "
                         "--swap-every")
    ap.add_argument("--boss", choices=["A", "B"], default=None,
                    help="sparring: this side is the boss - its policy gets the "
                         "guard assist and leans on --boss-lean; matches are "
                         "logged as mode 'sparring', not 'selfplay'")
    ap.add_argument("--boss-lean", default=None, metavar="MODEL",
                    help="policy blended into the boss's choices (human-only fit)")
    ap.add_argument("--boss-lean-w", type=float, default=0.5)
    ap.add_argument("--critic-beta", type=float, default=1.0,
                    help="how strongly the critic reranks the policy's choices "
                         "(0 = pure behaviour cloning)")
    ap.add_argument("--mirror-chance", type=float, default=0.0,
                    help="--auto: chance per match that the bots may pick the "
                         "same characters (mirror matches); 0 keeps teams disjoint")
    ap.add_argument("--auto-post-wait", type=float, default=18.0,
                    help="seconds from the victory pose to pressing A on the "
                         "results screen")
    ap.add_argument("--control-now", choices=["normal", "manual", "custom"],
                    default=None,
                    help="what the control-mode screen shows RIGHT NOW for "
                         "every row (it remembers between matches); only "
                         "needed after changing it by hand")
    ap.add_argument("--routes", action="store_true",
                    help="press known combo routes (authored, drilled, mined, the "
                         "game's CPU combos) over the model's own picks. Off by "
                         "default: the bot learns combos in training (route reward, "
                         "drills) instead of being driven through them in a match")
    ap.add_argument("--no-routes", action="store_true",
                    help=argparse.SUPPRESS)   # the old opt-out; routes are off by default now
    ap.add_argument("--book", action="store_true",
                    help="let the combo book (models/combobook.json) override the "
                         "model's pick mid-combo. Off by default, like --routes: "
                         "the bot learns its combos in training. (The book is "
                         "still read for tag-in timing either way.)")
    ap.add_argument("--no-book", action="store_true",
                    help="do not load the combo book at all (tag-in timing too)")
    ap.add_argument("--no-stats", action="store_true",
                    help="do not append match results to stats/matches.jsonl")
    ap.add_argument("--stop-file", default=None,
                    help="stop cleanly when this file appears (used by the GUI)")
    ap.add_argument("--matches", type=int, default=0, metavar="N",
                    help="stop after N finished matches. With --model-b the "
                         "two models are then compared side by side (also "
                         "saved to stats/compare.jsonl)")
    return ap.parse_args()


def parse_team(spec, label):
    team = [t.strip() for t in spec.split(",") if t.strip()]
    if len(team) != 3:
        raise SystemExit(f"--{label} needs exactly 3 characters, got {len(team)}")
    unknown = [t for t in team if t not in INDEX]
    if unknown:
        raise SystemExit(f"--{label}: unknown character(s): {', '.join(unknown)}")
    return team


class Bot:
    """One side: a policy, a pad, and its own view of who is on which team."""

    def __init__(self, tag, team, foes, agent, pad, args, shared_q=None,
                 player1=True):
        self.tag = tag
        self.player1 = player1
        self.team = team or []
        # A mirror (a character on both teams) cannot be pinned by name: the
        # pin would put both copies on one side. Use the enemy flag instead.
        self.pinned = bool(team and foes and not set(team) & set(foes))
        self.agent = agent
        self.pad = pad
        self.hold = args.hold
        self.roster = sampler.Roster()
        if self.pinned:
            # Both line-ups are known, so nothing is left to the enemy flag.
            self.roster.assign(team, "mine")
            self.roster.assign(foes, "theirs")
            self.roster.exclusive = True
        else:
            # The enemy flag is relative to Player 1: P1's own fighters read
            # enemy=0. Start from that, let the lever check correct it.
            self.roster.orient = 10.0 if player1 else -10.0
        self.model_path = args.model_b if (tag == "B" and args.model_b) else args.model
        self.data_dir, self.learns, self.table_dir = self.tables(tag, args)
        self.defense = None
        if not args.no_defense:
            boss = args.boss == tag
            self.defense_path = (Path(args.defense_file) if args.defense_file
                                 else datadir.path_in(self.table_dir, "defense.json"))
            self.defense = DefensePolicy(
                path=self.defense_path,
                epsilon=args.epsilon if self.learns else 0.0,
                learn=self.learns, assist=boss)
            # Two sides on the same table share one dict, so neither
            # overwrites the other's lessons when both save.
            if shared_q is not None and shared_q[0] == self.defense_path:
                self.defense.q = shared_q[1]
        self.args = args
        self.new_match()
        self.tracker = MatchTracker(
            mode="sparring" if args.boss else "selfplay", side=tag,
            model=args.model_b if (tag == "B" and args.model_b) else args.model,
            write=not args.no_stats)
        # Knowing the team size keeps a mirror match (benched fighter named
        # like one active on the other side) from ending as a false team KO
        # after two KOs (match.MatchTracker.team_size). The lockstep farm's
        # matches come from nextmatch.start, always three a side.
        if team and foes:
            self.tracker.team_size = len(team)
        elif args.lockstep:
            self.tracker.team_size = 3

        self.out = None
        self.taken = collections.Counter()
        self.n = 0
        self.last = None
        self.teams = {"me": None, "enemy": None, "mine": [], "theirs": []}
        self.action = None
        self.stick = (0.0, 0.0)
        self.escaped = False
        self.spent = None
        self.did_tag = False
        self.blind = 0          # ticks where our own side was not identified
        self.unpinned = set()   # characters no name pin could claim
        self.own_moves = set()  # distinct actions our own fighter ran
        self.warned_idle = False

    # Per-model state, swapped along with the agent by --swap-every.
    MODEL_STATE = ("model_path", "data_dir", "learns", "table_dir", "defense",
                   "defense_path", "combo", "book_raw", "route")

    @staticmethod
    def tables(tag, args):
        """(data folder, learns?, table folder) for one side.

        Each side reads the combo book, move and threat stats next to its own
        policy.pkl (ajbot.datadir), so the champion in models/ is a frozen
        snapshot and a challenger plays with what it was trained with. The
        defence and escape tables are frozen too - read from that folder,
        never written - except for the sparring learner (the side that is
        not the boss), which explores into --learn-dir, never into models/; a new challenger
        takes a snapshot of that table, and the champion inherits it when
        the challenger is promoted. Fair matches therefore run frozen on
        both sides. --no-learn freezes everyone."""
        model = args.model_b if (tag == "B" and args.model_b) else args.model
        data = Path(model).parent
        if args.learner == tag and not args.no_learn:
            # --learner: this model learns into its OWN defence/escape tables
            # (next to its policy.pkl), so what it learns here is what it
            # plays with next time. No boss assist on either side.
            return data, True, data
        learns = bool(args.boss) and args.boss != tag and not args.no_learn
        if learns:
            ld = Path(args.learn_dir)
            ld.mkdir(parents=True, exist_ok=True)
            for f in ("defense.json", "escape.json"):
                seed = datadir.path_in(data, f)
                if not (ld / f).exists() and seed.exists():
                    import shutil
                    shutil.copy2(seed, ld / f)
            return data, True, ld
        return data, False, data

    def new_match(self):
        """Per-match state starts clean; the defence table carries over."""
        args = self.args
        self.tagger = None if args.no_tag else TagPolicy(
            min_hits=args.tag_hits, cooldown=args.tag_cooldown, team=self.team)
        self.resources = None if args.no_resources else ResourcePolicy()
        self.escaper = None if args.no_escape else EscapePolicy(
            budget=args.escapes, min_hits_taken=args.escape_hits,
            path=(self.table_dir / "escape.json" if self.learns
                  else datadir.path_in(self.table_dir, "escape.json")),
            learn=self.learns)
        if getattr(self, "defense", None):
            self.defense.new_match()
        self.taken_at_start = collections.Counter(getattr(self, "taken", {}))
        # Auto-mode detector: a Change Combo or Plus Ultra the bot did not
        # press for means that character is on Normal (auto combo / auto
        # Plus Ultra), whatever the control-mode memory believes.
        from ajbot.sampler import Envelope
        self._auto_env = Envelope()
        self._last_tag_n = self._last_pu_n = -10 ** 6
        self._flagged_auto = set()
        # Idle watchdog (see _watchdog).
        self._no_me_since = None
        self._act_sig = None
        self._act_since = time.monotonic()
        self._idle_reported = set()
        # Combo chains by the game's rule, shared by this bot's policies.
        if not hasattr(self, "combo"):
            book = None if args.no_book else combobook.load(datadir.path_in(self.data_dir, "combobook.json"))
            self.combo = combobook.ComboPolicy(book) if book else None
            self.book_raw = book
            self.route = None if (args.no_routes or not args.routes) else routebook.RouteFollower(routebook.load_all(self.data_dir))
        self.chain = ChainTracker()
        self.match_combos = []
        for pol in (self.tagger, self.resources, self.escaper):
            if pol is not None:
                pol.chain = self.chain
        if self.resources is not None:
            self.resources.tagger = self.tagger
        if self.tagger is not None:
            self.tagger.tagbook = tagbook.get(getattr(self, "book_raw", None))
        if self.tagger is not None and self.combo is not None:
            self.tagger.seg_prior = self.combo.seg_prior()
            self.tagger.tag_prior = self.combo.tag_prior()

    def _watchdog(self, snap, teams):
        """A bot standing still in a live match is a failure, not a quiet spell.

          no fighter   5 s with no point fighter identified - the bot presses
                       nothing. Logged with every live record, then recovered
                       by dropping the name pins (the usual cause: the fighter
                       on point runs under an id no pin matches).
          no reaction  10 s of the point fighter's action not changing while
                       the bot keeps pressing - the inputs are not landing.
                       Logged; nothing to recover from here.
        Both go to logs/crash.log, like errors do.
        """
        now = time.monotonic()
        if self.tracker.state != "live":
            self._no_me_since = None
            self._act_since = now
            return
        me = teams.get("me") or {}
        if not me:
            self._no_me_since = self._no_me_since or now
            if now - self._no_me_since >= 5.0 and "no_me" not in self._idle_reported:
                self._idle_reported.add("no_me")
                self._report_idle("no fighter on point for 5 s", snap, teams)
                if self.roster.pinned or self.roster.exclusive:
                    self.roster.exclusive = False
                    self.roster.pinned.clear()
                    self.pinned = False
                    print(f"  !! bot {self.tag}: dropped its name pins to recover", flush=True)
            return
        self._no_me_since = None
        sig = (me.get("key"), me.get("act"), me.get("act_n", 0) < 3)
        if sig[:2] != (self._act_sig or (None, None))[:2] or sig[2]:
            self._act_sig = sig
            self._act_since = now
            return
        pressing = self.action not in (None, "None")
        from ajbot.sampler import act_class
        # No act at all is the match intro (2026-11-07: all 49 reports came
        # 9-18 s after the match began), not a bot whose inputs go nowhere.
        standing = (me.get("act") not in (None, "None")
                    and act_class(me.get("act")) in ("idle", "move", "other"))
        if (pressing and standing and now - self._act_since >= 10.0
                and "no_react" not in self._idle_reported):
            self._idle_reported.add("no_react")
            self._report_idle(f"{me.get('key')} stuck in '{me.get('act')}' for 10 s "
                              f"while the bot kept pressing", snap, teams)

    def _report_idle(self, what, snap, teams):
        lines = [f"IDLE bot {self.tag}: {what}",
                 f"  team {self.team}  pins {dict(self.roster.pinned)}  "
                 f"exclusive {self.roster.exclusive}  flipped {self.roster.flipped}",
                 f"  me {(teams.get('me') or {}).get('key')}  enemy "
                 f"{(teams.get('enemy') or {}).get('key')}  mine "
                 f"{[r.get('key') for r in teams.get('mine') or []]}"]
        for slot, r in sorted(snap.items(), key=lambda kv: kv[1].get("age", 1e9)):
            if r.get("age", 1e9) > 300:
                continue
            lines.append(f"  {slot:<6} {str(r.get('id')):<28} age {r.get('age')}  "
                         f"act {r.get('act')}  enemy {r.get('enemy')}  sk {r.get('sk')}  "
                         f"hp {r.get('hp')}  lever {r.get('lon')}/{r.get('lscale')}")
        text = "\n".join(lines)
        print("  !! " + text, flush=True)
        Path("logs").mkdir(exist_ok=True)
        with open(Path("logs") / "crash.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text)
            fh.write("\n\n")

    def _check_auto_mode(self, me):
        """Catch a character left on Normal by a lost menu press."""
        from ajbot.roster import base_name
        from ajbot import menus
        key = me.get("key")
        char = base_name(key)
        if not char or self.pad is None:
            return
        # Presses of our own that legitimately produce these events: a tag, a
        # Plus Ultra Combo (it ends with an automatic Change), and a Plus
        # Ultra from either the resource policy or the model.
        if self.did_tag or self.spent == "pu_combo":
            self._last_tag_n = self.n
        if self.spent in ("pu", "pu_combo") or self.action in ("PlusUltra1", "PlusUltra2"):
            self._last_pu_n = self.n
        tagc = self._auto_env.delta(key, "ev_tagc", me.get("ev_tagc"))
        pu = self._auto_env.delta(key, "ev_pu", me.get("ev_pu"))
        unpressed = ((tagc and self.n - self._last_tag_n > 20)
                     or (pu and self.n - self._last_pu_n > 20))
        if unpressed and char not in self._flagged_auto:
            self._flagged_auto.add(char)
            side = "1P" if self.pad.slot == 0 else "2P"
            menus.mark_control_mode(side, char, menus.CONTROL_NORMAL)
            print(f"  !! bot {self.tag}: {char} did an automatic "
                  f"{'Change Combo' if tagc else 'Plus Ultra'} - it is on Normal. "
                  f"Noted; it gets set to Manual next time it is picked.",
                  flush=True)

    def match_extra(self):
        extra = {"actions": dict(self.taken - self.taken_at_start),
                 "combo_stats": chain_summary(self.match_combos)}
        if self.tagger:
            extra["tag_stats"] = dict(self.tagger.stats)
            tb = self.tagger.tagbook
            if tb is not None:
                tb.observe(self.match_combos)
                try:
                    tb.save()
                except OSError as e:
                    print(f"  !! tag book not saved: {e}")
                extra["tagbook_picks"] = dict(tb.picks)
        if self.escaper:
            extra["emergency_dodges"] = self.escaper.used
        if self.defense:
            extra["defense_stats"] = {k: (dict(v) if isinstance(v, dict) else v)
                                      for k, v in self.defense.stats.items()}
        return extra

    # ---- decide ----
    def plan(self, snap):
        # This side's move/threat stats (and the features its policy was
        # trained on) come from its own model folder.
        with datadir.use(self.data_dir):
            return self._plan(snap)

    def _plan(self, snap):
        """Decide this tick and return the input to apply.

        Bookkeeping (spending a dodge, marking a tag) happens here rather than
        at press time, because the press is staged and paid for later; the two
        would otherwise fall a tick out of step.
        """
        self.roster.observe(snap)
        if not self.pinned:
            # Our fighter is the one whose lever follows OUR last stick.
            self.roster.observe_control(snap, self.stick)
        action, stick, teams = self.agent.decide(snap, self.roster)
        update_chain(self.chain, teams)
        if self.resources is not None:
            action = self.resources.gate_model_pu(action, teams)
        self._check_auto_mode(teams.get("me") or {})
        self._watchdog(snap, teams)
        mine_keys = {r.get("key") for r in teams.get("mine") or []}
        self.match_combos.extend(c for c in self.chain.drain() if c.victim not in mine_keys)
        if self.combo:
            self.combo.observe(teams.get("me"))
        if getattr(self, "route", None):
            self.route.observe(teams.get("me"), teams.get("enemy"))
        self.teams, self.action, self.stick = teams, action, stick
        self.escaped, self.spent, self.did_tag = False, None, False
        self.defense_label = None
        self.n += 1
        if not teams.get("me"):
            self.blind += 1
        me = teams.get("me") or {}
        for f in ("cmd", "act"):
            if me.get(f) not in (None, "None", "Idle"):
                self.own_moves.add(me.get(f))
        if self.n == 300 and len(self.own_moves) <= 1 and not self.warned_idle:
            # In the first self-play run on disk bot B's fighter never ran a
            # single action - its pad was not driving Player 2, so bot A
            # spent 21k ticks farming a statue and B learned nothing.
            self.warned_idle = True
            print(f"  !! bot {self.tag}: its fighter has not done anything in "
                  f"300 ticks - this pad is probably NOT controlling its "
                  f"player. Check the controller assignment in-game.")
        for rec in (teams.get("mine") or []) + (teams.get("theirs") or []):
            key = rec.get("key")
            # The 22 characters with no Lua namespace are keyed by slot handle,
            # so no name pin reaches them and they fall back to the enemy flag
            # - which is exactly the flag that cannot be trusted here.
            if self.pinned and key and self.roster.pin_of(key) is None:
                self.unpinned.add(key)
        if not self.pinned:
            names = [r.get("id") or r.get("key") for r in (teams.get("mine") or [])
                     if r.get("age", 1e9) <= 800]
            if names:
                self.team = names

        # Escaping outranks everything: while being comboed nothing else the
        # bot presses will come out anyway.
        if self.tagger:
            self.tagger.observe(teams)
            # A KO of our point fighter opens the game's partner-select
            # window (hold LT/RT to choose who comes in). Nothing else the
            # dead fighter could press matters, so this outranks the rest:
            # hold the chosen trigger until the newcomer is on point, then
            # let it go.
            was = self.tagger.held
            pick = self.tagger.death_pick(teams)
            if pick:
                if pick != was:
                    self.taken["PICK"] += 1
                return Move((), stick, (pick, 1.0), HOLD_TAG,
                            f"PICK {pick} ({self.tagger.pick_name or '?'})", keep=True)
            if was:
                return Move((), stick, (was, 0.0), 1, "PICK END")
        # The action head's Escape is a vote for EscapePolicy, which owns the
        # stock; on its own it presses nothing.
        model_esc = action == "Escape"
        if model_esc:
            action = None
        if self.escaper:
            self.escaper.observe(teams)
            if self.escaper.should_escape(teams, model_wants=model_esc):
                self.escaper.spend(teams)
                self.escaped = True
                self.taken["ESCAPE"] += 1
                return Move(ESCAPE_INPUT, stick, None, HOLD_ESCAPE, "ESCAPE")

        # Then resources: narrow windows worth more than whatever the action
        # head wanted this tick.
        if self.resources:
            what = self.resources.decide(teams)
            if what:
                self.resources.spend(what, teams)
                self.spent = what
                self.taken[what.upper()] += 1
                if what == "pu_combo":
                    side = self.tagger.partner if self.tagger else "left"
                    return Move((), stick, (side, 1.0), HOLD_TAG, "PU COMBO")
                return Move(RESOURCE_INPUT[what], stick, None,
                            HOLD_RESOURCE, what.upper())

        if self.tagger and self.tagger.should_tag(teams):
            side = self.tagger.partner
            self.tagger.tagged(teams)
            self.did_tag = True
            self.taken["TAG"] += 1
            return Move((), stick, (side, 1.0), HOLD_TAG,
                        f"TAG {side} {self.tagger.reason}")

        if self.defense:
            plan = self.defense.decide(teams)
            if plan:
                label, buttons, dstick, hold = plan
                self.defense_label = label
                if label != "wait":
                    self.taken[label] += 1
                return Move(tuple(buttons), dstick if dstick is not None else stick,
                            None, hold, label)

        me = teams.get("me") or {}
        # A pickup-buff Plus Ultra (Momo's Lucky Bag) drops where we stand:
        # stay under it until it lands, unless the enemy closes in.
        if self.resources and self.resources.staying(teams):
            self.taken["BAG"] += 1
            return Move((), (0.0, 0.0), None, 1, "BAG")
        if getattr(self, "route", None):
            rs = self.route.suggest(me, action, lambda a: a in ACTION_MAP
                                    and self.agent._available(a, me),
                                    comboing=self.chain.by_us is not None)
            if rs:
                action = rs
                self.taken["ROUTE"] += 1
            if self.route.holding(me):
                stick = (0.0, 0.0)
        if (self.combo and self.args.book and self.chain.by_us is not None
                and not (getattr(self, "route", None) and self.route.progress(me))):
            sug = self.combo.suggest(me, action, lambda a: a in ACTION_MAP
                                     and self.agent._available(a, me))
            if sug:
                action = sug
                self.taken["BOOK"] += 1
        if self.tagger is not None and self.tagger.handoff:
            op = self.tagger.opener(me, lambda a: a in ACTION_MAP
                                    and self.agent._available(a, me))
            if op:
                action = op
                self.taken["OPENER"] += 1
        buttons, stick, name = plan_press(action, stick, me, teams.get("enemy"))
        self.stick = stick
        self.taken[name or "-"] += 1
        # Nothing to press still costs a frame, so the stick change lands.
        hold = ACTION_HOLD_OVERRIDE.get(name, self.hold) if name else 1
        return Move(buttons, stick, None, hold, name or "-")

    # ---- report ----
    def line(self, mv):
        me, en = self.teams.get("me") or {}, self.teams.get("enemy") or {}
        return (f"  {self.tag} [{self.n:5d}] {str(me.get('id', '-')):<20} "
                f"{mv.label:<14} stick=({self.stick[0]:+.2f},{self.stick[1]:+.2f})"
                f"  vs {str(en.get('id', '-')):<20} hp={en.get('hp', 0):.2f}")

    def log(self, chars, mv):
        """Same schema record.py and play.py write, from THIS bot's side."""
        if not self.out:
            return
        me, en = self.teams.get("me") or {}, self.teams.get("enemy") or {}
        self.out.write_rec({
            "t": round(time.time(), 3), "chars": chars,
            "me": me.get("key"), "enemy": en.get("key"),
            "mine": [c.get("key") for c in self.teams.get("mine") or []],
            "theirs": [c.get("key") for c in self.teams.get("theirs") or []],
            "roster": self.roster.sides(),
            "action": me.get("cmd"),
            "lever": [me.get("lang"), me.get("lscale"), me.get("lon")],
            "chose": self.action,
            "stick": [round(self.stick[0], 3), round(self.stick[1], 3)],
            "tag": self.did_tag, "escape": self.escaped, "resource": self.spent,
            "defense": self.defense_label,
            "bot": True, "side": self.tag, "selfplay": True,
        })

    def health(self):
        """Remaining health per character on this bot's own side."""
        return {str(c.get("id") or c.get("key")):
                round(float(c.get("hp") or 0.0), 3)
                for c in (self.teams.get("mine") or [])}


def apply_moves(pairs):
    """Press every bot's move on one shared frame clock.

    Staged first, then advanced together: the whole tick costs max(hold) + 1
    frames no matter how many pads are driven, where doing it per pad would
    cost the sum.
    """
    pads = [bot.pad for bot, _ in pairs]
    for bot, mv in pairs:
        bot.pad.stick(mv.stick)
        if mv.buttons:
            bot.pad.hold(*mv.buttons)
        if mv.trigger:
            bot.pad.trigger(mv.trigger[0], mv.trigger[1])

    elapsed = 0
    for h in sorted({mv.hold for _, mv in pairs}):
        tick_all(pads, h - elapsed)
        elapsed = h
        for bot, mv in pairs:
            if mv.hold != h:
                continue
            if mv.buttons:
                bot.pad.release(*mv.buttons)
            if mv.trigger and not mv.keep:
                bot.pad.trigger(mv.trigger[0], 0.0)
    tick_all(pads, 1)           # push the releases


def do_pick(bots, hook, start):
    """Drive character select from both panels, then set everyone to Manual.

    NOT verified against the live 2P screen - the single-pad flow in play.py
    is. If it desyncs, set the match up by hand and run without --pick.
    """
    from ajbot import menus
    navs = []
    for bot in bots:
        # The Random cell sits on the opposite edge for 1P and 2P, so
        # horizontal routing depends on which slot the pad actually got.
        side = menus.detect_side(bot.pad, hook, "1P" if bot.pad.slot == 0 else "2P")
        bot.pick_side = side
        print(f"{bot.tag}: picking {' / '.join(bot.team)} as {side}")
        navs.append((bot, menus.CharacterSelect(bot.pad, start=start, side=side, hook=hook)))

    def one(bot, nav):
        nav.select_team(bot.team)
        bot.pad.idle(menus.SETTLE)
        menus.set_all_manual(bot.pad, count=3,
                             side=getattr(bot, "pick_side", None) or
                             ("1P" if bot.pad.slot == 0 else "2P"),
                             chars=bot.team, hook=hook)
        bot.pad.idle(30)

    # Both players pick their team and set control modes at the same time.
    menus.run_parallel([(lambda b=bot, n=nav: one(b, n)) for bot, nav in navs])

    print("teams locked in; waiting for the match to load", end="", flush=True)
    for _ in range(120):
        if hook.has_state():
            break
        print(".", end="", flush=True)
        time.sleep(0.5)
    print()
    if not hook.has_state():
        print("  match never started - are both pads on the right slots?")
        return False
    print("  match is live")
    sampler.reset(hook)
    return True


def _inst_tag():
    i = os.environ.get("AJ_INSTANCE", "")
    return f"-i{i}" if i not in ("", "0") else ""


def open_outputs(bots, args):
    if args.no_record:
        return
    prefix = (Path(args.record) if args.record
              else Path("botruns") / (str(int(time.time())) + _inst_tag()))
    prefix.parent.mkdir(parents=True, exist_ok=True)
    for bot in bots:
        path = prefix.with_name(f"{prefix.name}.{bot.tag.lower()}.jsonl")
        if path.exists():
            # A second match inside the same second - never overwrite.
            path = prefix.with_name(f"{prefix.name}_{int(time.time()*1000)%1000}"
                                    f".{bot.tag.lower()}.jsonl")
        from ajbot.recguard import RecGuard
        bot.out = RecGuard(path.open("w", encoding="utf-8"), path)
        bot.path = path
        print(f"recording {bot.tag} to {path}")


def close_outputs(bots, ticks):
    """Finish each file with a summary line.

    Self-play is only worth training on once runs can be filtered by outcome,
    and nothing else in a tick stream records who won. The line carries no
    `chars`, so dataset.py skips it as a tick.
    """
    for bot in bots:
        if not bot.out:
            continue
        end = getattr(bot, "at_end", None) or {}
        mine = end.get("hp") or bot.health()
        theirs = {}
        for other in bots:
            if other is not bot:
                theirs = (getattr(other, "at_end", None) or {}).get("hp") or other.health()
        bot.out.write(json.dumps({
            "summary": True, "side": bot.tag, "selfplay": True,
            "team": end.get("team") or bot.team, "ticks": ticks, "decisions": bot.n,
            "final_hp": mine, "final_hp_enemy": theirs,
            "hp_left": round(sum(mine.values()), 3),
            "hp_left_enemy": round(sum(theirs.values()), 3),
            "actions": dict(bot.taken),
        }) + "\n")
        if not bot.out.close():
            bot.path = None               # discarded: never showed fighting
        bot.out = None
    for bot in bots:
        bot.at_end = None


def main():
    args = parse_args()
    if args.control_now:
        from ajbot import menus
        menus.forget_control_modes(None, {"normal": menus.CONTROL_NORMAL,
                                          "manual": menus.CONTROL_MANUAL,
                                          "custom": menus.CONTROL_CUSTOM}[args.control_now])
        print(f"control-mode screen: treating every row as {args.control_now}")
    if bool(args.a) != bool(args.b) and not args.auto:
        print("give both --a and --b, or neither (neither = use the teams "
              "already selected in-game)")
        return 1
    if args.pick and not args.a:
        print("--pick needs --a and --b: the bots have to know whom to pick")
        return 1
    team_a = parse_team(args.a, "a") if args.a else None
    team_b = parse_team(args.b, "b") if args.b else None
    if not team_a:
        print("teams: using whatever is selected in-game (sides worked out live)")
    overlap = set(team_a or []) & set(team_b or [])
    if overlap:
        # Mirror picks: sides cannot be pinned by name, so the bots fall back
        # to the enemy flag (sampler.dedupe/teams keep both copies apart).
        print(f"mirror: {', '.join(sorted(overlap))} on both teams - sides "
              f"come from the enemy flag, not name pins.")

    try:
        agent_a = Agent(args.model, flip_x=args.flip_x, deadzone=args.deadzone)
    except FileNotFoundError:
        print(f"no policy at {args.model} - run: python training/train.py --save models")
        return 1
    if (args.model_b and args.model_b != args.model) or args.boss:
        try:
            agent_b = Agent(args.model_b or args.model, flip_x=args.flip_x,
                            deadzone=args.deadzone)
        except FileNotFoundError:
            print(f"no policy at {args.model_b}")
            return 1
        print(f"A: {args.model}   B: {args.model_b}")
    else:
        agent_b = agent_a          # stateless, so one load serves both sides
        print(f"policy: {args.model} (both sides)")
    print(f"  {len(agent_a.actions)} actions, {len(agent_a.chars)} characters")
    agent_a.explore = agent_b.explore = args.explore
    if args.boss:
        boss = agent_a if args.boss == "A" else agent_b
        boss.explore = 0.0
        if args.boss_lean:
            try:
                boss.lean = Agent(args.boss_lean, flip_x=args.flip_x, deadzone=args.deadzone)
                boss.lean_w = args.boss_lean_w
            except FileNotFoundError:
                print(f"no lean policy at {args.boss_lean} - boss plays without it")
        print(f"  boss: side {args.boss}, guard assist"
              + (f", leans {boss.lean_w:.0%} on {args.boss_lean}" if boss.lean else ""))
    agent_a.critic_beta = agent_b.critic_beta = args.critic_beta
    print(f"  critic: {'on, beta %.2f' % args.critic_beta if agent_a.critic is not None and args.critic_beta > 0 else 'off'}")
    if args.explore:
        print(f"  discovery on: {args.explore:.0%} of decisions try an alternative")
    unmapped = sorted(set(agent_a.actions) - set(ACTION_MAP) - UNMAPPED)
    if unmapped:
        print(f"  no input mapping for: {unmapped} (these will be skipped)")

    try:
        hook = Hook()
    except HookError as e:
        print(e)
        return 1
    from ajbot import menus as _menus
    _menus.use_hook(hook)
    # With --pick we are expected to be in character select, where the battle
    # Lua is not running yet, so the state gate comes after picking.
    if args.auto and (args.pick or args.dry):
        print("--auto drives character select itself: drop --pick/--dry "
              "(--a/--b fix the teams it picks every match)")
        return 1
    if not args.pick and not args.auto and not hook.has_state():
        print("not in a battle yet - waiting (start a match whenever you like)")
        if not wait_for_battle(hook, args.stop_file):
            print("stopped before a match started")
            return 0

    sampler.install(hook, every_pumps=args.every)
    sampler.reset(hook)

    pads = [None, None]
    link = None
    if args.lockstep and not args.dry:
        from ajbot.lockstep import Link
        link = Link(hook)
        pads = [link.pad(0), link.pad(1)]
        print(f"lockstep: virtual pads on slots 0 and 1, game frame {link.frame}")
    hook_pads = None
    if not args.lockstep and not args.dry and not args.vigem:
        # Both bot pads inside the game (ajhook answers its XInput), out of
        # Steam Input's reach; ViGEm below only for an old hook.
        from ajbot.hookpad import make_pads
        hook_pads = make_pads(2, hook)
        if hook_pads:
            pads = hook_pads
            print(f"in-game pads: A -> slot {pads[0].slot} "
                  f"({SLOT_NAMES.get(pads[0].slot, '?')}), B -> slot "
                  f"{pads[1].slot} ({SLOT_NAMES.get(pads[1].slot, '?')})")
            if args.slots and {pads[0].slot, pads[1].slot} != {0, 1}:
                print("  !! wanted Player 1 and Player 2, but a controller holds one")
                print("     of those slots - unplug it, or drop --slots.")
                return 1
        else:
            print("  (in-game pads unavailable - falling back to ViGEm pads)")
    if not hook_pads and not args.dry and not args.lockstep:
        # Two bot pads, no human on this run - nothing should already be
        # occupying an XInput slot before either of them exists.
        check_physical_slots(expected=0)
        # Order matters: XInput hands out slots in connection order, so the
        # first pad created is the lower player number.
        pads[0] = Pad()
        slot_a = pads[0].slot
        pads[1] = Pad()
        slot_b = pads[1].slot
        if slot_a < 0:
            slot_a = pads[0].slot     # A can show up late; it is asked again
            if slot_a == slot_b:
                slot_a = -1           # that was B's slot, not A's
        print(f"pad A -> slot {slot_a} ({SLOT_NAMES.get(slot_a, '?')})")
        print(f"pad B -> slot {slot_b} ({SLOT_NAMES.get(slot_b, '?')})")
        if slot_a == slot_b or slot_a < 0 or slot_b < 0:
            if any(p.hidden_by_steam() for p in pads):
                print("  !! the game cannot see one of the bot pads.")
                print("     Windows has it on a slot the game reads as empty: Steam")
                print("     is still holding that player for an unplugged Steam")
                print("     Controller. Close All's Justice, keep the Steam Controller")
                print("     off, start the game again, then start the bot.")
            else:
                print("  !! could not tell the two pads apart - unplug physical "
                      "controllers and try again.")
            for p in pads:
                p.reset()
            return 1
        if args.slots and {slot_a, slot_b} != {0, 1}:
            print("  !! wanted Player 1 and Player 2. XInput assigns slots in")
            print("     connection order - unplug or disable any physical")
            print("     controller, then start again.")
            for p in pads:
                p.reset()
            return 1

    # Which bot is Player 1 decides how it reads the enemy flag. Under --dry
    # there are no pads; A is assumed to be Player 1.
    a_p1 = True
    if pads[0] and pads[1]:
        a_p1 = pads[0].slot < pads[1].slot
    bot_a = Bot("A", team_a, team_b, agent_a, pads[0], args, player1=a_p1)
    bots = [bot_a,
            Bot("B", team_b, team_a, agent_b, pads[1], args,
                shared_q=(bot_a.defense_path, bot_a.defense.q) if bot_a.defense else None,
                player1=not a_p1)]

    if not args.dry and not link:
        for t in range(COUNTDOWN, 0, -1):
            print(f"  starting in {t}...", end="\r", flush=True)
            time.sleep(1)
        print(" " * 40, end="\r")

    auto = None
    if args.auto:
        auto = AutoLoop(pads, p1_index=0 if a_p1 else 1,
                        post_wait=args.auto_post_wait, start=args.pick_start,
                        cursor_mode=args.auto_cursor,
                        mirror_chance=args.mirror_chance,
                        fixed={0: team_a, 1: team_b})

        def pin_picked_teams():
            """The loop picked both teams, so each bot KNOWS its side. Pinned
            the moment the picks are made - before the match loads - so no
            tracker ever sees a tick with the sides inferred from the enemy
            flag (which put both teams on one bot and scored both as wins)."""
            for b in bots:
                if b.pad is None:
                    continue
                ours, theirs = auto.pins(b.pad is auto.p1)
                if ours and set(ours) & set(theirs):
                    b.tracker.left_battle()        # mirror: no name pins
                    b.tracker.team_size = len(ours)    # ...but the size is known
                    continue
                if ours:
                    b.roster.pinned.clear()
                    b.roster.reset()
                    b.roster.assign(ours, "mine")
                    b.roster.assign(theirs, "theirs")
                    b.roster.exclusive = True
                    b.team, b.pinned = ours, True
                    b.tracker.team_size = len(ours) if theirs else None
                # We just came through the menus: the next battle is a new
                # match even though its intro will have played long ago.
                b.tracker.left_battle()

        auto.on_picked = pin_picked_teams
        if team_a or team_b:
            print(f"[auto] fixed teams every match: A {'/'.join(team_a or ['random'])}"
                  f" vs B {'/'.join(team_b or ['random'])}", flush=True)
        print("[auto] endless mode: picking teams now - be on the "
              "2-player character select screen", flush=True)
        auto.start()

    if args.pick:
        if args.dry:
            print("--pick needs real pads; ignoring it under --dry")
        elif not do_pick(bots, hook, args.pick_start):
            for p in pads:
                p.reset()
            return 1

    unknowns = UnknownLog(expected=(team_a or []) + (team_b or []),
                          source="play2")
    proj_log = UnknownProjectileLog(source="play2")
    action_log = UnknownActionLog(source="play2")
    next_proj_poll = time.perf_counter()

    duel = agent_b is not agent_a
    # Head-to-head tally: policy -> [wins, losses], plus wins per side.
    tally = {args.model: [0, 0], args.model_b: [0, 0]} if duel else None
    side_wins = collections.Counter()
    # Every stats line of this run carries its id, so a comparison can be
    # rebuilt later (python -m ajbot.compare --run ID).
    run_id = time.strftime("%Y%m%d-%H%M%S") + _inst_tag()
    rows = []                   # this run's stats lines, both bots
    per_bot = collections.Counter()
    done = [False]
    if args.matches:
        print(f"stopping after {args.matches} match(es)"
              + (", then comparing the two models" if duel else ""))

    def finish(bot):
        # The file's summary line is written when the NEXT match opens its
        # files - by then the team is re-picked and health refilled - so what
        # it reports is captured here, at the end.
        bot.at_end = {"team": list(bot.team), "hp": bot.health()}
        extra = bot.match_extra()
        extra["run"] = run_id
        s = bot.tracker.commit(extra)
        if s:
            rows.append(s)
            per_bot[bot.tag] += 1
            if args.matches and min(per_bot[b.tag] for b in bots) >= args.matches:
                done[0] = True
            print(f"  bot {bot.tag}: " + describe(s))
            if auto and bot.pad is not None:
                auto.verify(bot.pad is auto.p1, s.get("team_roster"))
            if duel and s.get("result") in ("win", "loss"):
                won = s["result"] == "win"
                tally[bot.tracker.model][0 if won else 1] += 1
                if won:
                    side_wins["P1" if bot.player1 else "P2"] += 1
                if bot is bots[1]:
                    ma, mb = args.model, args.model_b
                    wa, la = tally[ma]
                    n = wa + la
                    print(f"  [duel] {Path(ma).stem} {wa} - {tally[mb][0]} "
                          f"{Path(mb).stem}"
                          + (f"  ({wa / n:.0%} for {Path(ma).stem})" if n else "")
                          + f"  | P1 won {side_wins['P1']}, P2 won {side_wins['P2']}")

    period = 1.0 / args.hz
    period_frames = max(1, round(60.0 / args.hz))

    def pace(nxt, tick_frame):
        """Wait out the rest of this decision period. Lockstep: run the game
        frames the period has left instead of sleeping."""
        if link:
            left = tick_frame + period_frames - link.frame
            if left > 0:
                link.step(left)
            return nxt
        nxt += period
        delay = nxt - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            nxt = time.perf_counter()
        return nxt
    hb = Heartbeat(enabled=not args.no_record)
    ticks = stalls = matches = crashes = 0
    stall_since = None
    try:
        nxt = time.perf_counter()
        while True:
            tick_frame = link.frame if link else 0
            try:
                if auto:
                    auto.tick()
                if done[0]:
                    print(f"\n{args.matches} match(es) played - stopping")
                    break
                if args.stop_file and Path(args.stop_file).exists():
                    try:
                        Path(args.stop_file).unlink()
                    except OSError:
                        pass
                    print("\nstop requested")
                    break
                try:
                    snap = sampler.snapshot(hook)
                    stall_since = None
                except HookError:
                    stalls += 1
                    if stalls % 20 == 1:
                        print("  ...waiting (paused or not in a match)")
                    stall_since = stall_since or time.time()
                    for bot in bots:
                        if bot.tracker.stalled(time.time() - stall_since) == "ended":
                            finish(bot)
                    hb.idle("NO BATTLE (menus, loading or paused)", matches)
                    if link:
                        link.step(15)   # keep the game going: it waits on us
                    else:
                        time.sleep(0.25)
                    nxt = time.perf_counter()
                    continue

                # Match boundaries, judged from each bot's own side.
                deduped = sampler.dedupe(snap)
                for bot in bots:
                    bot.roster.observe(snap)
                    ev = bot.tracker.update_teams(
                        deduped, sampler.teams(snap, roster=bot.roster))
                    if ev == "ended":
                        finish(bot)
                        if auto and bot is bots[0]:
                            auto.match_ended()
                    elif ev == "started" and bot is bots[0]:
                        if auto and not auto.match_started():
                            # Still on the results screen after the last match
                            # (health refilled, states live): not a new match.
                            # Counting it opened a recording that held the
                            # results screen + menus + the real match, and the
                            # real start was then never seen.
                            for b in bots:
                                b.tracker.cancel_start()
                            continue
                        matches += 1
                        if (duel and args.swap_every > 0 and matches > 1
                                and (matches - 1) % args.swap_every == 0):
                            # Same teams and pads; only the policies change
                            # sides, so each one plays both P1 and P2.
                            a, b = bots
                            a.agent, b.agent = b.agent, a.agent
                            a.tracker.model, b.tracker.model = (
                                b.tracker.model, a.tracker.model)
                            # Everything else that belongs to a model goes
                            # with it: its combo book, routes, move/threat
                            # data and defence/escape tables. Before this
                            # only the policy moved, so after a swap each
                            # policy played with the other model's data.
                            for k in Bot.MODEL_STATE:
                                va, vb = getattr(a, k, None), getattr(b, k, None)
                                setattr(a, k, vb)
                                setattr(b, k, va)
                            print(f"[duel] sides swapped: A now "
                                  f"{Path(a.tracker.model).stem}, B now "
                                  f"{Path(b.tracker.model).stem}")
                        print(f"\n=== match {matches} started ===")
                        close_outputs(bots, ticks)
                        open_outputs(bots, args)
                        for b in bots:
                            b.tracker.recording = getattr(b, "path", None)
                        hb.new_match()
                if auto and auto.phase == "post_match":
                    # A tracker that went live AFTER the last match ended is
                    # reading the results screen, not a new match.
                    for b in bots:
                        if b.tracker.state == "live" and (b.tracker.t0 or 0) >= getattr(auto, "ended_at", 0.0):
                            b.tracker.cancel_start()
                for bot in bots:
                    if bot.tracker.state == "live" and getattr(bot, "_match_seen", None) != bot.tracker.t0:
                        bot._match_seen = bot.tracker.t0
                        if not bot.pinned:
                            bot.roster.reset()
                            bot.roster.observe(snap)
                        if bot.defense:
                            bot.defense.save()      # last match's lessons (no-op when frozen)
                        bot.new_match()
                if not any(bot.tracker.state == "live" for bot in bots):
                    # Victory pose, result screen, menus: no presses.
                    for p in pads:
                        if p:
                            p.clear()
                    tick_all(pads, 1)
                    hb.idle("WAITING for a match", matches)
                    nxt = pace(nxt, tick_frame)
                    continue

                ticks += 1
                pairs = [(bot, bot.plan(snap)) for bot in bots]
                unknowns.observe(snap, bots[0].teams)
                action_log.observe(snap)
                now_pc = time.perf_counter()
                # Lockstep: a Lua eval cannot run while the game is parked
                # waiting on us, so this poll would just sit out its timeout.
                if now_pc >= next_proj_poll and not link:
                    proj_log.poll(hook)
                    next_proj_poll = now_pc + 5.0

                if not args.dry:
                    apply_moves(pairs)

                if any(bot.out for bot in bots):
                    # Built once: the character table is the same for both sides,
                    # only the me/enemy pointers into it differ.
                    chars = {}
                    for k, c in sampler.dedupe(snap).items():
                        c = {kk: (round(vv, 3) if isinstance(vv, float) else vv)
                             for kk, vv in c.items()}
                        c["slot"] = k
                        chars[k] = c
                    # The hook's own state table never forgets a lua_State
                    # once seen - across a long --auto session this tick's
                    # chars would otherwise carry every earlier match's
                    # fighters too (see sampler.prune_stale). Union both
                    # bots' relevant keys since the table is shared.
                    keep = set()
                    for bot in bots:
                        t = bot.teams
                        keep.update(k for k in
                                   ((t.get("me") or {}).get("key"),
                                    (t.get("enemy") or {}).get("key")) if k)
                        keep.update(c.get("key") for c in t.get("mine") or [])
                        keep.update(c.get("key") for c in t.get("theirs") or [])
                    chars = sampler.prune_stale(chars, keep)
                    for bot, mv in pairs:
                        bot.log(chars, mv)
                    hb.wrote()
                a_teams = bots[0].teams
                hb.live(matches, getattr(bots[0], "path", None) if bots[0].out else None,
                        (a_teams.get("me") or {}).get("key"),
                        (a_teams.get("enemy") or {}).get("key"))

                for bot, mv in pairs:
                    if mv.label != bot.last or ticks % 30 == 0:
                        print(bot.line(mv))
                        bot.last = mv.label

                nxt = pace(nxt, tick_frame)
            except KeyboardInterrupt:
                raise
            except Exception:
                # One bad tick must not end an overnight run: the virtual
                # pads vanish with the process and the game pauses on
                # 'controller disconnected'. Log it and carry on.
                import traceback
                crashes += 1
                msg = traceback.format_exc()
                print(f"  !! error in the play loop (#{crashes}), continuing:", flush=True)
                print(msg, flush=True)
                Path("logs").mkdir(exist_ok=True)
                with open(Path("logs") / "crash.log", "a", encoding="utf-8") as fh:
                    fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg)
                    fh.write("\n")
                if crashes > 50:
                    print("  !! 50 errors - giving up", flush=True)
                    break
                time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nstopped")
    except RuntimeError as e:
        print(f"\n{e}")
        return 1
    finally:
        for bot in bots:
            if bot.tracker.close() == "ended":
                finish(bot)
        print(f"{matches} match(es) this run")
        if duel and rows:
            from ajbot import compare
            entry = compare.build(run_id, args.model, args.model_b, rows)
            if not args.no_stats:
                compare.save(entry)
            print("\n" + compare.report(entry), flush=True)
        close_outputs(bots, ticks)
        line = unknowns.summary()
        if line:
            print(line)
        unknowns.close()
        line = proj_log.summary()
        if line:
            print(line)
        proj_log.close()
        line = action_log.summary()
        if line:
            print(line)
        action_log.close()
        for p in pads:
            if p:
                p.reset()
        if link:
            link.release()
            print(f"lockstep: {link.timeouts} slow steps (map loads)")
        try:
            sampler.stop(hook)
        except HookError:
            pass
        hook.close()

        print(f"\n{ticks} ticks")
        for bot in bots:
            live = bot.health()
            print(f"\n  bot {bot.tag}: {bot.n} decisions, "
                  f"{len(live)} of its 3 fighters seen")
            if bot.blind:
                print(f"    !! {bot.blind} ticks with no fighter on its side - "
                      f"check the --{bot.tag.lower()} spelling against the "
                      f"lua ids printed above")
            if bot.unpinned:
                print(f"    !! not pinned to a side: "
                      f"{', '.join(sorted(bot.unpinned))} - these have no lua "
                      f"namespace, so they fell back to the enemy flag")
            if live:
                print("    final hp: " +
                      ", ".join(f"{k}={v:.2f}" for k, v in live.items()))
            for a, c in bot.taken.most_common(10):
                print(f"    {str(a):<16}{c:6d}  {100*c/max(bot.n, 1):5.1f}%")
            if bot.route:
                for ln in bot.route.report().splitlines():
                    print("    " + ln)
            if bot.tagger:
                print("    " + bot.tagger.summary())
                if bot.tagger.tagbook is not None and bot is bots[-1]:
                    print("    " + bot.tagger.tagbook.summary())
            if bot.defense:
                print("    " + bot.defense.summary())
        for bot in bots:
            if bot.defense:
                bot.defense.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())

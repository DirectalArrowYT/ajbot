"""Run the trained policy live - the bot plays.

    play.py --p1 --pick Bakugo,Eraserhead,Nomu
    play.py --dry                  # decide and print, press nothing
    play.py --hz 20 --hold 3
    play.py --flip-x               # if it moves mirrored

Built for TRAINING MODE: nobody can be KO'd there, so the bot can keep playing
indefinitely while the CPU still combos and tags.

The virtual pad appears as an extra controller. XInput assigns slots in
connection order, so for Player 1 the pad must exist before any physical
controller does; --p1 refuses to start otherwise.

The model handles attacks and movement. Around it (priority order):

  escape    - emergency dodge out of a combo
  resources - Rising, Plus Ultra, Plus Ultra Combo
  tag       - Change / Change Combo, with partner cooldowns learned live
  defence   - just guard / guard / counter / dodge against an incoming attack,
              chosen by a bandit that learns which answer pays per situation
              and remembers it in models/defense.json between sessions

Without --pick the sides are worked out from the enemy flag plus two checks
that hold (the CPU reports ai=1; our fighter's lever follows OUR stick). The
old rule - "whoever has a live lever is ours" - pinned the CPU's fighters onto
our team, which is why the first random runs were 90-99% blind.

It keeps running across matches: each match is detected (intro pose to
victory pose), recorded to its own botruns/ file and summarised into
stats/matches.jsonl. Between matches the bot presses nothing.

Ctrl-C stops and releases the pad (so does creating --stop-file, which is how
the GUI stops it cleanly).
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import argparse
import collections
import json
import sys
import time
from pathlib import Path

from ajbot import Pad, sampler
from ajbot.pad import SLOT_NAMES, check_physical_slots
from ajbot.agent import (Agent, press, press_tag, press_resource, press_escape,
                         press_defense, hold_tag, release_tag, TagPolicy,
                         ResourcePolicy, EscapePolicy,
                         DefensePolicy, ACTION_MAP, UNMAPPED, update_chain)
from ajbot.chain import ChainTracker, summarize as chain_summary
from ajbot import combobook, tagbook
from ajbot import routes as routebook
from ajbot.hook import Hook, HookError
from ajbot.unknowns import UnknownLog, UnknownProjectileLog, UnknownActionLog
from ajbot.match import MatchTracker, describe
from ajbot.heartbeat import Heartbeat, wait_for_battle

COUNTDOWN = 5


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/policy.pkl")
    ap.add_argument("--hz", type=float, default=15.0, help="decisions per second")
    ap.add_argument("--hold", type=int, default=3, help="frames to hold a button")
    ap.add_argument("--every", type=int, default=3, help="sampler cadence, in pumps")
    ap.add_argument("--dry", action="store_true", help="decide only, do not press")
    ap.add_argument("--flip-x", action="store_true", help="mirror stick X")
    ap.add_argument("--deadzone", type=float, default=0.18)
    ap.add_argument("--record", metavar="FILE", default=None,
                    help="log the bot's play (default botruns/<timestamp>.jsonl)")
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--p1", action="store_true",
                    help="require the pad to be Player 1 (XInput slot 0)")
    ap.add_argument("--vigem", action="store_true",
                    help="drive the game through a ViGEm virtual pad instead of "
                         "the in-game pad (Steam Input can hide ViGEm pads)")
    ap.add_argument("--online", action="store_true",
                    help="online lobby: the bot is Player 1 (implies --p1) and "
                         "waits as long as it takes for the opponent to lock in")
    ap.add_argument("--pick", metavar="A,B,C", default=None,
                    help="pick this team on character select first, then play")
    ap.add_argument("--pick-best", action="store_true",
                    help="pick the team the bot is most confident with (win-rate "
                         "lower bound in stats/matches.jsonl)")
    ap.add_argument("--pick-counter", action="store_true",
                    help="online: pick the team with the best record against the "
                         "team last fought, and re-pick it at character select "
                         "between matches")
    ap.add_argument("--pick-random", action="store_true",
                    help="pick three random characters, and a new random team "
                         "at character select before every match")
    ap.add_argument("--pick-start", default="DekuFinal",
                    help="character the cursor starts on")
    ap.add_argument("--no-tag", action="store_true", help="disable the tag policy")
    ap.add_argument("--tag-hits", type=int, default=2,
                    help="hits landed before a Change Combo is considered")
    ap.add_argument("--tag-cooldown", type=int, default=12,
                    help="decisions to wait between tags")
    ap.add_argument("--no-defense", action="store_true",
                    help="no just guard / guard / counter / dodge policy")
    ap.add_argument("--defense-file", default="models/defense.json",
                    help="where the defence bandit keeps what it learned")
    ap.add_argument("--no-learn", action="store_true",
                    help="use the defence table as-is, no exploration")
    ap.add_argument("--epsilon", type=float, default=0.12,
                    help="defence exploration rate")
    ap.add_argument("--no-resources", action="store_true",
                    help="do not spend Rising or Plus Ultra")
    ap.add_argument("--no-escape", action="store_true",
                    help="do not use the emergency dodge")
    ap.add_argument("--escapes", type=int, default=1,
                    help="dodges available at match start (+1 per main lost)")
    ap.add_argument("--escape-hits", type=int, default=2,
                    help="hits of a combo on us before the emergency dodge is "
                         "spent (fewer at low health)")
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
    ap.add_argument("--critic-beta", type=float, default=1.0,
                    help="how strongly the critic reranks the policy's choices "
                         "(0 = pure behaviour cloning)")
    ap.add_argument("--no-stats", action="store_true",
                    help="do not append match results to stats/matches.jsonl")
    ap.add_argument("--stop-file", default=None,
                    help="stop cleanly when this file appears (used by the GUI)")
    return ap.parse_args()


def _should_stop(path):
    if path and Path(path).exists():
        try:
            Path(path).unlink()
        except OSError:
            pass
        return True
    return False


def do_pick(pad, hook, roster, team, start, load_wait_s=60.0, stop_file=None):
    """Drive character select, set everyone to Manual, wait for the match."""
    from ajbot import menus
    # The Random cell sits on the opposite edge for 1P and 2P, so horizontal
    # routing depends on which slot the pad actually got.
    side = menus.detect_side(pad, hook, "1P" if pad.slot == 0 else "2P")
    # The enemy flag is relative to Player 1: on Player 2 our own fighters read
    # enemy=1. Without this the side tracker started out assuming Player 1,
    # and in a mirror match (the same character on both teams) the name pin
    # then went to Player 1's copy - the bot took the human's fighter for its
    # own (matches logged as wins with nothing dealt).
    roster.orient = 10.0 if side == "1P" else -10.0
    print(f"picking {' / '.join(team)} as {side} (cursor assumed on {start})")
    nav = menus.CharacterSelect(pad, start=start, side=side, hook=hook)
    nav.select_team(team)
    pad.idle(menus.SETTLE * 2)
    menus.set_all_manual(pad, count=3, side=side, chars=team, hook=hook)
    pad.idle(60)

    print("team locked in; waiting for the match to load", end="", flush=True)
    deadline = time.time() + load_wait_s
    while time.time() < deadline:
        if hook.has_state() or _should_stop(stop_file):
            break
        print(".", end="", flush=True)
        time.sleep(0.5)
    print()
    if not hook.has_state():
        print("  match never started - is the pad on the right player slot?")
        return False
    print("  match is live")
    sampler.reset(hook)
    # We chose this team, so pin it. The enemy flag is relative to the viewing
    # perspective and reports the bot's OWN characters as enemies on Player 1.
    roster.assign(team, "mine")
    print(f"  pinned as mine: {', '.join(team)}")
    return side


def choose_team(args, opponents=None):
    """The team to pick: counter the opponents' team when --pick-counter and
    their team is known, else the most confident team."""
    from ajbot.autoloop import best_team, counter_team
    if args.pick_random:
        # The bot picks the characters itself (not the game's Random cell)
        # so it knows each row for the control-mode screen.
        import random
        from ajbot.roster import DISPLAY_ORDER, NO_NAMESPACE
        team = random.sample([c for c in DISPLAY_ORDER if c not in NO_NAMESPACE], 3)
        print(f"random team: {' / '.join(team)}")
        return team
    if args.pick_counter and opponents:
        team, why = counter_team(opponents)
        if len(team) >= 3:
            print(f"counter-pick vs {' / '.join(opponents)}: " + ", ".join(
                f"{n} ({why[n][0]:.2f}, {why[n][1]} games vs them)" for n in team))
            return team
    team = best_team()
    if len(team) >= 3:
        print(f"most confident team: {' / '.join(team)}"
              + (" (opponents unknown yet)" if args.pick_counter else ""))
    return team


def last_online_opponents():
    from ajbot.autoloop import last_online_opponents as f
    return f()


def select_visible(hook, side):
    """True while character select is on screen for `side` and not yet locked
    in: the cursor feed is fresh."""
    from ajbot import menus
    st = menus._pad_state(hook, side)
    return bool(st and "pos" in st and not st.get("complete")
                and st.get("age_ms", 1 << 30) <= menus.CURSOR_MAX_AGE_MS)


def repick(pad, hook, roster, team, side):
    """Pick `team` at character select between online matches (no waiting for
    the load: the main loop sees the match start)."""
    from ajbot import menus
    print(f"\nre-picking {' / '.join(team)} as {side}")
    nav = menus.CharacterSelect(pad, start=team[0], side=side, hook=hook)
    nav.select_team(team)
    pad.idle(menus.SETTLE * 2)
    menus.set_all_manual(pad, count=3, side=side, chars=team, hook=hook)
    pad.idle(60)
    # The previous team's pins survive Roster.reset on purpose; a new team
    # replaces them.
    roster.pinned.clear()
    roster.assign(team, "mine")
    print(f"  locked in; pinned as mine: {', '.join(team)}")


def main():
    args = parse_args()
    if args.online:
        args.p1 = True
    if (args.pick_best or args.pick_counter or args.pick_random) and not args.pick:
        team = choose_team(args, args.online and last_online_opponents())
        if len(team) < 3:
            print("not enough finished matches in stats/matches.jsonl to rank a team")
            return 1
        args.pick = ",".join(team)
    if args.control_now:
        from ajbot import menus
        menus.forget_control_modes(None, {"normal": menus.CONTROL_NORMAL,
                                          "manual": menus.CONTROL_MANUAL,
                                          "custom": menus.CONTROL_CUSTOM}[args.control_now])
        print(f"control-mode screen: treating every row as {args.control_now}")

    try:
        agent = Agent(args.model, flip_x=args.flip_x, deadzone=args.deadzone)
    except FileNotFoundError:
        print(f"no policy at {args.model} - run: python training/train.py --save models")
        return 1
    print(f"policy: {len(agent.actions)} actions, {len(agent.chars)} characters")
    agent.critic_beta = args.critic_beta
    print(f"critic: {'on, beta %.2f' % args.critic_beta if agent.critic is not None and args.critic_beta > 0 else 'off'}")
    print(f"mapped: {sorted(set(agent.actions) & set(ACTION_MAP))}")
    unmapped = sorted(set(agent.actions) - set(ACTION_MAP) - UNMAPPED)
    if unmapped:
        print(f"no input mapping for: {unmapped} (these will be skipped)")

    try:
        hook = Hook()
    except HookError as e:
        print(e)
        return 1
    from ajbot import menus as _menus
    _menus.use_hook(hook)
    # With --pick we are expected to be in character select, where the battle
    # Lua is not running yet, so the state gate comes after picking.
    if not args.pick and not hook.has_state():
        print("not in a battle yet - waiting (start a match whenever you like)")
        if not wait_for_battle(hook, args.stop_file):
            print("stopped before a match started")
            return 0

    sampler.install(hook, every_pumps=args.every)
    sampler.reset(hook)
    roster = sampler.Roster()
    team_pick = [t.strip() for t in (args.pick or "").split(",") if t.strip()]
    def new_tagger():
        return None if args.no_tag else TagPolicy(min_hits=args.tag_hits,
                                                  cooldown=args.tag_cooldown,
                                                  team=team_pick)
    tagger = new_tagger()
    defense = None if args.no_defense else DefensePolicy(
        path=args.defense_file, epsilon=0.0 if args.no_learn else args.epsilon,
        learn=not args.no_learn)
    if defense and defense.q:
        print(f"defence: loaded {sum(len(v) for v in defense.q.values())} "
              f"learned answers from {args.defense_file}")
    resources = None if args.no_resources else ResourcePolicy()
    escaper = None if args.no_escape else EscapePolicy(
        budget=args.escapes, min_hits_taken=args.escape_hits)

    # Combo chains by the game's rule, shared by every policy; and the combo
    # book mined from the recordings (python training/combobook.py builds it).
    book = None if args.no_book else combobook.load()
    combo = combobook.ComboPolicy(book) if book else None
    if combo:
        print(f"combo book: {book.get('segments', 0)} segments, "
              f"{len(combo.chars)} characters"
              + ("" if args.book else " (tag-in timing only; --book follows it)"))
    elif not args.no_book:
        print("no combo book yet - build one with: python training/combobook.py")
    route = None if (args.no_routes or not args.routes) else routebook.RouteFollower(routebook.load_all(Path(args.model).parent))
    if route:
        print(f"authored routes: {', '.join(sorted(route.routes))}; "
              f"CPU combo routes for {len(route.ai_routes)} characters")

    def wire():
        """Fresh chain tracker, shared by the policies for this match."""
        chain = ChainTracker()
        for pol in (tagger, resources, escaper):
            if pol is not None:
                pol.chain = chain
        if resources is not None:
            resources.tagger = tagger
        if tagger is not None:
            tagger.tagbook = tagbook.get(book)
        if tagger is not None and combo is not None:
            tagger.seg_prior = combo.seg_prior()
            tagger.tag_prior = combo.tag_prior()
        return chain

    chain = wire()
    match_combos = []

    if not args.dry:
        # Before creating the bot's own pad: is more than one XInput slot
        # already occupied? A human on this adapter is expected to hold one.
        check_physical_slots(expected=1)
    pad = None
    if not args.dry:
        # The bot's pad lives inside the game (ajhook answers the game's
        # XInput on the first free slot), so Steam Input cannot hide it.
        # ViGEm only when asked, or when the installed hook is too old.
        if not args.vigem:
            from ajbot.hookpad import make_pads
            pads = make_pads(1, hook)
            if pads:
                pad = pads[0]
            else:
                print("  (in-game pad unavailable - falling back to a ViGEm pad)")
        if pad is None:
            pad = Pad()
    if pad:
        slot = pad.slot
        kind = "in-game" if getattr(pad, "hook", None) is not None else "virtual"
        print(f"{kind} pad created -> XInput slot {slot} "
              f"({SLOT_NAMES.get(slot, '?')})")
        # Starting guess for which enemy-flag value is ours (refined live by the
        # stick check, and set exactly when --pick measures the side).
        roster.orient = 10.0 if slot == 0 else -10.0
        if slot < 0 and pad.hidden_by_steam():
            print("  !! the game cannot see the bot's virtual pad - Windows has it,")
            print("     but Steam Input is not passing it to All's Justice. Close")
            print("     the game, turn any Steam Controller off, start the game")
            print("     again, then start the bot.")
            pad.reset()
            return 1
        if args.p1 and slot != 0:
            print("  !! wanted Player 1 but this pad is not slot 0.")
            if pad.hidden_by_steam():
                print("     The game cannot see this pad: Steam is still holding")
                print("     Player 1 for an unplugged Steam Controller. Close")
                print("     All's Justice, keep the Steam Controller off, start")
                print("     the game again, then start the bot.")
            else:
                print("     XInput assigns slots in connection order - unplug or")
                print("     disable any physical controller, then start again.")
            pad.reset()
            return 1
        for t in range(COUNTDOWN, 0, -1):
            print(f"  starting in {t}...", end="\r", flush=True)
            time.sleep(1)
        print(" " * 40, end="\r")

    picked_side = None
    if args.pick:
        from ajbot.roster import INDEX
        team = [t.strip() for t in args.pick.split(",") if t.strip()]
        unknown = [t for t in team if t not in INDEX]
        if unknown:
            print(f"unknown character(s): {', '.join(unknown)}")
            if pad:
                pad.reset()
            return 1
        if pad:
            picked_side = do_pick(pad, hook, roster, team, args.pick_start,
                                  load_wait_s=1800.0 if args.online else 60.0,
                                  stop_file=args.stop_file)
            if not picked_side:
                pad.reset()
                return 1

    out = None
    matches = 0

    def open_recording():
        """A new recording file for the match that just started."""
        if args.no_record:
            return None
        if args.record:
            base = Path(args.record)
            path = base if matches <= 1 else base.with_name(
                f"{base.stem}.m{matches}{base.suffix}")
        else:
            path = Path("botruns") / f"{int(time.time())}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"  recording to {path}")
        tracker.recording = path
        return path.open("w", encoding="utf-8")

    tracker = MatchTracker(mode="bot", model=args.model, write=not args.no_stats)
    hb = Heartbeat(enabled=not args.no_record)
    match_taken = collections.Counter()
    stall_since = None

    def finish_match():
        """Close the match's recording and write its stats line."""
        nonlocal out
        if out:
            out.close()
            out = None
        extra = {"actions": dict(match_taken),
                 "team_picked": team_pick or None,
                 "online": bool(args.online),
                 "pick_mode": ("random" if args.pick_random else
                               "counter" if args.pick_counter else
                               "best" if args.pick_best else
                               "fixed" if team_pick else "manual"),
                 "combo_stats": chain_summary(match_combos)}
        if tagger:
            extra["tag_stats"] = dict(tagger.stats)
            if tagger.tagbook is not None:
                tagger.tagbook.observe(match_combos)
                try:
                    tagger.tagbook.save()
                except OSError as e:
                    print(f"  !! tag book not saved: {e}")
                extra["tagbook_picks"] = dict(tagger.tagbook.picks)
        if escaper:
            extra["emergency_dodges"] = escaper.used
            extra["escape_stats"] = dict(escaper.stats, opp_combo_scale=round(escaper.scale, 2))
        if resources:
            extra["resource_stats"] = {"pressed": dict(resources.spent),
                                       "came_out": dict(resources.came)}
            print("  " + resources.summary())
        if defense:
            extra["defense_stats"] = {k: (dict(v) if isinstance(v, dict) else v)
                                      for k, v in defense.stats.items()}
        s = tracker.commit(extra)
        if s:
            print("  " + describe(s))
        plan_repick(s)

    pending = None        # team to pick when character select comes back
    # Online always drives Player 1 (--online implies --p1); otherwise the
    # side character select measured.
    my_side = "1P" if args.online else (picked_side or "1P")

    def plan_repick(s):
        nonlocal pending
        if not (pad and (args.pick_random or (args.online and
                                               (args.pick_counter or args.pick_best)))):
            return
        opp = (s or {}).get("opponents_roster") or (s or {}).get("opponents") or []
        team = choose_team(args, opp)
        if len(team) >= 3:
            pending = team
            print(f"  next pick at character select: {' / '.join(team)}")

    unknowns = UnknownLog(expected=team_pick, source="play")
    proj_log = UnknownProjectileLog(source="play")
    action_log = UnknownActionLog(source="play")
    next_proj_poll = time.perf_counter()
    period = 1.0 / args.hz
    taken = collections.Counter()
    n = stalls = blind = 0
    last = None
    last_stick = (0.0, 0.0)
    last_wait_diag = 0.0
    ev_seen = 0
    try:
        nxt = time.perf_counter()
        while True:
            if _should_stop(args.stop_file):
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
                if tracker.stalled(time.time() - stall_since) == "ended":
                    finish_match()
                if pending and select_visible(hook, my_side):
                    team, pending = pending, None
                    try:
                        repick(pad, hook, roster, team, my_side)
                        team_pick[:] = team
                        unknowns.expected = list(team)
                    except Exception as e:   # noqa: BLE001 - keep playing
                        print(f"  !! re-pick failed ({type(e).__name__}: {e}); "
                              f"playing whatever is picked")
                hb.idle("NO BATTLE (menus, loading or paused)", matches)
                time.sleep(0.25)
                nxt = time.perf_counter()
                continue

            roster.observe(snap)
            # Which enemy-flag value is our team: CPU fighters report ai=1,
            # and our fighter's lever follows the stick we pushed last tick.
            roster.observe_control(snap, last_stick)

            action, stick, teams = agent.decide(snap, roster)
            me, en = teams["me"], teams["enemy"]
            unknowns.observe(snap, teams)
            action_log.observe(snap)
            now = time.perf_counter()
            if now >= next_proj_poll:
                proj_log.poll(hook)
                next_proj_poll = now + 5.0

            event = tracker.update_teams(sampler.dedupe(snap), teams)
            if event == "started":
                if pending:
                    print("  (character select never showed - kept the old team)")
                    pending = None
                matches += 1
                print(f"\n=== match {matches} started ===")
                if out:
                    out.close()
                out = open_recording()
                hb.new_match()
                # Per-match state starts clean; the defence table carries over
                # because it is learning across matches on purpose.
                roster.reset()
                roster.observe(snap)
                if defense:
                    defense.new_match()   # the per-match layer: a new person
                tagger = new_tagger()
                resources = None if args.no_resources else ResourcePolicy()
                escaper = None if args.no_escape else EscapePolicy(
                    budget=args.escapes, min_hits_taken=args.escape_hits)
                chain = wire()
                match_combos.clear()
                match_taken.clear()
            elif event == "ended":
                finish_match()
                print("  waiting for the next match (press nothing until then)")
            if tracker.state != "live":
                # Victory pose, result screen, menus: pressing buttons here
                # would skip screens or navigate menus.
                if pad:
                    pad.clear()
                    pad.tick(1)
                if time.time() - last_wait_diag >= 4.0:
                    last_wait_diag = time.time()
                    mine_n = [r.get("id", "?") for r in teams.get("mine") or []]
                    theirs_n = [r.get("id", "?") for r in teams.get("theirs") or []]
                    print(f"  ...still waiting: state={tracker.state} "
                          f"mine={mine_n} theirs={theirs_n} me={(me or {}).get('id')} "
                          f"enemy={(en or {}).get('id')}")
                    raw = sampler.dedupe(snap)
                    print(f"  ...raw snapshot: {len(raw)} entities")
                    for k, r in raw.items():
                        print(f"      {k:<20} id={r.get('id')!r:<20} "
                              f"enemy={r.get('enemy')} age={r.get('age')} "
                              f"active={r.get('active')} ai={r.get('ai')}")
                hb.idle("WAITING for a match", matches)
                last_stick = (0.0, 0.0)
                nxt += period
                delay = nxt - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    nxt = time.perf_counter()
                continue
            n += 1
            if me and me.get("act") not in (None, "None"):
                ev_seen += 1          # the action wrappers are live

            # No opponent means every enemy feature is a zero, so the bot is
            # deciding from half a picture AND the recording is unusable for
            # training. Without --pick the side split rests on
            # infer_from_lever, which fails silently when the enemy flag reads
            # 0 for everyone: five runs on disk are 94-100% blind, one of them
            # 97k ticks. Say so early rather than after the session.
            if en is None:
                blind += 1
                if blind in (30, 300) or blind % 3000 == 0:
                    print(f"  !! no opponent identified for {blind} of {n} "
                          f"ticks - this recording will not be trainable. "
                          f"Restart with --pick so the team can be pinned.")

            update_chain(chain, teams)
            if resources is not None:
                action = resources.gate_model_pu(action, teams)
            match_combos.extend(c for c in chain.drain() if c.victim not in
                                {r.get("key") for r in teams["mine"]})
            if combo:
                combo.observe(me)
            if route:
                route.observe(me, (teams or {}).get("enemy"))

            # Escaping outranks everything: while being comboed nothing else
            # the bot presses will come out anyway.
            escaped = False
            # The action head's Escape is a vote for EscapePolicy, which owns
            # the stock; on its own it presses nothing.
            model_esc = action == "Escape"
            if model_esc:
                action = None
            if escaper:
                escaper.observe(teams)
                if escaper.should_escape(teams, model_wants=model_esc):
                    if pad:
                        press_escape(pad, escaper, teams=teams)
                    else:
                        escaper.spend(teams)
                    escaped = True
                    taken["ESCAPE"] += 1
                    print(f"  [{n:5d}] ESCAPE  ({escaper.budget - escaper.used} left) "
                          f"{escaper.last_reason}")

            # Then resources: both are narrow windows worth more than whatever
            # the action head wanted this tick.
            spent = None
            if not escaped and resources:
                spent = resources.decide(teams)
                if spent:
                    side = tagger.partner if tagger else "left"
                    if pad:
                        press_resource(pad, resources, spent, teams=teams, side=side)
                    else:
                        resources.spend(spent, teams)
                    taken[spent.upper()] += 1
                    print(f"  [{n:5d}] {spent.upper()}")

            did_tag = False
            picked = None
            if tagger:
                # Every tick, so swaps and hits are seen even when a higher-
                # priority press used the decision.
                tagger.observe(teams)
                # A KO of our point fighter opens the game's partner-select
                # window (hold LT/RT to pick who comes in). Hold the chosen
                # trigger across ticks until the newcomer is on point.
                was = tagger.held
                picked = tagger.death_pick(teams)
                if picked:
                    if pad:
                        hold_tag(pad, picked, hold_frames=args.hold)
                    if picked != was:
                        taken["PICK"] += 1
                        print(f"  [{n:5d}] PICK -> {picked} ({tagger.pick_name or '?'})")
                elif was:
                    if pad:
                        release_tag(pad, was)
            if not escaped and spent is None and tagger and not picked:
                if tagger.should_tag(teams):
                    if pad:
                        press_tag(pad, tagger, teams=teams)
                    else:
                        tagger.tagged(teams)
                    did_tag = True
                    taken["TAG"] += 1
                    chain_note = f" chain x{tagger.chain_len}" if tagger.chain_len > 1 else ""
                    print(f"  [{n:5d}] TAG -> {tagger.partner} ({tagger.reason}){chain_note}")

            guarded = None
            if not (escaped or spent or did_tag) and defense:
                plan = defense.decide(teams)
                if plan:
                    guarded = plan[0]
                    if pad:
                        press_defense(pad, plan)
                    if guarded != "wait":
                        taken[guarded] += 1

            if escaped or spent or did_tag or guarded:
                pass          # that press already consumed this decision
            elif pad:
                # With --book, mid-combo, the combo book knows which continuation keeps the
                # hits coming longest for this character.
                # With --routes, a known route (models/authored_routes.json) outranks the
                # mined book: someone who knows the character wrote it down.
                if route:
                    rs = route.suggest(me, action, lambda a: a in ACTION_MAP
                                       and agent._available(a, me or {}),
                                       comboing=chain.by_us is not None)
                    if rs:
                        action = rs
                        match_taken["ROUTE"] += 1
                    if route.holding(me):
                        stick = (0.0, 0.0)
                if combo and args.book and chain.by_us is not None and not (route and route.progress(me)):
                    sug = combo.suggest(me, action, lambda a: a in ACTION_MAP
                                        and agent._available(a, me or {}))
                    if sug:
                        action = sug
                        match_taken["BOOK"] += 1
                if tagger is not None and tagger.handoff:
                    op = tagger.opener(me, lambda a: a in ACTION_MAP
                                       and agent._available(a, me or {}))
                    if op:
                        action = op
                        match_taken["OPENER"] += 1
                did = press(pad, action, stick, hold_frames=args.hold, me=me, en=en)
                taken[did or "-"] += 1
                match_taken[did or "-"] += 1
            else:
                taken[action or "-"] += 1
                match_taken[action or "-"] += 1
            for label in ("ESCAPE" if escaped else None,
                          spent.upper() if spent else None,
                          "TAG" if did_tag else None,
                          guarded if guarded and guarded != "wait" else None):
                if label:
                    match_taken[label] += 1

            if out:
                # Same schema record.py writes, so bot runs and human runs are
                # directly comparable, plus what the bot chose.
                chars = {}
                for k, c in sampler.dedupe(snap).items():
                    c = {kk: (round(vv, 3) if isinstance(vv, float) else vv)
                         for kk, vv in c.items()}
                    c["slot"] = k
                    chars[k] = c
                mine_keys = [c.get("key") for c in teams["mine"]]
                theirs_keys = [c.get("key") for c in teams["theirs"]]
                # The hook's own state table never forgets a lua_State once
                # seen - across a long session this tick's chars would
                # otherwise carry every earlier match's fighters too (see
                # sampler.prune_stale).
                keep = {k for k in ((me or {}).get("key"), (en or {}).get("key")) if k}
                keep.update(mine_keys)
                keep.update(theirs_keys)
                chars = sampler.prune_stale(chars, keep)
                out.write(json.dumps({
                    "t": round(time.time(), 3), "chars": chars,
                    "me": (me or {}).get("key"), "enemy": (en or {}).get("key"),
                    "mine": mine_keys,
                    "theirs": theirs_keys,
                    "roster": roster.sides(),
                    "action": (me or {}).get("cmd"),
                    "lever": [(me or {}).get("lang"), (me or {}).get("lscale"),
                              (me or {}).get("lon")],
                    "chose": action,
                    "stick": [round(stick[0], 3), round(stick[1], 3)],
                    "tag": did_tag, "escape": escaped, "resource": spent,
                    "defense": guarded, "bot": True,
                }) + "\n")
                hb.wrote()
            hb.live(matches, tracker.recording if out else None,
                    (me or {}).get("key"), (en or {}).get("key"))

            last_stick = stick
            if action != last or n % 30 == 0:
                mid = (me or {}).get("id", "-")
                eid = (en or {}).get("id", "-")
                print(f"  [{n:5d}] {mid:<20} act={str(action):<14} "
                      f"stick=({stick[0]:+.2f},{stick[1]:+.2f})  vs {eid} "
                      f"hp={(en or {}).get('hp', 0):.2f}")
                last = action

            nxt += period
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.perf_counter()
    except KeyboardInterrupt:
        print("\nstopped")
    except RuntimeError as e:
        print(f"\n{e}")
        return 1
    finally:
        if tracker.close() == "ended":
            finish_match()
        if out:
            out.close()
        print(f"{matches} match(es) this run")
        if pad:
            pad.reset()
        try:
            sampler.stop(hook)
        except HookError:
            pass
        hook.close()
        print(f"{n} decisions")
        if blind:
            print(f"  !! {blind} ({100*blind/max(n,1):.0f}%) had no opponent; "
                  f"above ~50% train.py will skip this run entirely")
        for a, c in taken.most_common(14):
            print(f"   {str(a):<16}{c:6d}  {100*c/max(n, 1):5.1f}%")
        if route:
            for ln in route.report().splitlines():
                print("  " + ln)
        if tagger:
            print("  " + tagger.summary())
            if tagger.tagbook is not None:
                print("  " + tagger.tagbook.summary())
        if defense:
            defense.save()
            print("  " + defense.summary())
            rows = defense.table()
            if rows:
                print("  best answer learned so far, per situation:")
                for r in rows:
                    print(r)
        if n > 200 and ev_seen == 0:
            print("  !! no action/event data in any sample - the sampler's")
            print("     action wrappers are not reporting. Defence and the new")
            print("     tag/escape logic fall back to the old flags.")
        if roster.flipped:
            print("  (sides: the enemy flag was read FLIPPED this run)")
        line = unknowns.summary()
        if line:
            print("  " + line)
        unknowns.close()
        line = proj_log.summary()
        if line:
            print("  " + line)
        proj_log.close()
        line = action_log.summary()
        if line:
            print("  " + line)
        action_log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

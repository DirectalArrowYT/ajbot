"""Endless matches: when a match ends, start the next one with random teams
and a random stage - no menus.

    python3 endless.py                 # forever
    python3 endless.py --matches 5     # stop after 5 new matches
    python3 endless.py --all           # random picks include Story/DLC entries
    python3 endless.py --stage random  # any free-battle stage (default: the gym, St809)

Needs the offline game with ajhook + AJProbe. Start it right after launching
the game: it waits for the hook; at the title it waits for the save to load,
then starts the first match itself (boot straight into a match). In a battle
or on a result screen it just picks up from there. Each new match goes
through nextmatch.start(); both players are human slots for the two ML bots.

Matches go title -> stage directly. The pause menu there reads "single
player mode" and Return to Main Menu loads forever, which bots never use.
Going title -> hub (st_MS01) -> stage instead fixes those but leaves the
battle camera actor at the world origin for the whole game session (see
go_hub), so the hub route is not used.

Match end = the game log reaching "OnBeginBattleSequence = EventVictory" (the
result screen). ajhook's native pump still runs there, so the next OpenLevel
works from it.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import nextmatch as nm

HERE = Path(__file__).resolve().parent
# Match end = the game's own battle sequence reaching the result screen. It
# logs every phase change ("OnBeginBattleSequence = Battle", ... "BattleEnd*",
# "BeforeEventVictory", "EventVictory"). In a free battle EventVictory IS the
# result screen (victory poses + FreeBattleResult buttons) and waits there for
# a button; "Result" only shows up in some other contents. Every end (KO or
# time up) passes EventVictory, and travelling from it works. (UMG
# IsVisible() reads false for every widget in this game, so the end banners
# cannot be watched directly.)
GYM = "St809"  # EStageId 21 ArchivesYueiHighSchoolGymnasiumGamma: ML matches stay here

END_MARKS = ("OnBeginBattleSequence = EventVictory", "OnBeginBattleSequence = Result")


class LogWatch:
    """Reads only what the game appended to HeroGame.log since the last call."""

    def __init__(self, path):
        self.path = path
        self.pos = path.stat().st_size if path and path.exists() else 0

    def new_text(self):
        if not self.path:
            return ""
        try:
            size = self.path.stat().st_size
            if size < self.pos:  # new game session: the log was rotated
                self.pos = 0
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
            self.pos += len(data)
            return data.decode("utf-8", "replace")
        except OSError:
            return ""


def hook_alive():
    try:
        return nm.call("F", timeout=5).startswith("frames=")
    except OSError:
        return False


def probe(lua, timeout=20):
    try:
        r = subprocess.run([sys.executable, str(HERE / "ueprobe.py"), lua],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def in_battle():
    try:
        return nm.call("Ereturn 1", timeout=5) == "1"
    except OSError:
        return False


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


SAVE_LOADED = "USaveLoadWork::Load / Status[Successed]"


def game_log():
    """The running game's HeroGame.log (inside the Proton prefix)."""
    from ajbot import local
    prefixes = [os.environ.get("AJ_PREFIX", ""), local.get("game.prefix", ""),
                str(HERE.parent / "aj-prefix")]
    # Game copy N (offline_game.sh AJ_INSTANCE=N) runs in <prefix>-N. Never
    # fall back to another copy's log: its match ends are not ours.
    inst = os.environ.get("AJ_INSTANCE", "")
    if inst not in ("", "0"):
        prefixes = [p + f"-{inst}" for p in prefixes if p]
    tail = "/AppData/Local/BANDAI*/MY*/Saved/Logs/HeroGame.log"
    for pre in filter(None, prefixes):
        hits = glob.glob(pre + "/pfx/drive_c/users/steamuser" + tail)
        if hits:
            return Path(hits[0])
    if inst not in ("", "0"):
        return None
    # The player's own game, started from Steam: Windows keeps the log in
    # %LOCALAPPDATA%, Linux Steam in the app's Proton prefix (compatdata).
    hits = []
    if os.environ.get("LOCALAPPDATA"):
        hits += glob.glob(os.environ["LOCALAPPDATA"].replace("\\", "/") + tail[len("/AppData/Local"):])
    for lib in steam_libraries():
        hits += glob.glob(f"{lib}/steamapps/compatdata/{STEAM_APPID}/pfx/drive_c/users/steamuser" + tail)
    hits = [h for h in hits if os.path.isfile(h)]
    return Path(max(hits, key=os.path.getmtime)) if hits else None


STEAM_APPID = 2362050


def steam_libraries():
    """Every Steam library folder on this Linux machine (native and Flatpak Steam)."""
    home = Path.home()
    roots = [home / ".steam/steam", home / ".local/share/Steam",
             home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    libs = []
    for root in roots:
        vdf = root / "steamapps/libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        libs.append(str(root))
        libs += re.findall(r'"path"\s+"([^"]+)"', text)
    seen, out = set(), []
    for lib in libs:
        real = os.path.realpath(lib)
        if real not in seen:
            seen.add(real)
            out.append(lib)
    return out


def wait_hook(timeout):
    """Until ajhook answers and its frame pump runs (native calls work)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = nm.call("F", timeout=5)
            if r.startswith("frames=") and not r.startswith("frames=0 "):
                return True
        except OSError:
            pass
        time.sleep(2)
    return False


def where():
    """"menu" (OutGame world: title screen), "hub" (st_MS01, the main-menu hub
    the title's Game Start opens), "stage" (a battle or its result screen) or
    "" (no player controller yet, or UE4SS not answering)."""
    out = probe('local p = FindFirstOf("PlayerController") '
                'out(p and p:IsValid() and p:GetFullName() or "-")')
    if "/Game/Stage/OutGame/" in out:
        return "menu"
    if "/Game/Stage/st_MS01/" in out:
        return "hub"
    return "stage" if "/Game/Stage/" in out else ""


def at_menu():
    return where() == "menu"


def wait_menu(timeout):
    """Until the main menu world is up and the save has loaded. The title
    screen loads the save by itself; travelling before that would run the
    match (and its end-of-match save) on top of an unloaded save."""
    path = game_log()
    if not path:
        log("game log not found - waiting 30 s instead of watching for the save load")
    t0 = time.time()
    while time.time() - t0 < timeout:
        loaded = True
        if path:
            try:
                loaded = SAVE_LOADED in path.read_text(errors="replace")
            except OSError:
                loaded = False
        elif time.time() - t0 < 30:
            loaded = False
        if loaded and at_menu():
            return True
        time.sleep(2)
    return False


HUB_READY = "OnEndBattleSequence = StartHud"


def go_hub(timeout):
    """Title -> hub, the way the title's Game Start does it (OpenLevel
    st_MS01). Not used: every battle started from a nextmatch travel after
    this keeps CameraBattle (lock-on sub-camera, empty CharacterList) at
    0,0,0, and neither filling the list nor re-pointing both local players'
    view targets moves it. Kept for further digging."""
    watch = LogWatch(game_log())
    try:
        log(nm.travel(nm.HUB))
    except (RuntimeError, OSError) as e:
        log(f"hub travel failed: {e}")
        return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(2)
        if HUB_READY in watch.new_text() and where() == "hub":
            return True
    return where() == "hub"


def begin(a):
    """Start a match and wait for it to come up. Returns (teams, stage) or None."""
    try:
        ids = lambda t: [int(x) for x in t.split(",")] if t else None
        # Read every match: the league swaps in a new challenger (and its
        # routes) between matches.
        names = nm.pool_from(a.pool_from) if a.pool_from else None
        teams, stage = nm.start(ids(a.p1), ids(a.p2), all_=a.all, log=log, mirror=a.mirror,
                                names=names,
                                stage=None if a.stage == "random" else a.stage)
    except (RuntimeError, OSError) as e:
        log(f"next match failed: {e}")
        return None
    t0 = time.time()
    time.sleep(5)
    while not (where() == "stage" and in_battle()):
        if time.time() - t0 > 120:
            log("new match did not come up in 120 s")
            break
        if a.speed != 1:
            nm.set_speed(a.speed)  # reaches the new level's WorldSettings once it is up
        time.sleep(2)
    time.sleep(2)
    if a.speed != 1 and not nm.set_speed(a.speed):
        log(f"could not set speed x{a.speed:g}")
    if not nm.set_free_mode():
        log("could not set FreeBattle mode (pause menu may open as Training)")
    return teams, stage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", type=int, default=0, help="stop after N new matches (0 = forever)")
    ap.add_argument("--all", action="store_true", help="random picks include Story/DLC menu entries")
    ap.add_argument("--p1", help="fixed team for player 1: menu ids, e.g. 24,4,2 (default random)")
    ap.add_argument("--p2", help="fixed team for player 2 (default random)")
    ap.add_argument("--pool-from", metavar="JSON",
                    help="random picks only from the characters in this mined_routes.json "
                         "(re-read each match; missing file = everyone)")
    ap.add_argument("--mirror", action="store_true",
                    help="player 2 gets player 1's team (random or --p1)")
    ap.add_argument("--stage", default=GYM,
                    help=f"stage for every match (default {GYM}, Gymnasium Gamma); "
                         f"'random' = any free-battle stage")
    ap.add_argument("--poll", type=float, default=2.0, help="seconds between end checks")
    ap.add_argument("--post", type=float, default=4.0,
                    help="seconds on the result screen before the next match")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="game speed multiplier (fixed 1/60 s steps, just less waiting; "
                         "the box manages about x3.6 at 1440p)")
    ap.add_argument("--log", default=str(HERE / "endless_matches.jsonl"))
    ap.add_argument("--boot-timeout", type=float, default=300.0,
                    help="how long to wait for the hook and the main menu after launch")
    ap.add_argument("--menu-settle", type=float, default=3.0,
                    help="seconds at the main menu before the first match starts")
    a = ap.parse_args()

    log("waiting for the game (ajhook)")
    if not wait_hook(a.boot_timeout):
        sys.exit(f"ajhook did not answer in {a.boot_timeout:.0f} s - is the game running?")
    started, n, fails = time.time(), 0, 0
    teams = stage = silent = None
    t0, place = time.time(), ""
    while not place and time.time() - t0 < a.boot_timeout:
        place = where() or ("stage" if in_battle() else "")
        if not place:
            time.sleep(2)
    if place == "menu":
        log("at the title - waiting for the save load")
        if not wait_menu(a.boot_timeout):
            sys.exit("title / save load not seen - stopping")
    if place in ("menu", "hub"):
        time.sleep(a.menu_settle)
        r = begin(a)
        if r:
            teams, stage = r
            n += 1
            started = time.time()
            log(f"match {n} running")
    path = game_log()
    if not path:
        sys.exit("game log (HeroGame.log) not found - set AJ_PREFIX")
    watch = LogWatch(path)
    log("waiting for a match to end")
    while not a.matches or n < a.matches:
        time.sleep(a.poll)
        text = watch.new_text()
        if not any(m in text for m in END_MARKS):
            if a.speed != 1 and teams:
                nm.set_speed(a.speed)  # cheap; covers anything resetting it
            if hook_alive():
                silent = None
            else:
                silent = silent or time.time()
                if time.time() - silent > 60:
                    sys.exit("game not answering for 60 s (closed or crashed?) - stopping")
            continue
        silent = None
        log("match over (result screen)")
        if teams:
            with open(a.log, "a") as f:
                f.write(json.dumps({"t": round(started), "ended": round(time.time()),
                                    "secs": round(time.time() - started), "p1": teams[0],
                                    "p2": teams[1], "stage": stage}) + "\n")
        time.sleep(a.post)
        r = begin(a)
        watch.new_text()  # skip whatever the old match logged meanwhile
        if not r:
            fails += 1
            if fails >= 5:
                sys.exit("giving up after 5 failures in a row")
            time.sleep(5)
            continue
        fails = 0
        teams, stage = r
        n += 1
        started = time.time()
        log(f"match {n} running")


if __name__ == "__main__":
    main()

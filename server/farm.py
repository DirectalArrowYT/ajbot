"""Run several headless game copies with lockstep bots, and keep them running.

    python3 server/farm.py --copies 2             # copies 0 and 1, forever
    python3 server/farm.py --copies 4 --speed 100 --python <venv>/bin/python

For each copy N it starts, in order:
  1. the game:      offline_game.sh with AJ_INSTANCE=N, AJ_VPADS=2, headless
  2. endless.py:    random gym matches for copy N        (endless[-N].log)
  3. play2.py:      the two lockstep bots for copy N      (bots[-N].log)
and then watches them. If the game exits (it crashes now and then with a bare
"Fatal error!"), its endless.py and bots are stopped and the whole copy is
started again. If only endless.py or the bots exit, just that piece is
restarted. Events go to farm.log (and stdout).

Stop with Ctrl-C or SIGTERM: every copy's game, endless.py and bots are
stopped. Copies start `--stagger` seconds apart so boots don't all load at
once. Run from the AJBot checkout (endless.py/play2.py/models live here).
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import nextmatch as nm

HERE = Path(__file__).resolve().parent.parent   # the repo root


def log(msg):
    line = time.strftime("[%H:%M:%S] ") + msg
    print(line, flush=True)
    with open(HERE / "farm.log", "a") as f:
        f.write(line + "\n")


def stop(proc, grace=15):
    """Stop a child and everything it started (each runs in its own group)."""
    if proc is None or proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            pass


LEAGUE = HERE / "league.json"
# (decision explore, defence epsilon): sparring learners explore hard for new
# data; the fair league plays close to the policy.
SPAR_EXPLORE = (0.25, 0.2)
FAIR_EXPLORE = (0.08, 0.12)
HUMAN = "models/human/policy.pkl"      # human-only fit the boss leans on


def league_mtime():
    try:
        return LEAGUE.stat().st_mtime
    except OSError:
        return 0.0


GAME_BITS = (b"offline_game", b"netbridge", b"AJGAME", b"proton", b"wine", b"pressure-vessel")


def sweep(n):
    """Kill whatever is left of game copy n. The launcher's bridges get
    reparented out of its process group, keep the copy's lock, and make
    every relaunch fail with "already running"."""
    tag, hit = f"AJ_INSTANCE={n}".encode(), 0
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            env = (d / "environ").read_bytes().split(b"\0")
            cmd = (d / "cmdline").read_bytes()
        except OSError:
            continue
        if tag in env and any(b in cmd for b in GAME_BITS):
            try:
                os.kill(int(d.name), signal.SIGKILL)
                hit += 1
            except OSError:
                pass
    return hit


XVFB_CLIENTS = 2048          # Xvfb's maximum -maxclients
XVFB_FD_LIMIT = 1800         # restart it past this many open fds (leaked clients)
XVFB_LOG_CAP = 20 << 20      # bytes of Xvfb output kept per start
HOOK_FAILS_RESTART = 3       # boots in a row with no hook answer -> suspect the display


def xvfb_pid(n):
    """pid of the Xvfb serving :n, or None."""
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv and argv[0].endswith(b"Xvfb") and f":{n}".encode() in argv[1:3]:
            return int(d.name)
    return None


def xvfb_fds(pid):
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return None


def _raise_nofile():
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 65536 if hard == resource.RLIM_INFINITY else min(65536, hard)
    if soft < want:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))


def start_xvfb(n):
    """A private X server for the headless games, shared by every farm that
    names the same display; left running when the farm stops.

    Its output goes through `head -c` so a spinning accept() error cannot fill
    the disk (it once wrote 43 GB), and `cat` keeps draining after the cap so
    Xvfb never blocks or gets SIGPIPE - with no tie to this process, since
    Xvfb outlives the farm."""
    sock = Path(f"/tmp/.X11-unix/X{n}")
    if sock.exists() and xvfb_pid(n):
        return
    logf = HERE / f"xvfb-{n}.log"
    cmd = (f"exec Xvfb :{n} -screen 0 1280x720x24 -nolisten tcp -maxclients {XVFB_CLIENTS} 2>&1 "
           f"| {{ head -c {XVFB_LOG_CAP} > {shlex.quote(str(logf))}; cat > /dev/null; }}")
    subprocess.Popen(["sh", "-c", cmd], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, preexec_fn=_raise_nofile)
    for _ in range(50):
        if sock.exists() and xvfb_pid(n):
            log(f"farm: Xvfb :{n} up ({XVFB_CLIENTS} clients, pid {xvfb_pid(n)})")
            return
        time.sleep(0.2)
    raise SystemExit(f"Xvfb :{n} did not start - see {logf}")


def kill_xvfb(n):
    """Xvfb ignores SIGTERM once wedged: SIGKILL, then clear its socket/lock."""
    pid = xvfb_pid(n)
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        for _ in range(50):
            if not Path(f"/proc/{pid}").exists():
                break
            time.sleep(0.1)
    for f in (Path(f"/tmp/.X11-unix/X{n}"), Path(f"/tmp/.X{n}-lock")):
        try:
            f.unlink()
        except OSError:
            pass


HOOK_FAILS = [0]             # hook timeouts in a row, over every copy


class Copy:
    def __init__(self, n, a):
        self.n, self.a = n, a
        self.sfx = f"-{n}" if n else ""
        self.port = 47380 + n
        self.game = self.endless = self.bots = None
        self.restarts = 0
        self.env = dict(os.environ, AJ_INSTANCE=str(n), AJHOOK_PORT=str(self.port),
                        AJ_PREFIX=a.prefix)
        self.env.pop("DISPLAY", None)    # the launcher sets DISPLAY/XAUTHORITY itself
        if a.xvfb is not None:
            # The desktop's X server allows 256 clients and each copy takes
            # ~30, so past ~5 copies games hang at boot. A private Xvfb has room.
            for k in ("WAYLAND_DISPLAY", "XAUTHORITY"):
                self.env.pop(k, None)
            self.env["DISPLAY"] = f":{a.xvfb}"

    def _spawn(self, argv, logname, cwd, env):
        out = open(HERE / logname, "ab")
        return subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)

    def hook_ready(self):
        try:
            r = nm.call("F", port=self.port, timeout=5)
            return r.startswith("frames=") and not r.startswith("frames=0 ")
        except (OSError, ValueError):
            return False

    def start_game(self):
        if (k := sweep(self.n)):
            log(f"copy {self.n}: killed {k} leftover game processes")
            time.sleep(2)
        env = dict(self.env, AJ_VPADS="2", AJ_GAME_ARGS=self.a.game_args)
        launcher = Path(self.a.launcher)
        self.game = self._spawn(["bash", str(launcher)], f"offline_game{self.sfx}.log",
                                launcher.parent, env)
        self.started = time.time()
        log(f"copy {self.n}: game starting (pid {self.game.pid}, port {self.port})")

    def start_endless(self):
        self.endless = self._spawn([sys.executable, str(HERE / "server" / "endless.py"),
                                    "--speed", str(self.a.speed),
                                    # Fair league copies mirror the teams, so
                                    # the score measures the policy, not the draw.
                                    *(["--mirror"] if self.n % 2 == 1 and not self.a.models else []),
                                    # ... and (--fair-pool) only pick characters
                                    # the challenger has mined combo routes for.
                                    *(["--pool-from", self.a.fair_pool]
                                      if self.a.fair_pool and self.n % 2 == 1 and not self.a.models
                                      else []),
                                    *shlex.split(self.a.endless_args)],
                                   f"endless{self.sfx}.log", HERE, self.env)
        log(f"copy {self.n}: endless.py started")

    def league_args(self):
        """Models and roles for this copy, from league.json (league.py).

        Odd copies are the FAIR league: the champion against the challenger
        when there is one (sides alternate), close to the policy - these
        matches decide promotion. Even copies SPAR: the learner (the
        challenger, else the champion) explores hard against a boss - the
        champion with the guard assist, leaning on the human-only policy - so
        the data shows what gets guarded and punished.

        --models A,B skips the league: model A on side A, B on side B, fair."""
        if self.a.models:
            a, b = self.a.models.split(",")
            return (["--model", a, "--model-b", b, "--explore", str(FAIR_EXPLORE[0]),
                     "--epsilon", str(FAIR_EXPLORE[1])], None)
        try:
            lg = json.loads(LEAGUE.read_text())
        except (OSError, ValueError):
            lg = {}
        champ = lg.get("champion") or "models/policy.pkl"
        chall = lg.get("challenger")
        if self.n % 2 == 1:
            if chall:
                a, b = (champ, chall) if self.n % 4 == 1 else (chall, champ)
                out = ["--model", a, "--model-b", b]
            else:
                out = ["--model", champ]
            explore = FAIR_EXPLORE
        else:
            learner = chall or champ
            boss = "B" if self.n % 4 == 0 else "A"
            a, b = (learner, champ) if boss == "B" else (champ, learner)
            out = ["--model", a, "--model-b", b, "--boss", boss]
            if (HERE / HUMAN).exists():
                out += ["--boss-lean", HUMAN]
            explore = SPAR_EXPLORE
        out += ["--explore", str(explore[0]), "--epsilon", str(explore[1])]
        return out, lg.get("gen")

    def start_bots(self):
        largs, gen = self.league_args()
        self.league_t = league_mtime()
        self.bots = self._spawn([self.a.python, str(HERE / "bot" / "play2.py"), "--lockstep",
                                 *largs, *self.a.bot_args], f"bots{self.sfx}.log", HERE, self.env)
        log(f"copy {self.n}: bots started ({' '.join(largs)}" + (f", gen {gen})" if gen else ")"))

    def endless_log_t(self):
        try:
            return (HERE / f"endless{self.sfx}.log").stat().st_mtime
        except OSError:
            return time.time()

    def stop_all(self):
        stop(self.bots)
        stop(self.endless)
        stop(self.game, grace=30)
        self.game = self.endless = self.bots = None
        if (k := sweep(self.n)):
            log(f"copy {self.n}: killed {k} leftover game processes")

    def step(self):
        """One watch pass: (re)start whatever this copy is missing."""
        now = time.time()
        if self.game is None or self.game.poll() is not None:
            if self.game is not None:
                self.restarts += 1
                log(f"copy {self.n}: game exited (code {self.game.returncode}) after "
                    f"{(now - self.started) / 60:.0f} min - restart #{self.restarts}")
                self.stop_all()
                time.sleep(self.a.cooldown)
            self.start_game()
            return
        if self.endless is None or self.bots is None:
            if not self.hook_ready():
                if now - self.started > self.a.boot_timeout:
                    log(f"copy {self.n}: hook never answered in {self.a.boot_timeout:.0f} s "
                        f"- killing the game")
                    HOOK_FAILS[0] += 1
                    stop(self.game, grace=30)
                return
        # A game that hangs after a result screen keeps its process (and hook)
        # alive while endless.py waits forever: kill it when endless's log
        # has been silent too long, and the next pass relaunches it.
        quiet = now - self.endless_log_t()
        if self.endless is not None and now - self.started > self.a.stall and quiet > self.a.stall:
            log(f"copy {self.n}: no match progress for {quiet / 60:.0f} min - killing the game")
            stop(self.game, grace=30)
            return
        HOOK_FAILS[0] = 0      # this copy's hook answered: the display works
        if self.endless is None:
            self.start_endless()
            self.bots_after = now + self.a.bots_delay
        elif self.endless.poll() is not None:
            log(f"copy {self.n}: endless.py exited (code {self.endless.returncode}) - restarting")
            self.start_endless()
        if self.bots is None:
            if now >= self.bots_after:
                self.start_bots()
        elif self.bots.poll() is not None:
            log(f"copy {self.n}: bots exited (code {self.bots.returncode}) - restarting")
            self.start_bots()
        elif not self.a.models and league_mtime() != self.league_t:
            log(f"copy {self.n}: league changed - restarting the bots on the new models")
            stop(self.bots)
            self.start_bots()


def xvfb_health(n, copies):
    """Restart a full or wedged Xvfb: leaked client sockets (games killed while
    connected) eventually take every slot, and then no game reaches the display."""
    pid = xvfb_pid(n)
    fds = xvfb_fds(pid) if pid else None
    why = None
    if pid is None:
        why = "not running"
    elif fds is not None and fds >= XVFB_FD_LIMIT:
        why = f"{fds} open fds (limit {XVFB_FD_LIMIT}, {XVFB_CLIENTS} clients max)"
    elif HOOK_FAILS[0] >= HOOK_FAILS_RESTART:
        why = f"{HOOK_FAILS[0]} boots in a row with no hook answer"
    if why is None:
        return
    log(f"farm: Xvfb :{n} {why} - restarting it and every copy")
    for c in copies:
        c.stop_all()
    kill_xvfb(n)
    start_xvfb(n)
    HOOK_FAILS[0] = 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--copies", type=int, default=2)
    ap.add_argument("--first", type=int, default=0, help="number of the first copy")
    ap.add_argument("--speed", type=float, default=100)
    from ajbot import local
    ap.add_argument("--launcher", default=local.get("game.launcher") or str(HERE / "server" / "offline_game.sh"))
    ap.add_argument("--prefix", default=local.get("game.prefix") or os.environ.get("AJ_PREFIX", ""),
                    help="main Proton prefix; copy N uses <prefix>-N")
    ap.add_argument("--game-args", default="-nullrhi -RenderOffScreen")
    ap.add_argument("--python", default=sys.executable, help="python for play2.py (the venv)")
    ap.add_argument("--stagger", type=float, default=45, help="seconds between copy starts")
    ap.add_argument("--bots-delay", type=float, default=30,
                    help="seconds after endless.py starts before the bots join")
    ap.add_argument("--cooldown", type=float, default=10, help="pause before a restart")
    ap.add_argument("--boot-timeout", type=float, default=300)
    ap.add_argument("--stall", type=float, default=600,
                    help="seconds without endless.py output before a game counts as hung")
    ap.add_argument("--xvfb", type=int, default=None, metavar="N",
                    help="run the games on a private Xvfb display :N (started if missing)"
                         " instead of the desktop's X server")
    ap.add_argument("--fair-pool", metavar="JSON", default=None,
                    help="fair copies pick only characters in this mined_routes.json, "
                         "e.g. models/challenger/mined_routes.json")
    ap.add_argument("--endless-args", default="",
                    help="extra endless.py arguments, e.g. \"--p1 24,4,2 --p2 24,4,2\"")
    ap.add_argument("--models", metavar="A,B",
                    help="fixed models for side A and side B instead of the league's")
    ap.add_argument("bot_args", nargs="*", help="extra play2.py arguments (after --)")
    a = ap.parse_args()

    if a.xvfb is not None:
        start_xvfb(a.xvfb)
    copies = [Copy(n, a) for n in range(a.first, a.first + a.copies)]
    stopping = False

    def on_signal(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    log(f"farm: copies {[c.n for c in copies]}, speed {a.speed}")
    try:
        for i, c in enumerate(copies):
            if i:
                t = time.time() + a.stagger
                while time.time() < t and not stopping:
                    for d in copies[:i]:
                        d.step()
                    time.sleep(2)
            if stopping:
                break
            c.step()
        next_check = 0.0
        while not stopping:
            if a.xvfb is not None and time.time() >= next_check:
                next_check = time.time() + 60
                xvfb_health(a.xvfb, copies)
            for c in copies:
                c.step()
            time.sleep(5)
    finally:
        log("farm: stopping every copy")
        for c in copies:
            c.stop_all()


if __name__ == "__main__":
    main()

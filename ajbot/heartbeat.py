"""A periodic "[rec] ..." status line proving data is being written.

The GUI shows the newest one as its recording indicator and flags it when the
tick count stops rising, so a recorder that silently stalls (no battle Lua, a
blind roster, a closed file) is visible instead of discovered after a session.

    [rec] LIVE match 2 | 1234 ticks (+38 in 2s) | runs/1789.jsonl | Bakugo vs Nomu
    [rec] WAITING for a match | recorded 3 match(es) so far
    [rec] NO BATTLE (menus or paused) | recorded 3 match(es) so far
    [rec] OFF (--no-record) | playing match 2
"""
import time


class Heartbeat:
    def __init__(self, every=2.0, enabled=True):
        self.every = every
        self.enabled = enabled
        self.ticks = 0              # ticks written in the current match
        self.total = 0
        self._last_print = 0.0
        self._last_ticks = 0
        self._last_t = time.monotonic()

    def wrote(self):
        self.ticks += 1
        self.total += 1

    def new_match(self):
        self.ticks = 0
        self._last_ticks = 0

    def _due(self):
        now = time.monotonic()
        if now - self._last_print < self.every:
            return None
        dt = now - self._last_t
        self._last_print = self._last_t = now
        return dt

    def live(self, match, path, me=None, enemy=None):
        dt = self._due()
        if dt is None:
            return
        if not self.enabled or path is None:
            print(f"[rec] OFF (--no-record) | playing match {match}", flush=True)
            return
        gained = self.ticks - self._last_ticks
        self._last_ticks = self.ticks
        who = f" | {_short(me)} vs {_short(enemy)}" if me or enemy else ""
        print(f"[rec] LIVE match {match} | {self.ticks} ticks (+{gained} in {dt:.0f}s) "
              f"| {path}{who}", flush=True)

    def idle(self, why, matches):
        if self._due() is None:
            return
        print(f"[rec] {why} | recorded {matches} match(es) so far", flush=True)


def wait_for_battle(hook, stop_file=None, every=2.0):
    """Block until the battle Lua is running. False if a stop was requested.

    The scripts used to exit with "no lua state" when started from menus or
    character select - which is exactly when the GUI starts them.
    """
    from pathlib import Path
    last = 0.0
    while not hook.has_state():
        if stop_file and Path(stop_file).exists():
            try:
                Path(stop_file).unlink()
            except OSError:
                pass
            return False
        now = time.monotonic()
        if now - last >= every:
            last = now
            print("[rec] NO BATTLE (menus, loading or paused) | "
                  "waiting for a match to start", flush=True)
        time.sleep(0.25)
    return True


def _short(cid):
    if isinstance(cid, str) and cid.startswith("Pl"):
        return cid[2:].lstrip("0123456789") or cid
    return cid or "?"

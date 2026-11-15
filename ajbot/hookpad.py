"""Bot pads served from inside the game by ajhook (no ViGEm device).

On Windows the bot's ViGEm pad goes through Steam Input before the game sees
it, and Steam can hide it: Windows lists the pad, the game never does, and
the bot drives nothing (2026-11-10). ajhook already answers the game's own
XInputGetState (ajbot.lockstep, for the farm), so the bot's pad can live
there instead: Steam never sees it, and it sits on the first XInput slot
the real controllers leave free - Player 2 behind a human on slot 0, or
Player 1 with no controller connected.

    pads = make_pads(1, hook)        # HookPad on a free slot, or None
    pads[0].hold(NATTACK); pads[0].tick(2)

A HookPad keeps Pad's interface and real-time frame clock; only the
transport differs. ajhook sends neutral if no state arrives for 2 s, so a
killed bot never leaves a button held. The slot stays connected until the
game closes (or `release_pads`), and the next run reuses it.
"""
import struct
import time

from .hook import HookError
from .pad import NEUTRAL, Pad

KEEPALIVE_S = 0.5      # resend an unchanged state this often (hook times out at 2 s)
MAX_SLOTS = 4


def _axis(v):
    v = max(-1.0, min(1.0, float(v)))
    return int(round(v * 32767)) if v >= 0 else int(round(v * 32768))


def _trig(v):
    return int(round(max(0.0, min(1.0, float(v))) * 255))


def _parse_n(reply):
    """'vpads=1 base=1' -> (1, 1); 'vpads=2' -> (2, 0)."""
    n, b = 0, 0
    for tok in reply.split():
        k, _, v = tok.partition("=")
        if k == "vpads":
            n = int(v)
        elif k == "base":
            b = int(v)
    return n, b


def _game_slots(hook):
    r = hook._send("G", "").strip()
    if r.startswith("!"):
        raise HookError(r)
    return {int(t) for t in r.split()}


def make_pads(n, hook, log=print):
    """`n` in-game pads on consecutive free XInput slots, or None when this
    hook cannot serve them (an older ajhook.asi, or the XInput import not
    found) - the caller falls back to ViGEm pads."""
    if hook is None:
        return None
    try:
        r = hook._send("N", "")
        if r.startswith("!"):
            return None
        cur, base = _parse_n(r)
        if cur != n:
            # Any virtual pads from an earlier run go first, so the free-slot
            # search below sees only the real controllers.
            if cur:
                hook._send("N", "0")
            real = _game_slots(hook)
            base = next((b for b in range(MAX_SLOTS - n + 1)
                         if not any(s in real for s in range(b, b + n))), None)
            if base is None:
                log(f"  !! no {n} free XInput slot(s) left (real controllers on {sorted(real)})")
                return None
            r = hook._send("N", f"{n}@{base}")
            if _parse_n(r) != (n, base):
                log(f"  !! ajhook did not take the virtual pads: {r}")
                return None
        else:
            r = hook._send("N", f"{n}@{base}")      # resets them to neutral, re-announces
    except HookError as e:
        log(f"  !! in-game pads unavailable ({e})")
        return None
    pads = [HookPad(hook, i, base + i) for i in range(n)]
    # UE re-polls XInput after the device-change message; give it a moment
    # so the first presses are not lost.
    time.sleep(0.3)
    for p in pads:
        p._flush()
    return pads


def release_pads(hook):
    """Disconnect the virtual slots (they read neutral anyway once idle)."""
    try:
        hook._send("N", "0")
    except HookError:
        pass


class HookPad(Pad):
    """Pad whose state is answered by ajhook inside the game."""

    def __init__(self, hook, index, slot):  # noqa - Pad.__init__ makes a device
        self.hook = hook
        self.index = index
        self._slot = slot
        self._buttons = set()
        self._stick = NEUTRAL
        self._rstick = NEUTRAL
        self._lt = self._rt = 0.0
        self.frame = 0
        self._next = time.perf_counter()
        self._sent = None
        self._sent_at = 0.0
        self.errors = 0

    @property
    def slot(self):
        return self._slot

    def hidden_by_steam(self):
        return False

    def pack(self):
        b = 0
        for x in self._buttons:
            b |= int(x)
        lx, ly = self._stick
        rx, ry = self._rstick
        return struct.pack("<HBBhhhh", b & 0xFFFF, _trig(self._lt), _trig(self._rt),
                           _axis(lx), _axis(ly), _axis(rx), _axis(ry)).hex()

    def _flush(self):
        s = self.pack()
        now = time.perf_counter()
        if s == self._sent and now - self._sent_at < KEEPALIVE_S:
            return
        try:
            self.hook._send("Z", f"pad {self.index} {s}")
            self._sent, self._sent_at = s, now
        except HookError:
            # The game closed or the pipe hiccuped: the next frame retries.
            self.errors += 1
            self._sent = None

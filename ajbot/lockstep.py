"""Lockstep pads: the bots press buttons through ajhook's virtual XInput
slots and the game only advances when they step it.

    link = Link(hook)                 # also switches clock.now() to game time
    pads = [link.pad(0), link.pad(1)] # Player 1, Player 2
    pads[0].hold(NATTACK); tick_all(pads, 2)   # = 2 game frames, no sleeping

A LockstepPad keeps Pad's interface (hold/release/stick/trigger/tick/do/...),
so play2/agent code works unchanged. tick()/tick_all() send one "Z<frames>
<pad0> <pad1>" request: ajhook sets both virtual pads, lets the game run that
many frames and answers when the game thread is parked again. How fast that
goes is only up to the game thread and the bot - at engine speed xN
(nextmatch.set_speed) the engine never sleeps between frames.

The game must expose the virtual slots: launch with AJHOOK_VPADS=2 (the
offline launcher's AJ_VPADS=2), or Link turns them on at run time with N2.
If the bot stops stepping for 3 s, ajhook lets the game run freely again.
"""
import struct

from . import clock
from .hook import HookError
from .pad import NEUTRAL, Pad

FPS = 60.0


def _axis(v):
    v = max(-1.0, min(1.0, float(v)))
    return int(round(v * 32767)) if v >= 0 else int(round(v * 32768))


def _trig(v):
    return int(round(max(0.0, min(1.0, float(v))) * 255))


class Link:
    """One gate for all lockstep pads (the game has one frame clock)."""

    def __init__(self, hook, pads=2):
        self.hook = hook
        self.pads = []
        self.frame = 0
        self.timeouts = 0
        r = hook._send("N", "")
        if r != f"vpads={pads}":
            r = hook._send("N", str(pads))
        if r != f"vpads={pads}":
            raise HookError(f"could not enable {pads} virtual pads: {r}")
        self.frame = self._sync()
        clock.use_frames(lambda: self.frame)

    def pad(self, slot):
        p = LockstepPad(self, slot)
        self.pads.append(p)
        self.pads.sort(key=lambda q: q.slot)
        return p

    def _state(self, slot):
        for p in self.pads:
            if p.slot == slot:
                return p.pack()
        return "0" * 24

    def step(self, frames):
        """Apply every pad's current state and run `frames` game frames."""
        n = max((p.slot for p in self.pads), default=-1) + 1
        states = " ".join(self._state(i) for i in range(n))
        try:
            r = self.hook._send("Z", f"{max(0, int(frames))} {states}")
        except HookError as e:
            # "timeout": a map load holds the game thread for seconds; the
            # frames still run once it is back. Anything else (gate released
            # because we were too slow) re-arms on the next step.
            self.timeouts += 1
            r = str(e)
        self.frame = _frame_of(r, self.frame)
        for p in self.pads:
            p.frame += frames
        return self.frame

    def _sync(self):
        return _frame_of(self.hook._send("Z", "?"), 0)

    def release(self):
        """Let the game run freely again (pads go neutral)."""
        try:
            self.hook._send("Z", "set " + " ".join("0" * 24 for _ in self.pads))
            self.hook._send("Z", "off")
        except HookError:
            pass
        clock.use_frames(None)


def _frame_of(reply, default):
    for tok in reply.split():
        if tok.startswith("frame="):
            try:
                return int(tok[6:])
            except ValueError:
                break
    return default


class LockstepPad(Pad):
    """Pad whose state lives in ajhook and whose frames are game frames."""

    def __init__(self, link, slot):  # noqa - Pad.__init__ makes a real device
        self.link = link
        self._slot = slot
        self._buttons = set()
        self._stick = NEUTRAL
        self._rstick = NEUTRAL
        self._lt = self._rt = 0.0
        self.frame = 0
        self._next = 0.0

    @property
    def slot(self):
        return self._slot

    def pack(self):
        b = 0
        for x in self._buttons:
            b |= int(x)
        lx, ly = self._stick
        rx, ry = self._rstick
        return struct.pack("<HBBhhhh", b & 0xFFFF, _trig(self._lt), _trig(self._rt),
                           _axis(lx), _axis(ly), _axis(rx), _axis(ry)).hex()

    def _flush(self):
        pass            # state is sent with the next step

    def tick(self, frames=1):
        if frames > 0:
            self.link.step(frames)

    def reset(self):
        self.clear()

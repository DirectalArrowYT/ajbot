"""The combo trainer's gate (practice.py --trainer).

Before each step the game is held on the frame the bot would press it, an
overlay on the game window shows the input, and the step goes in only once
the person presses it on their own controller - read from inside the game by
ajhook's Zreal, so a Steam Input pad counts. The bot then presses it on that
exact frame: the person goes at their own pace, and between prompts the
combo plays in real time. Back on the pad quits.
"""
import os
import time

from ajbot.hook import HookError
from ajbot.overlay import Overlay, arrow

BUTTON_BITS = [(0x1000, "A"), (0x2000, "B"), (0x4000, "X"), (0x8000, "Y"), (0x0100, "LB"),
               (0x0200, "RB"), (0x0040, "LS"), (0x0080, "RS"), (0x0010, "Start"),
               (0x0001, "Up"), (0x0002, "Down"), (0x0004, "Left"), (0x0008, "Right")]
BACK_BIT = 0x0020
NOTE = "Back on the pad: quit"


def chip(step):
    """Short text for one route step on the overlay."""
    src = (step.get("src") or step["label"]).strip()
    if step["label"] == "Melee" and set(src.upper()) == {"A"}:
        return "A"
    if step["label"] == "Tag":
        return f"Tag {step.get('tag') or ''}".strip()
    return src


def names_of(bits):
    return [n for bit, n in BUTTON_BITS if bits & bit]


class HumanGate:
    def __init__(self, g, route, stop_file=None):
        self.g = g
        self.stop_file = stop_file          # the GUI's Stop, honoured while the game is held
        self.chain = [chip(s) for s in route["steps"]]
        self.quit = False
        if self.read() is None:
            raise RuntimeError("the game's hook cannot read your controller (Zreal) - "
                               "install the current ajhook.asi and restart the game")
        self.ov = Overlay()

    def read(self):
        """(buttons, lt, rt, stick x, stick y) over every real pad, or None."""
        try:
            r = self.g.hook._send("Z", "real")
        except HookError:
            return None
        if r.startswith("!"):
            return None
        btn, lt, rt, sx, sy = 0, 0, 0, 0.0, 0.0
        for tok in r.split():
            if ":" not in tok:
                continue
            b = bytes.fromhex(tok.split(":", 1)[1])
            x = int.from_bytes(b[4:6], "little", signed=True) / 32767
            y = int.from_bytes(b[6:8], "little", signed=True) / 32767
            btn |= int.from_bytes(b[0:2], "little")
            lt, rt = max(lt, b[2]), max(rt, b[3])
            if abs(x) + abs(y) > abs(sx) + abs(sy):
                sx, sy = x, y
        return btn, lt, rt, sx, sy

    def running(self, k, note=NOTE):
        self.ov.show(chain=self.chain, k=k, note=note)

    def result(self, ok, r):
        if ok:
            self.ov.show(chain=self.chain, k=len(self.chain), banner="LANDED", good=True, note=NOTE)
            return
        k = r.get("fail_at")
        at = self.chain[k] if k is not None and k < len(self.chain) else "the end"
        self.ov.show(chain=self.chain, k=-1 if k is None else k, banner=f"Dropped at {at}",
                     good=False, note=r.get("why") or NOTE)

    def wait(self, k, buttons, stick, trig):
        """Hold the game until the person presses step k's input. False = quit."""
        need = 0
        for b in buttons:
            need |= int(b)
        names = names_of(need) + (["LT" if trig == "left" else "RT"] if trig else [])
        want = arrow(stick)
        shown = stick if want else None

        def prompt(wrong=None):
            self.ov.show(chain=self.chain, k=k, prompt=self.chain[k], buttons=names,
                         stick=shown, wrong=wrong, note=NOTE)
        prompt()
        cur = self.read() or (0, 0, 0, 0.0, 0.0)
        down = cur[0]                         # held when the prompt came up: release first
        tdown = (cur[1] > 128, cur[2] > 128)
        keep = time.time()
        while True:
            if time.time() - keep > 1.0:
                self.g.link.step(0)           # keeps the gate holding this frame
                keep = time.time()
                if self.stop_file and os.path.exists(self.stop_file):
                    self.quit = True
                    self.ov.clear()
                    return False
            st = self.read()
            if st is None:
                time.sleep(0.05)
                continue
            btn, lt, rt, sx, sy = st
            if btn & BACK_BIT:
                self.quit = True
                self.ov.clear()
                return False
            new = btn & ~down
            tl, tr = lt > 128, rt > 128
            if trig:
                fresh = (tl and not tdown[0]) if trig == "left" else (tr and not tdown[1])
                ok = fresh and (btn & need) == need
            else:
                ok = bool(new & need) and (btn & need) == need
            if ok:
                if want and arrow((sx, sy)) != want:
                    prompt("Hold the stick that way too")
                else:
                    self.running(k + 1)
                    return True
            elif new & ~need & ~BACK_BIT:
                prompt("That was " + " + ".join(names_of(new & ~need)))
            down, tdown = btn, (tl, tr)
            time.sleep(0.004)

    def close(self):
        self.ov.close()

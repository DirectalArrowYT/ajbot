"""The combo trainer's gate (practice.py --trainer).

Before each step the game is held on the frame the bot would press it, an
overlay on the game window shows the input, and the step goes in only once
the person presses it - on their own controller (read from inside the game by
ajhook's Zreal, so a Steam Input pad counts) or on the keyboard, through the
keys the game's own options assign to each action. The bot then presses it
on that exact frame: the person goes at their own pace, and between prompts
the combo plays in real time. Back on the pad or Backspace quits.

While it runs the game gets no keyboard messages (ajhook Zkeys mute), so a
key pressed for a prompt is not played a second time by the keyboard player.
"""
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

from ajbot.hook import HookError
from ajbot.overlay import Overlay, arrow, game_window

BUTTON_BITS = [(0x1000, "A"), (0x2000, "B"), (0x4000, "X"), (0x8000, "Y"), (0x0100, "LB"),
               (0x0200, "RB"), (0x0040, "LS"), (0x0080, "RS"), (0x0010, "Start"),
               (0x0001, "Up"), (0x0002, "Down"), (0x0004, "Left"), (0x0008, "Right")]
BACK_BIT = 0x0020
NOTE = "Back on the pad: quit"
KEY_NOTE = "Backspace: quit"
VK_BACK = 0x08

# EConfigChangeableAction -> the game's default key, for when the options
# cannot be read (they live in SaveData.ConfigSaveData.Controllers: preset 0 is
# the keyboard, 1 the first controller).
DEFAULT_KEYS = {1: "W", 2: "S", 3: "A", 4: "D", 5: "J", 6: "K", 7: "L", 8: "SpaceBar",
                9: "LeftShift", 10: "F", 11: "Q", 12: "E", 22: "MiddleMouseButton", 23: "Z"}
DEFAULT_PAD = {1: "Gamepad_LeftStick_Up", 2: "Gamepad_LeftStick_Down",
               3: "Gamepad_LeftStick_Left", 4: "Gamepad_LeftStick_Right",
               5: "Gamepad_FaceButton_Left", 6: "Gamepad_FaceButton_Top",
               7: "Gamepad_FaceButton_Right", 8: "Gamepad_FaceButton_Bottom",
               9: "Gamepad_LeftShoulder", 10: "Gamepad_RightShoulder",
               11: "Gamepad_LeftTrigger", 12: "Gamepad_RightTrigger",
               22: "Gamepad_LeftThumbstick", 23: "Gamepad_RightThumbstick"}
# What the bot's pad presses, by the Unreal key name of the default layout.
PAD_INPUT = {"Gamepad_FaceButton_Bottom": ("btn", 0x1000), "Gamepad_FaceButton_Right": ("btn", 0x2000),
             "Gamepad_FaceButton_Left": ("btn", 0x4000), "Gamepad_FaceButton_Top": ("btn", 0x8000),
             "Gamepad_LeftShoulder": ("btn", 0x0100), "Gamepad_RightShoulder": ("btn", 0x0200),
             "Gamepad_LeftThumbstick": ("btn", 0x0040), "Gamepad_RightThumbstick": ("btn", 0x0080),
             "Gamepad_LeftTrigger": ("lt", 0), "Gamepad_RightTrigger": ("rt", 0),
             "Gamepad_LeftStick_Up": ("y", 1), "Gamepad_LeftStick_Down": ("y", -1),
             "Gamepad_LeftStick_Left": ("x", -1), "Gamepad_LeftStick_Right": ("x", 1)}

READ_BINDINGS = '''
local s = FindFirstOf("SaveData")
local c = s.ConfigSaveData.Controllers
for i, tag in ipairs({"kb", "pad"}) do
  c[i].ActionMapper:ForEach(function(k, v)
    out(tag .. " " .. tostring(k:get()) .. " " .. v:get().KeyName:ToString())
  end)
end
'''

# Unreal key name -> (virtual-key code, short label).
_NAMED = {"SpaceBar": (0x20, "Space"), "LeftShift": (0xA0, "Shift"), "RightShift": (0xA1, "R Shift"),
          "LeftControl": (0xA2, "Ctrl"), "RightControl": (0xA3, "R Ctrl"), "LeftAlt": (0xA4, "Alt"),
          "RightAlt": (0xA5, "R Alt"), "Enter": (0x0D, "Enter"), "Tab": (0x09, "Tab"),
          "CapsLock": (0x14, "Caps"), "Up": (0x26, "↑"), "Down": (0x28, "↓"), "Left": (0x25, "←"),
          "Right": (0x27, "→"), "LeftMouseButton": (0x01, "LMB"), "RightMouseButton": (0x02, "RMB"),
          "MiddleMouseButton": (0x04, "MMB"), "ThumbMouseButton": (0x05, "M4"),
          "ThumbMouseButton2": (0x06, "M5"), "Semicolon": (0xBA, ";"), "Comma": (0xBC, ","),
          "Period": (0xBE, "."), "Slash": (0xBF, "/"), "Tilde": (0xC0, "`"),
          "LeftBracket": (0xDB, "["), "RightBracket": (0xDD, "]"), "Apostrophe": (0xDE, "'"),
          "Hyphen": (0xBD, "-"), "Equals": (0xBB, "="), "Backslash": (0xDC, "\\"),
          "Insert": (0x2D, "Ins"), "Delete": (0x2E, "Del"), "Home": (0x24, "Home"),
          "End": (0x23, "End"), "PageUp": (0x21, "PgUp"), "PageDown": (0x22, "PgDn")}
_DIGITS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]


def vk_of(name):
    """(virtual-key code, label) for an Unreal key name, or None."""
    if name in _NAMED:
        return _NAMED[name]
    if len(name) == 1 and name.isalnum():
        return ord(name.upper()), name.upper()
    if name in _DIGITS:
        return 0x30 + _DIGITS.index(name), str(_DIGITS.index(name))
    if name.startswith("NumPad") and name[6:] in _DIGITS:
        return 0x60 + _DIGITS.index(name[6:]), "Num" + str(_DIGITS.index(name[6:]))
    if name[:1] == "F" and name[1:].isdigit() and 1 <= int(name[1:]) <= 12:
        return 0x6F + int(name[1:]), name
    return None


def read_bindings():
    """({action: key name} for the keyboard, the same for the first
    controller, read from the game?) - the defaults where reading fails."""
    kb, pad = dict(DEFAULT_KEYS), dict(DEFAULT_PAD)
    probe = Path(__file__).resolve().parents[1] / "server" / "ueprobe.py"
    try:
        r = subprocess.run([sys.executable, str(probe), READ_BINDINGS],
                           capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return kb, pad, False
    got = False
    for line in r.splitlines():
        p = line.split()
        if len(p) == 3 and p[0] in ("kb", "pad") and p[1].isdigit():
            (kb if p[0] == "kb" else pad)[int(p[1])] = p[2]
            got = True
    return kb, pad, got


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


class Keyboard:
    """The keyboard, read as the bot's pad would see it through the game's
    key assignments - only while the game window has the focus."""

    def __init__(self):
        kb, pad, self.live = read_bindings()
        self.keys = []                  # (vk, kind, value)
        self.label = {}                 # ("btn", bit) / ("lt",) / ("x", 1) ... -> key label
        for action, pkey in pad.items():
            what = PAD_INPUT.get(pkey)
            k = vk_of(kb.get(action, "None"))
            if not what or not k:
                continue
            self.keys.append((k[0], *what))
            self.label.setdefault(what if what[0] in ("btn", "x", "y") else (what[0],), k[1])
        self.u32 = ctypes.windll.user32
        self.hwnd = None
        self.used = False

    def focused(self):
        fg = self.u32.GetForegroundWindow()
        if not self.hwnd or fg != self.hwnd:
            self.hwnd = game_window()
        return bool(fg) and fg == self.hwnd

    def down(self, vk):
        return bool(self.u32.GetAsyncKeyState(vk) & 0x8000)

    def read(self):
        """(buttons, lt, rt, x, y, quit) from the keyboard."""
        if not self.focused():
            return 0, 0, 0, 0.0, 0.0, False
        btn, lt, rt, x, y = 0, 0, 0, 0.0, 0.0
        for vk, kind, val in self.keys:
            if not self.down(vk):
                continue
            self.used = True
            if kind == "btn":
                btn |= val
            elif kind == "lt":
                lt = 255
            elif kind == "rt":
                rt = 255
            elif kind == "x":
                x += val
            else:
                y += val
        quit_ = self.down(VK_BACK)
        return btn, lt, rt, max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y)), quit_

    def names(self, bits, trig):
        out = [self.label.get(("btn", b), n) for b, n in BUTTON_BITS if bits & b]
        if trig:
            left = trig == "left"
            out.append(self.label.get(("lt",) if left else ("rt",), "LT" if left else "RT"))
        return out

    def stick_keys(self, stick):
        """Key labels for a stick direction: e.g. ['W', 'D']."""
        if not arrow(stick):
            return []
        sx, sy = stick
        out = []
        if sy > 0.38:
            out.append(self.label.get(("y", 1), "Up"))
        if sy < -0.38:
            out.append(self.label.get(("y", -1), "Down"))
        if sx < -0.38:
            out.append(self.label.get(("x", -1), "Left"))
        if sx > 0.38:
            out.append(self.label.get(("x", 1), "Right"))
        return out


class HumanGate:
    def __init__(self, g, route, stop_file=None):
        self.g = g
        self.stop_file = stop_file          # the GUI's Stop, honoured while the game is held
        self.chain = [chip(s) for s in route["steps"]]
        self.quit = False
        self.pad_seen = False
        if self._pads() is None:
            raise RuntimeError("the game's hook cannot read your controller (Zreal) - "
                               "install the current ajhook.asi and restart the game")
        self.kb = Keyboard()
        if not self.kb.live:
            print("[trainer] could not read the game's key options - using the default keys",
                  flush=True)
        try:
            r = self.g.hook._send("Z", "keys mute")
        except HookError:
            r = "!"
        if not r.startswith("keys"):
            print("[trainer] this ajhook.asi cannot hold back the keyboard - keys pressed for a "
                  "prompt may also reach the game", flush=True)
        self.ov = Overlay()

    def _pads(self):
        """(buttons, lt, rt, stick x, stick y) over every real pad, all zero
        when none is connected; None when the hook has no Zreal."""
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
            self.pad_seen = True
            b = bytes.fromhex(tok.split(":", 1)[1])
            x = int.from_bytes(b[4:6], "little", signed=True) / 32767
            y = int.from_bytes(b[6:8], "little", signed=True) / 32767
            btn |= int.from_bytes(b[0:2], "little")
            lt, rt = max(lt, b[2]), max(rt, b[3])
            if abs(x) + abs(y) > abs(sx) + abs(sy):
                sx, sy = x, y
        return btn, lt, rt, sx, sy

    def read(self):
        """Pads and keyboard together: (buttons, lt, rt, x, y), or None."""
        p = self._pads()
        if p is None:
            return None
        kb, klt, krt, kx, ky, quit_ = self.kb.read()
        btn = p[0] | kb | (BACK_BIT if quit_ else 0)
        sx, sy = (kx, ky) if abs(kx) + abs(ky) > abs(p[3]) + abs(p[4]) else (p[3], p[4])
        return btn, max(p[1], klt), max(p[2], krt), sx, sy

    def keys_mode(self):
        """Show keyboard keys: no pad connected, or the keyboard is in use."""
        return self.kb.used or not self.pad_seen

    def note(self):
        return KEY_NOTE if self.keys_mode() else NOTE

    def running(self, k):
        self.ov.show(chain=self.chain, k=k, note=self.note())

    def result(self, ok, r):
        if ok:
            self.ov.show(chain=self.chain, k=len(self.chain), banner="LANDED", good=True,
                         note=self.note())
            return
        k = r.get("fail_at")
        at = self.chain[k] if k is not None and k < len(self.chain) else "the end"
        self.ov.show(chain=self.chain, k=-1 if k is None else k, banner=f"Dropped at {at}",
                     good=False, note=r.get("why") or self.note())

    def wait(self, k, buttons, stick, trig):
        """Hold the game until the person presses step k's input. False = quit."""
        need = 0
        for b in buttons:
            need |= int(b)
        want = arrow(stick)

        def prompt(wrong=None):
            if self.keys_mode():
                self.ov.show(chain=self.chain, k=k, prompt=self.chain[k], keys=True,
                             buttons=self.kb.stick_keys(stick) + self.kb.names(need, trig),
                             wrong=wrong, note=KEY_NOTE)
            else:
                names = names_of(need) + (["LT" if trig == "left" else "RT"] if trig else [])
                self.ov.show(chain=self.chain, k=k, prompt=self.chain[k], buttons=names,
                             stick=stick if want else None, wrong=wrong, note=NOTE)
        cur = self.read() or (0, 0, 0, 0.0, 0.0)
        mode = self.keys_mode()
        prompt()
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
            if self.keys_mode() != mode:      # first key press: show keys from now on
                mode = self.keys_mode()
                prompt()
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
                    prompt("Hold that direction too")
                else:
                    self.running(k + 1)
                    return True
            elif new & ~need & ~BACK_BIT:
                wrong = (self.kb.names(new & ~need, None) if self.keys_mode()
                         else names_of(new & ~need))
                prompt("That was " + " + ".join(wrong))
            down, tdown = btn, (tl, tr)
            time.sleep(0.004)

    def close(self):
        try:
            self.g.hook._send("Z", "keys pass")
        except HookError:
            pass
        self.ov.close()

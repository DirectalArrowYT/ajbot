"""Virtual gamepad driver for MY HERO ACADEMIA: All's Justice.

Button map extracted from the game's own KeyConfig_SwitchSide.json (TypeA layout).
Action names follow Braden's combo notation.
"""
import ctypes
import ctypes.wintypes as _w
import sys
import time

try:
    import vgamepad as vg
except ImportError:          # Linux, or a training-only machine: the button
    vg = None                # constants below still import; on Linux Pad()
                             # drives a uinput pad instead (see _UInputX360)


class _XGamepad(ctypes.Structure):
    _fields_ = [("wButtons", _w.WORD), ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class _XState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", _w.DWORD), ("Gamepad", _XGamepad)]


def _load_xinput():
    if not hasattr(ctypes, "windll"):
        return None          # not Windows (the Linux training server)
    for dll in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return ctypes.windll.LoadLibrary(dll)
        except OSError:
            continue
    return None


_XINPUT = _load_xinput()


def _xinput_connected():
    """Set of occupied XInput slots (0 == Player 1).

    The game's own view when this process already has an ajhook connection:
    a Steam Controller (or anything else run through Steam Input) is handed
    to the game inside its process only, so Windows' XInput lists 0 slots
    while the game has it on Player 1. Read from Windows, the bot's pad then
    looked like Player 1 and Human vs Bot drove the wrong panel (2026-11-09).
    """
    if _XINPUT is None:
        if sys.platform != "win32":
            return _hook_xinput_slots()
        return set()
    game = _registered_hook_slots()
    if game is not None:
        return game
    st = _XState()
    return {i for i in range(4)
            if _XINPUT.XInputGetState(i, ctypes.byref(st)) == 0}


def _registered_hook_slots():
    """The game's XInput slots through the hook connection the process
    already registered (menus.use_hook), or None. Never opens one: this is
    polled, and a connect attempt with the game closed costs a timeout."""
    try:
        from . import menus
        h = menus._HOOK
        return h.xinput_slots() if h is not None else None
    except Exception:
        return None


def _hook_xinput_slots():
    """Linux: the game runs under Proton, so only its own (Wine) XInput knows
    which slots are taken. ajhook answers that with the G opcode. Empty when
    the hook is not reachable, which leaves Pad.slot at -1 (unknown) and side
    detection to menus.detect_side()."""
    try:
        from .menus import _try_hook
        h = _try_hook()
        return h.xinput_slots() if h is not None else set()
    except Exception:
        return set()


class _UInputX360:
    """The slice of vgamepad.VX360Gamepad that Pad uses, as a Linux uinput
    device that looks exactly like the kernel xpad driver's Xbox 360 pad
    (same name, USB ids, buttons and axis ranges). Proton's SDL backend
    recognises it by those ids and hands it to the game as an XInput pad.
    Needs write access to /dev/uinput (the uaccess ACL Steam's udev rules
    give the logged-in user is enough)."""

    _BTN = None      # XUSB bit -> evdev key code, filled on first use
    _HAT = {0x0001: ("y", -1), 0x0002: ("y", 1), 0x0004: ("x", -1), 0x0008: ("x", 1)}

    def __init__(self):
        from evdev import UInput, AbsInfo, ecodes as e
        self._e = e
        if _UInputX360._BTN is None:
            _UInputX360._BTN = {
                0x1000: e.BTN_A, 0x2000: e.BTN_B, 0x4000: e.BTN_X, 0x8000: e.BTN_Y,
                0x0100: e.BTN_TL, 0x0200: e.BTN_TR, 0x0020: e.BTN_SELECT,
                0x0010: e.BTN_START, 0x0400: e.BTN_MODE,
                0x0040: e.BTN_THUMBL, 0x0080: e.BTN_THUMBR,
            }
        stick = AbsInfo(0, -32768, 32767, 16, 128, 0)
        trig = AbsInfo(0, 0, 255, 0, 0, 0)
        hat = AbsInfo(0, -1, 1, 0, 0, 0)
        caps = {
            e.EV_KEY: sorted(self._BTN.values()),
            e.EV_ABS: [(e.ABS_X, stick), (e.ABS_Y, stick), (e.ABS_RX, stick),
                       (e.ABS_RY, stick), (e.ABS_Z, trig), (e.ABS_RZ, trig),
                       (e.ABS_HAT0X, hat), (e.ABS_HAT0Y, hat)],
        }
        self.ui = UInput(caps, name="Microsoft X-Box 360 pad", vendor=0x045E,
                         product=0x028E, version=0x110, bustype=e.BUS_USB)
        self._sent = {}
        self.reset()

    def reset(self):
        self._buttons = 0
        self._axes = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0}

    def press_button(self, button):
        self._buttons |= int(button)

    def left_joystick_float(self, x_value_float, y_value_float):
        self._axes["lx"], self._axes["ly"] = x_value_float, y_value_float

    def right_joystick_float(self, x_value_float, y_value_float):
        self._axes["rx"], self._axes["ry"] = x_value_float, y_value_float

    def left_trigger_float(self, value_float):
        self._axes["lt"] = value_float

    def right_trigger_float(self, value_float):
        self._axes["rt"] = value_float

    @staticmethod
    def _stick(v, invert=False):
        v = max(-1.0, min(1.0, float(v)))
        v = -v if invert else v          # XInput +Y is up, evdev +Y is down
        return int(round(v * 32767)) if v >= 0 else int(round(v * 32768))

    def update(self):
        e, a = self._e, self._axes
        want = {}
        for bit, code in self._BTN.items():
            want[(e.EV_KEY, code)] = 1 if self._buttons & bit else 0
        hx = hy = 0
        for bit, (axis, d) in self._HAT.items():
            if self._buttons & bit:
                if axis == "x":
                    hx += d
                else:
                    hy += d
        want[(e.EV_ABS, e.ABS_HAT0X)] = hx
        want[(e.EV_ABS, e.ABS_HAT0Y)] = hy
        want[(e.EV_ABS, e.ABS_X)] = self._stick(a["lx"])
        want[(e.EV_ABS, e.ABS_Y)] = self._stick(a["ly"], invert=True)
        want[(e.EV_ABS, e.ABS_RX)] = self._stick(a["rx"])
        want[(e.EV_ABS, e.ABS_RY)] = self._stick(a["ry"], invert=True)
        want[(e.EV_ABS, e.ABS_Z)] = int(round(max(0.0, min(1.0, a["lt"])) * 255))
        want[(e.EV_ABS, e.ABS_RZ)] = int(round(max(0.0, min(1.0, a["rt"])) * 255))
        dirty = False
        for key, v in want.items():
            if self._sent.get(key) != v:
                self.ui.write(key[0], key[1], v)
                self._sent[key] = v
                dirty = True
        if dirty:
            self.ui.syn()

    def close(self):
        self.ui.close()


def _new_gamepad():
    if vg is not None:
        return vg.VX360Gamepad()
    if sys.platform.startswith("linux"):
        return _UInputX360()
    raise RuntimeError("no virtual pad backend: pip install vgamepad (Windows) "
                       "or evdev (Linux)")


SLOT_NAMES = {0: "Player 1", 1: "Player 2", 2: "Player 3", 3: "Player 4"}


def check_physical_slots(expected=1, wait_s=6.0, poll_s=0.4):
    """Warn (and briefly wait) if more XInput slots look occupied than
    `expected`, checked BEFORE any of this process's own virtual pads exist.

    Steam Input can double-register one physical controller as TWO XInput
    slots - seen with the 8BitDo GBros Adapter, most often right after a
    previous session's virtual pad disconnects and Steam re-scans. When that
    happens here, the bot's own virtual pad lands on the NEXT free slot
    (Player 3) instead of Player 2, and neither --p1 nor detect_side() can
    make sense of it - the game now sees three controllers, not two.

    This cannot be fixed from here: a real controller's XInput slot is owned
    by Windows/Steam, not by this process. What we CAN do is notice it before
    making things worse, and give the fix (unplug/replug the adapter's
    dongle, or toggle Steam Input off/on for it) a few seconds to land.
    Returns the final occupied-slot set either way; the caller decides
    whether to proceed.
    """
    slots = _xinput_connected()
    if len(slots) <= expected:
        return slots
    print(f"!! {len(slots)} XInput slots are already occupied ({sorted(slots)}) "
          f"before this bot's own pad exists - expected at most {expected}.")
    print("   Usual causes:")
    print("   * another bot/pad process is still running (an old play.py,")
    print("     play2.py or capture tool) - its virtual pad holds a slot;")
    print("     close it, or end stray python.exe in Task Manager")
    print("   * the Steam Input / 8BitDo double-registration bug (one physical")
    print("     controller counted twice) - unplug and replug the dongle")
    print(f"   Waiting up to {wait_s:.0f}s for the slot count to drop back down...")
    waited = 0.0
    while waited < wait_s:
        time.sleep(poll_s)
        waited += poll_s
        slots = _xinput_connected()
        if len(slots) <= expected:
            print(f"   fixed - {len(slots)} slot(s) occupied now.")
            return slots
    print(f"   still {len(slots)} slots occupied after {wait_s:.0f}s - starting "
          f"anyway; the bot's pad may end up as Player {len(slots) + 1}.")
    return slots

if vg is not None:
    B = vg.XUSB_BUTTON
else:
    class B:                 # the XInput button bits vgamepad would supply
        XUSB_GAMEPAD_DPAD_UP, XUSB_GAMEPAD_DPAD_DOWN = 0x0001, 0x0002
        XUSB_GAMEPAD_DPAD_LEFT, XUSB_GAMEPAD_DPAD_RIGHT = 0x0004, 0x0008
        XUSB_GAMEPAD_START, XUSB_GAMEPAD_BACK = 0x0010, 0x0020
        XUSB_GAMEPAD_LEFT_THUMB, XUSB_GAMEPAD_RIGHT_THUMB = 0x0040, 0x0080
        XUSB_GAMEPAD_LEFT_SHOULDER, XUSB_GAMEPAD_RIGHT_SHOULDER = 0x0100, 0x0200
        XUSB_GAMEPAD_GUIDE = 0x0400
        XUSB_GAMEPAD_A, XUSB_GAMEPAD_B = 0x1000, 0x2000
        XUSB_GAMEPAD_X, XUSB_GAMEPAD_Y = 0x4000, 0x8000

# KeyConfig TypeA -> Xbox pad
NATTACK = B.XUSB_GAMEPAD_X           # A  (normal attack)
QUIRKTEC1 = B.XUSB_GAMEPAD_Y         # Q1
QUIRKTEC2 = B.XUSB_GAMEPAD_B         # Q2
JUMP = B.XUSB_GAMEPAD_A
DASH = B.XUSB_GAMEPAD_LEFT_SHOULDER
GUARD = B.XUSB_GAMEPAD_RIGHT_SHOULDER

# Menu navigation
CONFIRM = B.XUSB_GAMEPAD_A
CANCEL = B.XUSB_GAMEPAD_B
DPAD_UP = B.XUSB_GAMEPAD_DPAD_UP
DPAD_DOWN = B.XUSB_GAMEPAD_DPAD_DOWN
DPAD_LEFT = B.XUSB_GAMEPAD_DPAD_LEFT
DPAD_RIGHT = B.XUSB_GAMEPAD_DPAD_RIGHT
PAGE_LEFT = B.XUSB_GAMEPAD_LEFT_SHOULDER
PAGE_RIGHT = B.XUSB_GAMEPAD_RIGHT_SHOULDER
START = B.XUSB_GAMEPAD_START
BACK = B.XUSB_GAMEPAD_BACK
RSTICK = B.XUSB_GAMEPAD_RIGHT_THUMB   # right stick click - activates Rising
LSTICK = B.XUSB_GAMEPAD_LEFT_THUMB

FPS = 60.0
FRAME = 1.0 / FPS

NEUTRAL = (0.0, 0.0)
UP = (0.0, 1.0)
DOWN = (0.0, -1.0)
LEFT = (-1.0, 0.0)
RIGHT = (1.0, 0.0)

# action -> (buttons, stick or None). None stick = leave whatever movement is held.
ACTIONS = {
    "A":      ((NATTACK,), None),
    "Q1":     ((QUIRKTEC1,), None),
    "Q2":     ((QUIRKTEC2,), None),
    "JUMP":   ((JUMP,), None),
    "DASH":   ((DASH,), None),
    "GUARD":  ((GUARD,), None),
    "UB":     ((GUARD, NATTACK), None),          # GUARDNO
    "PU1":    ((GUARD, QUIRKTEC1), None),        # PULTRA1
    "PU2":    ((GUARD, QUIRKTEC2), None),        # PULTRA2
    "PU_ALL": ((GUARD, QUIRKTEC1, QUIRKTEC2), None),
    "TAG1":   ((GUARD,), LEFT),                  # SIDEKICKATK1
    "TAG2":   ((GUARD,), RIGHT),                 # SIDEKICKATK2
}

# Tilt variants need a stick direction held with the button.
TILT_ACTIONS = {
    "TQ": (QUIRKTEC2,),       # QUIRKTEC3 = stick dir + B
    "LK": (NATTACK,),         # LKAKU     = stick dir + X
}


def tick_all(pads, frames=1):
    """Advance several pads on ONE shared frame clock.

    Each pad's own tick() sleeps, so ticking two pads in series costs two
    frames of wall time for one frame of game time - at 15 decisions/second
    that alone would blow the budget. Here every pad is flushed first and the
    sleep is paid once. Their internal clocks are left in sync so a later solo
    tick() does not burst to catch up.
    """
    pads = [p for p in pads if p is not None]
    if not pads:
        return
    if getattr(pads[0], "link", None) is not None:
        # Lockstep (ajbot.lockstep): one request runs the frames on the
        # game's own clock, for every pad on the link at once.
        if frames > 0:
            pads[0].link.step(int(frames))
        return
    lead = pads[0]
    for _ in range(max(0, int(frames))):
        for p in pads:
            p._flush()
            p.frame += 1
        lead._next += FRAME
        delay = lead._next - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            lead._next = time.perf_counter()
    for p in pads[1:]:
        p._next = lead._next


class Pad:
    """Frame-oriented virtual controller. One update() call == one game frame."""

    def __init__(self):
        self._slots_before = _xinput_connected()
        self._slot = None
        self.gp = _new_gamepad()
        self._buttons = set()
        self._stick = NEUTRAL
        self._rstick = NEUTRAL  # right stick tilt = dodge
        self._lt = 0.0          # triggers are analog, not buttons
        self._rt = 0.0
        self._next = time.perf_counter()
        self.frame = 0
        self._flush()           # a pad is only given a slot once it reports
        # XInput can take a moment to notice it: ~1.5 s under Proton, where
        # the uinput device goes through SDL and winebus first.
        # Through the game's view (see _xinput_connected) it can also lag a
        # little behind Windows on Windows.
        # The game's view has been seen to take over 3 s (2026-11-10: pad A
        # read -1, then pad B found slot 0 already taken - A's, arriving
        # late), so wait up to 8 s; it stops as soon as the slot appears.
        wait = 5.0 if sys.platform != "win32" else (
            8.0 if _registered_hook_slots() is not None else 1.0)
        deadline = time.perf_counter() + wait
        while time.perf_counter() < deadline:
            if _xinput_connected() - self._slots_before:
                break
            time.sleep(0.025)

    @property
    def slot(self):
        """XInput user index: 0 is Player 1. -1 if it cannot be determined.

        Queried from XInput rather than vgamepad's get_index(), which returns
        a 1-BASED ViGEm target index - reading it as an XInput slot reports
        Player 2 for a pad that is really Player 1.

        Slots go in connection order, so a virtual pad created while a real
        controller is already plugged in lands behind it.
        """
        if self._slot is None:
            new = _xinput_connected() - self._slots_before
            if not new:
                return -1         # not seen yet: ask again next time, do not cache
            self._slot = min(new)
        return self._slot

    def hidden_by_steam(self):
        """Windows gave this pad a slot but the game cannot see it.

        Inside the game Steam answers XInput, and after a Steam Controller is
        unplugged it keeps that controller's player slot (seen 2026-11-10:
        Windows had our pad on slot 0, the game read slot 0 as empty, and the
        next pad became Player 2). Only restarting the game - with the Steam
        Controller off - gives the slot back."""
        if _XINPUT is None or _registered_hook_slots() is None:
            return False
        if self.slot >= 0:
            return False
        st = _XState()
        win = {i for i in range(4) if _XINPUT.XInputGetState(i, ctypes.byref(st)) == 0}
        # Not by slot number: a Steam-only controller can hold game slot 0
        # while Windows puts our pad on ITS slot 0 (seen 2026-11-10: windows
        # {0}, game {0}, and the pad was invisible to the game). Our pad never
        # appeared in the game, yet Windows has it: Steam is hiding it.
        return bool(win)

    # ---- low level ----
    def hold(self, *buttons):
        self._buttons.update(buttons)

    def release(self, *buttons):
        self._buttons.difference_update(buttons)

    def stick(self, xy):
        self._stick = xy

    def rstick(self, xy):
        """Right stick tilt. Tilting it is a Dodge (the tutorial's
        alternative to Guard + direction); clicking it is RSTICK."""
        self._rstick = xy

    def trigger(self, side, value=1.0):
        """Analog trigger, 0..1. Tag switching is bound to these."""
        if side == "left":
            self._lt = float(value)
        else:
            self._rt = float(value)

    def clear(self):
        self._buttons.clear()
        self._stick = NEUTRAL
        self._rstick = NEUTRAL
        self._lt = self._rt = 0.0

    def _flush(self):
        self.gp.reset()
        for b in self._buttons:
            self.gp.press_button(button=b)
        x, y = self._stick
        self.gp.left_joystick_float(x_value_float=x, y_value_float=y)
        rx, ry = self._rstick
        self.gp.right_joystick_float(x_value_float=rx, y_value_float=ry)
        self.gp.left_trigger_float(value_float=self._lt)
        self.gp.right_trigger_float(value_float=self._rt)
        self.gp.update()

    def tick(self, frames=1):
        """Apply current state and sleep until the next frame boundary."""
        for _ in range(frames):
            self._flush()
            self.frame += 1
            self._next += FRAME
            delay = self._next - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                self._next = time.perf_counter()

    # ---- action level ----
    def do(self, action, hold_frames=2, recover_frames=0, direction=None):
        """Press an action for hold_frames, then optionally wait recover_frames."""
        if action in TILT_ACTIONS:
            buttons = TILT_ACTIONS[action]
            stick = direction if direction is not None else RIGHT
        elif action in ACTIONS:
            buttons, stick = ACTIONS[action]
        else:
            raise KeyError(f"unknown action: {action}")

        prev_stick = self._stick
        if stick is not None:
            self.stick(stick)
        self.hold(*buttons)
        self.tick(hold_frames)
        self.release(*buttons)
        if stick is not None:
            self.stick(prev_stick)
        if recover_frames:
            self.tick(recover_frames)

    def tap(self, button, hold_frames=3, gap_frames=9):
        """Single menu press. Menus need a gap between presses to register."""
        self.hold(button)
        self.tick(hold_frames)
        self.release(button)
        self.tick(gap_frames)

    def tap_n(self, button, count, hold_frames=3, gap_frames=9):
        for _ in range(count):
            self.tap(button, hold_frames, gap_frames)

    def move(self, xy, frames):
        self.stick(xy)
        self.tick(frames)
        self.stick(NEUTRAL)

    def idle(self, frames):
        self.clear()
        self.tick(frames)

    def reset(self):
        self.clear()
        self._flush()

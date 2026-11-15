"""Menu automation: character select and the control-mode screen.

Option indices come from the game's own tables:
  DT_ComboSelect         Normal=0, Manual=1, Custom=2
  DT_CustomSettingSelect AutoCombo=0, AutoSwitchCombo=1, AutoPlusUltra=2,
                         AutoDaash=3, AutoGuard=4
  DT_CustomSettingOption ON=0, OFF=1

Manual is the preset that switches auto-combo and auto-Plus-Ultra off, so the
bot only needs to land on index 1.

Character-select geometry lives in ajbot/grid.py - 12x6, wrapping in both
axes, with the Random cell as a seventh horizontal stop.
"""
from .pad import (
    CONFIRM, CANCEL, DPAD_UP, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT,
    PAGE_LEFT, PAGE_RIGHT, START,
)
import json
import threading
from pathlib import Path

from . import grid as G

MENU_GAP = 15           # frames between menu presses; the grid drops
                        # inputs sent during its scroll animation
VERTICAL_GAP = 30       # frames between UP/DOWN taps on the character grid.
                        # Only 3 rows are visible, so vertical moves scroll it,
                        # and at MENU_GAP it still swallowed one press in a long
                        # run: "down x7" landed 6 rows down and "up x5" landed 4,
                        # putting every pick exactly one row off.
VERTICAL_HOLD = 4
START_HOLD = 8          # START needs a longer press than d-pad taps
SETTLE = 24             # frames to let a screen settle before committing
CONTROL_OPEN_WAIT = 150 # frames (2.5 s) for the control-mode screen to appear
                        # after the last pick; a Right sent before it is ready
                        # is lost, the row stays Normal and its two sub-options
                        # appear, so the next Down lands inside them
CONTROL_GAP = 30        # frames after each press on that screen: a toggle
                        # folds/unfolds the sub-options, which animates

CONTROL_NORMAL = 0
CONTROL_MANUAL = 1
CONTROL_CUSTOM = 2


def _step(pad, button, count, gap=MENU_GAP, hold=3):
    if count > 0:
        pad.tap_n(button, count, hold_frames=hold, gap_frames=gap)


# ---------------- character select ----------------

CURSOR_MAX_AGE_MS = 1500   # a pushed cursor older than this is not trusted
CURSOR_RETRIES = 3         # re-route attempts when the game shows a miss


_HOOK = None


def use_hook(hook):
    """Register the process's existing ajhook connection for the menu code.

    ajhook's pipe has only a few server instances; opening one connection per
    navigator/step exhausted them ("busy"), so everything here shares one."""
    global _HOOK
    _HOOK = hook


def _try_hook():
    """The shared ajhook connection, opening one if none was registered; None
    if the game/DLL is absent."""
    global _HOOK
    if _HOOK is not None:
        return _HOOK
    try:
        from .hook import Hook
        _HOOK = Hook(timeout=0.3)
    except Exception:
        _HOOK = None
    return _HOOK


STEP_WAIT_FRAMES = 18      # frames to wait for the cursor to register a press
CONFIRM_WAIT_FRAMES = 40   # frames to wait for a pick to register
GOTO_MAX_PRESSES = 90      # safety cap for one goto()


def _press(pad, button, hold=3):
    pad.hold(button)
    pad.tick(hold)
    pad.release(button)


def _wait(pad, cond, frames):
    """Tick the pad until cond() is truthy (returned) or `frames` pass (None)."""
    for _ in range(frames):
        pad.tick(1)
        v = cond()
        if v:
            return v
    return None


def detect_side(pad, hook, fallback):
    """Which player panel this pad really drives: "1P" or "2P".

    The pad's XInput slot is only a guess. With a human on a physical
    controller the virtual pad can report slot 0 yet be Player 2 in the game,
    so the navigator read the human's cursor, never saw it reach the target
    and scrolled down forever. Here the truth is measured: tap Right, see
    whose cursor moved, tap Left to put it back. A tap during which both
    cursors moved (the human moving at the same time) is repeated.
    """
    if hook is None:
        return fallback

    def positions():
        try:
            return {s: (hook.pad_state(s) or {}).get("pos") for s in ("1P", "2P")}
        except Exception:
            return None

    for _ in range(4):
        before = positions()
        if not before or all(v is None for v in before.values()):
            pad.idle(MENU_GAP)
            continue
        _press(pad, DPAD_RIGHT)
        _wait(pad, lambda: positions() != before, 24)
        after = positions() or {}
        moved = [s for s in before if before[s] is not None and after.get(s) != before[s]]
        _press(pad, DPAD_LEFT)
        pad.idle(MENU_GAP * 2)
        if len(moved) == 1:
            return moved[0]
    return fallback


class CharacterSelect:
    """Cursor-tracking navigator for the character grid.

    `side` picks which panel the pad drives, because the Random cell sits on a
    different edge for 1P and 2P. A freshly connected virtual pad claims 2P.

    With ajhook's cursor feed it is closed-loop: each press is sent, the cursor
    is watched until it actually moves (a press swallowed by the scroll
    animation is simply sent again), the route is recomputed from where the
    cursor really is, and a pick is confirmed by the game's picks counter.
    Without the feed it falls back to blind press counting.
    """

    def __init__(self, pad, start="DekuFinal", side="2P", hook=None):
        self.pad = pad
        self.side = side
        self.cursor = G.position_of(start) if isinstance(start, str) else start
        self.hook = hook if hook is not None else _try_hook()
        st = self._state()
        if st is not None:
            self.cursor = self._frm(st)

    def _state(self):
        """Fresh cursor state for this side, or None (no feed / off the screen)."""
        if self.hook is None:
            return None
        try:
            st = self.hook.pad_state(self.side)
        except Exception:
            return None
        if not st or "pos" not in st or st.get("age_ms", 1 << 30) > CURSOR_MAX_AGE_MS:
            return None
        return st

    def _frm(self, st):
        """(row, col) the cursor is on. The Random cell is not a grid cell: the
        reported position stays on the last cell, so place it beside its row -
        left of column 0 for 2P, right of the last column for 1P."""
        row, col = st["row"], st["col"]
        if st.get("random"):
            col = -1 if self.side == "2P" else G.cols_in_row(row)
        return row, col

    def _seen(self):
        st = self._state()
        if st is None or st.get("random"):
            return None
        return st["row"], st["col"]

    def _goto_blind(self, target):
        dr, dc = G.route(self.cursor, target, self.side)
        _step(self.pad, DPAD_DOWN if dr > 0 else DPAD_UP, abs(dr),
              gap=VERTICAL_GAP, hold=VERTICAL_HOLD)
        if dr:
            self.pad.idle(MENU_GAP)       # let the last scroll finish
        _step(self.pad, DPAD_RIGHT if dc > 0 else DPAD_LEFT, abs(dc))
        self.cursor = target

    def goto(self, name):
        target = G.position_of(name)
        if self._state() is None:
            self._goto_blind(target)
            return target
        for _ in range(GOTO_MAX_PRESSES):
            st = self._state()
            if st is None:
                self._goto_blind(target)
                return target
            frm = self._frm(st)
            if frm == target and not st.get("random"):
                self.cursor = target
                return target
            dr, dc = G.route(frm, target, self.side)
            if dr:
                btn = DPAD_DOWN if dr > 0 else DPAD_UP
            else:
                btn = DPAD_RIGHT if dc > 0 else DPAD_LEFT
            before = (st["pos"], st.get("random"))
            _press(self.pad, btn)
            # A swallowed press leaves the cursor unchanged: the loop re-sends it.
            _wait(self.pad, lambda: (lambda s: s is not None and
                                     (s["pos"], s.get("random")) != before)(self._state()),
                  STEP_WAIT_FRAMES)
        print(f"[menus] {self.side}: gave up reaching {name} (cursor {self._frm(self._state() or {'row': 0, 'col': 0})})",
              flush=True)
        return target

    def pick(self, name):
        pos = self.goto(name)
        st = self._state()
        base = st.get("banner", 0) if st is not None else None
        for _ in range(3):
            _press(self.pad, CONFIRM)
            if base is None:
                self.pad.idle(MENU_GAP * 2)
                break
            landed = _wait(self.pad, lambda: (lambda s: s is not None and
                                              (s.get("banner", 0) > base or s.get("complete")))(self._state()),
                           CONFIRM_WAIT_FRAMES)
            if landed:
                break
        return pos

    def select_team(self, names):
        if len(names) != 3:
            raise ValueError(f"need exactly 3 characters, got {len(names)}")
        return [self.pick(n) for n in names]

    def describe(self, name):
        r, c = G.position_of(name)
        return f"{name}: row {r} col {c}  ({G.describe(self.cursor, (r, c), self.side)})"


def select_team(pad, names, start="DekuFinal", side="2P"):
    return CharacterSelect(pad, start, side).select_team(names)


# ---------------- control mode ----------------

def set_control_mode(pad, mode=CONTROL_MANUAL, from_index=CONTROL_NORMAL):
    """Move one character's control-mode selector horizontally to `mode`.

    Does not commit - the screen is committed once with START after every row
    is set. Adjacent options only (one step), so it works with or without
    wrap-around.
    """
    delta = mode - from_index
    _step(pad, DPAD_RIGHT if delta > 0 else DPAD_LEFT, abs(delta))


# The control-mode screen REMEMBERS each character's setting, per player
# (confirmed in game), and nothing readable reports it. Each character's row
# toggles Normal <-> Manual with one Left or Right press, so pressing Right
# blindly on every visit switched a character that was already Manual back
# to Normal - auto combo and auto Plus Ultra on again. What the bot last set
# is remembered here, per player side and character, and a row is only
# pressed when that character is not already Manual.
CONTROL_STATE = Path("stats") / "control_modes.json"


def _load_modes():
    try:
        state = json.loads(CONTROL_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # An earlier version stored one list per side (per row); per row is the
    # wrong model, so it is dropped.
    return {k: v for k, v in state.items() if isinstance(v, dict)}


def _save_modes(state):
    try:
        CONTROL_STATE.parent.mkdir(parents=True, exist_ok=True)
        CONTROL_STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError:
        pass


def forget_control_modes(side=None, mode=CONTROL_NORMAL):
    """Declare what EVERY character shows right now on a side (e.g. after
    setting them by hand), clearing what the bot remembered."""
    state = _load_modes()
    for s in ([side] if side else ["1P", "2P"]):
        state[s] = {"_default": mode}
    _save_modes(state)


def mark_control_mode(side, char, mode):
    """Correct the memory for one character (e.g. auto combo seen in play)."""
    state = _load_modes()
    state.setdefault(side, {})[char] = mode
    _save_modes(state)


def is_manual(side, char):
    state = _load_modes().get(side, {})
    return state.get(char, state.get("_default", CONTROL_NORMAL)) == CONTROL_MANUAL


def run_parallel(jobs):
    """Run callables at the same time, one thread each (one per pad), and wait
    for all of them; the first exception is re-raised afterwards."""
    errors = []

    def wrap(fn):
        def run():
            try:
                fn()
            except BaseException as e:      # noqa: BLE001 - reported below
                errors.append(e)
        return run

    threads = [threading.Thread(target=wrap(j), daemon=True) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]


def _pad_state(hook, side):
    if hook is None:
        return None
    try:
        return hook.pad_state(side)
    except Exception:
        return None


CONTROL_OPEN_MAX_FRAMES = 480   # wait for the control-mode screen (8 s cap)
CONTROL_ROW_WAIT_FRAMES = 36    # frames for a row toggle / cursor move to register
CONTROL_CLOSE_MAX_FRAMES = 300  # wait for the screen to close after confirming
_MODES_LOCK = threading.Lock()  # both pads run this at once; the file is shared


def _goto_control_row(pad, hook, side, row):
    """Move the control-mode cursor to `row` (0-based), verified by the game's
    cursor-row byte; a swallowed press is re-sent. Falls back to a timed Down."""
    for _ in range(8):
        st = _pad_state(hook, side)
        cur = st.get("crow") if st else None
        if cur is None:
            return False
        if cur == row:
            return True
        _press(pad, DPAD_DOWN if cur < row else DPAD_UP, hold=4)
        _wait(pad, lambda: (lambda s: s is not None and s.get("crow") != cur)(_pad_state(hook, side)),
              CONTROL_ROW_WAIT_FRAMES)
    return False


def set_all_manual(pad, count=3, commit=True, side="1P", chars=None, hook=None):
    """Control-mode screen: every row to Manual.

    Rows are top to bottom in pick order. With ajhook's feed this is
    closed-loop: it waits for the screen to open, moves to each row by the
    game's own cursor-row byte, toggles a row only if the game's live row
    state says it is not already Manual (that state is set from the remembered
    value on open and flips on every Left/Right press), and verifies each
    press. Without the feed it falls back to timed presses driven by what the
    bot last set (stats/control_modes.json), which is blind.
    """
    hook = hook if hook is not None else _try_hook()
    with _MODES_LOCK:
        mine = dict(_load_modes().get(side, {}))
    default = mine.get("_default", CONTROL_NORMAL)
    live = _pad_state(hook, side) is not None

    if live:
        opened = _wait(pad, lambda: (lambda s: s is not None and s.get("combo"))(_pad_state(hook, side)),
                       CONTROL_OPEN_MAX_FRAMES)
        if not opened:
            print(f"[menus] warning: {side} control-mode screen never reported open; continuing anyway",
                  flush=True)
        pad.idle(6)
    else:
        pad.idle(CONTROL_OPEN_WAIT)
    st = _pad_state(hook, side)
    print(f"[menus] {side} control-mode rows at open: "
          f"{st.get('rows') if st and st.get('rows') is not None else 'unreadable (using remembered file)'}",
          flush=True)

    for i in range(count):
        char = chars[i] if chars and i < len(chars) else None
        st = _pad_state(hook, side)
        rows = st.get("rows") if st else None
        if rows is not None and i < len(rows):
            if not _goto_control_row(pad, hook, side, i):
                if i:
                    pad.tap(DPAD_DOWN, hold_frames=4, gap_frames=CONTROL_GAP)
            for _ in range(3):
                st = _pad_state(hook, side)
                rows = st.get("rows") if st else rows
                if rows is None or rows[i] == CONTROL_MANUAL:
                    break
                _press(pad, DPAD_RIGHT, hold=4)
                _wait(pad, lambda: (lambda s: s is not None and s.get("rows") is not None
                                    and s["rows"][i] == CONTROL_MANUAL)(_pad_state(hook, side)),
                      CONTROL_ROW_WAIT_FRAMES)
        else:
            if i:
                pad.tap(DPAD_DOWN, hold_frames=4, gap_frames=CONTROL_GAP)
            current = mine.get(char, default) if char else CONTROL_NORMAL
            if current != CONTROL_MANUAL:
                _step(pad, DPAD_RIGHT, 1, gap=CONTROL_GAP, hold=4)
        if char:
            mine[char] = CONTROL_MANUAL

    st = _pad_state(hook, side)
    rows = st.get("rows") if st else None
    if rows is not None:
        print(f"[menus] {side} rows before commit: {rows[:count]}", flush=True)
        if any(r != CONTROL_MANUAL for r in rows[:count]):
            print(f"[menus] warning: {side} rows {rows[:count]} before commit, expected all Manual",
                  flush=True)
    if commit:
        pad.idle(SETTLE if not live else 6)
        for attempt in range(2):
            _press(pad, CONFIRM, hold=START_HOLD)
            if not live:
                pad.idle(MENU_GAP * 3)
                break
            closed = _wait(pad, lambda: (lambda s: s is not None and not s.get("combo"))(_pad_state(hook, side)),
                           CONTROL_CLOSE_MAX_FRAMES // 2)
            if closed:
                break
        # ComboModeSettings holds what was just committed (it resets to defaults
        # when the screen reopens, so it only confirms this commit).
        st = _pad_state(hook, side)
        after = st.get("ctl") if st else None
        if after is not None:
            for i in range(min(count, len(after))):
                char = chars[i] if chars and i < len(chars) else None
                if char:
                    mine[char] = after[i][0]
            if any(m[0] != CONTROL_MANUAL for m in after[:count]):
                print(f"[menus] warning: {side} committed control modes {after[:count]}, "
                      f"expected all Manual - remembered state corrected", flush=True)
    with _MODES_LOCK:
        state = _load_modes()
        state.setdefault(side, {}).update(mine)
        _save_modes(state)


def press_start(pad, repeat=1):
    """The control-mode screen's "Start" prompt is the A button, not Menu."""
    for _ in range(repeat):
        pad.tap(CONFIRM, hold_frames=START_HOLD, gap_frames=MENU_GAP * 3)


def press_menu(pad, repeat=1):
    """The actual Menu/Start button, for screens that want it."""
    for _ in range(repeat):
        pad.tap(START, hold_frames=START_HOLD, gap_frames=MENU_GAP * 3)


def back(pad, times=1):
    pad.tap_n(CANCEL, times, gap_frames=MENU_GAP)


def confirm(pad, times=1):
    pad.tap_n(CONFIRM, times, gap_frames=MENU_GAP)


def page(pad, direction=1, times=1):
    _step(pad, PAGE_RIGHT if direction > 0 else PAGE_LEFT, times)

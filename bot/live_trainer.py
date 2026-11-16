"""The combo trainer, in the person's own match (practice.py --trainer).

Nothing is started or travelled to: go into your own training match, pick
the character, and play. You control your fighter with your own controller or
keyboard the whole time; the trainer only watches and steps in:

  * the next input of the combo is shown at the bottom of the screen, in the
    game's own key icons for your device (server/uewidget.Prompt);
  * once the combo has started, each next step's input window comes from the
    game's own montage notifies (ajbot/movewindows.py; the route's learned
    timing where there is no game data). The game runs at full speed through
    the move and drops to slow motion (SLOW, global time dilation) just
    before the window opens; the bar behind the prompt fills red and drains
    right to left until the window opens, then green until it closes;
  * a wrong move, a step that comes too late, or no step for a while drops
    the attempt and it starts over from the first step.

The game is never held: the person's inputs always go straight to it. The
overlay window is only used when the in-game prompt cannot be built. Stop
in the GUI, or Backspace, ends it.
"""
import json
import os
import re
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import uecall as U  # noqa: E402

from ajbot import dataset as D
from ajbot import demo
from ajbot import movewindows as MW
from ajbot import sampler
from ajbot.agent import plan_press
from ajbot.hook import Hook, HookError
from ajbot.overlay import Overlay, arrow
import trainer_gate as TG

NEUTRAL = (0.0, 0.0)
DASHES = {"Dash", "DashForward", "DashBack"}
SLOW = 0.2             # game speed around each input window
SLOW_LEAD_S = 0.2      # slow down this many game seconds before the window opens
RESUME_S = 0.15        # after a press lands, full speed for at least this (game s)
LATE_GRACE_S = 0.2     # the step may register this long after its window closed
DEBOUNCE_F = 8         # the previous step's label again within this: the same press
LAG_F = 6              # the step's montage may start this many frames before it registers
LEARNED_EARLY_S = 0.1  # learned timing (no game data): window around the due frame
LEARNED_LATE_S = 0.35
IDLE_S = 2.5           # combo started but no step for this long: dropped
CHECK_S = 2.0          # before the combo starts, re-check the prompt widget this often
SHOW_RESULT_S = 1.6
POLL_S = 0.003
GS = "/Script/Engine.GameplayStatics"
TRACE = Path(__file__).resolve().parents[1] / "logs" / "trainer_trace.jsonl"


def same(want, got, step):
    if want == got or got in (step.get("alias") or ()):
        return True
    return want in DASHES and got in DASHES


class Live:
    def __init__(self, route, stop_file=None, log=print):
        self.route = route
        self.steps = route["steps"]
        self.n = len(self.steps)
        self.chain = [TG.chip(s) for s in self.steps]
        learned = route.get("learned") or {}
        adj = learned.get("adj")
        self.adj = adj if adj and len(adj) == self.n else [0] * self.n
        self.lat = int(learned.get("lat", 2))
        self.stop_file = stop_file
        self.log = log
        self.hook = Hook(timeout=10.0)
        self.kb = TG.Keyboard(read_game=False)   # no UE4SS struct reads: they crash the game
        self.ui = self.ov = None
        self.pad_seen = False
        self.quit = False
        self.fps = 60.0
        self.wins = {}        # fighter folder -> movewindows.Montages
        self.trace = None
        self.dil = None       # game speed we last set (None = not yet)
        self.pc = None
        self.dil_at = [(0, 1.0)]   # (frame, speed from that frame) - the game clock

    # ---- game ----
    def frame(self):
        r = self.hook._send("Z", "?")
        for tok in r.split():
            if tok.startswith("frame="):
                return int(tok[6:])
        raise HookError(f"no frame in {r!r}")

    def view(self):
        try:
            t = sampler.teams(sampler.snapshot(self.hook))
        except HookError:
            return None, None
        return t.get("me"), t.get("enemy")

    def read(self):
        """Pads and keyboard: (buttons, lt, rt, x, y) - Backspace sets Back."""
        r = TG.real_pads(self.hook)
        seen, p = r if r else (False, (0, 0, 0, 0.0, 0.0))
        self.pad_seen = self.pad_seen or seen
        kb, klt, krt, kx, ky, quit_ = self.kb.read()
        btn = p[0] | kb | (TG.BACK_BIT if quit_ else 0)
        sx, sy = (kx, ky) if abs(kx) + abs(ky) > abs(p[3]) + abs(p[4]) else (p[3], p[4])
        return btn, max(p[1], klt), max(p[2], krt), sx, sy

    def stopped(self):
        return self.quit or bool(self.stop_file and os.path.exists(self.stop_file))

    # ---- what is drawn ----
    def show(self, markup, ov=None, fast=True):
        """In-game prompt; the overlay (with `ov` state) if that fails."""
        if self.ui:
            try:
                self.ui.show(markup, check=not fast)
                return
            except Exception as e:  # noqa: BLE001
                self.log(f"in-game prompt failed ({e}) - using the overlay")
                self.ui = None
        if ov is not None:
            if not self.ov:
                self.ov = Overlay()
            self.ov.show(chain=self.chain, **ov)

    def step_markup(self, k, me, en):
        st = self.steps[k]
        if st["label"] == "Tag":
            return "{Sidekick1,} / {Sidekick2,}   " + self.chain[k], (0, None, "left")
        lab = st.get("press") or st["label"]
        stick = NEUTRAL
        if st.get("rel") is not None and me:
            from ajbot.agent import toward
            stick = toward(me, float(st["rel"]), en=en)
        buttons, stick, _ = plan_press(lab, stick, me, en)
        need = 0
        for b in buttons:
            need |= int(b)
        want = stick if arrow(stick) else None
        head = "<default3>Start:</> " if k == 0 else ""
        return (head + TG.input_markup(self.kb, need, want, None) + "   " + self.chain[k],
                (need, want, None))

    # ---- slow motion + the meter ----
    def slow(self, on):
        """Game speed SLOW (on) or normal, through the world's global time
        dilation; the change is logged on the game clock."""
        v = SLOW if on else 1.0
        if v == self.dil:
            return
        try:
            # cached: the lookup is a UE4SS probe (~0.1 s); a level load clears it
            pc = self.pc = self.pc or U.find_first("PlayerController")
            if pc:
                U.call(U.cdo(GS), f"{GS}:SetGlobalTimeDilation",
                       struct.pack("<Qf", pc, v) + b"\0" * 4)
        except Exception as e:  # noqa: BLE001
            self.log(f"could not change game speed ({e})")
            return
        try:
            f = self.frame()
        except HookError:
            f = self.dil_at[-1][0]
        self.dil_at.append((f, v))
        self.dil = v

    def gt(self, f):
        """Game seconds at frame `f` (frames before the first change at 1x)."""
        t = 0.0
        ch = self.dil_at
        for i, (f0, v) in enumerate(ch):
            f1 = ch[i + 1][0] if i + 1 < len(ch) else None
            if i == 0 and f < f0:
                return (f - f0) / self.fps
            if f1 is not None and f >= f1:
                t += (f1 - f0) * v / self.fps
            else:
                return t + (f - f0) * v / self.fps
        return t

    def meter(self, p, color=None):
        if self.ui:
            try:
                import uewidget
                self.ui.meter(p, color or uewidget.RED)
            except Exception:  # noqa: BLE001
                pass

    # ---- main loop ----
    def run(self):
        self.hook._send("Z", "keys pass")
        self.hook._send("Z", "off")
        try:
            vp = self.hook._send("N", "")
            if not vp.startswith("vpads=0"):
                # bot pads left on a slot would take the person's place
                self.hook._send("N", "0")
        except HookError:
            pass
        sampler.install(self.hook, every_pumps=1)
        try:
            import uewidget
            self.ui = uewidget.Prompt()
            self.ui.ensure()
        except Exception as e:  # noqa: BLE001
            self.log(f"in-game prompt unavailable ({e}) - using the overlay")
            self.ui = None
        self.log(f"trainer: {self.route['name']} - go into your training match and play; "
                 f"the game slows down around each step's input window")
        try:
            TRACE.parent.mkdir(exist_ok=True)
            self.trace = open(TRACE, "a", buffering=1)
            self.tr(event="start", route=self.route["name"], t=round(time.time()))
        except OSError:
            self.trace = None
        self.measure_fps()
        tries = landed = 0
        try:
            while not self.stopped():
                r = self.attempt()
                if r is None:
                    break
                tries += 1
                landed += r == "LANDED"
                self.log(f"#{tries} {r}   ({landed} landed)")
        finally:
            try:
                self.hook._send("Z", "off")
            except HookError:
                pass
            self.dil = None
            self.slow(False)
            if self.ui:
                try:
                    self.ui.meter(None)
                    self.ui.hide(check=False)
                except Exception:  # noqa: BLE001
                    pass
            if self.ov:
                self.ov.close()
        self.log(f"trainer stopped: {landed}/{tries} landed")
        return 0

    def measure_fps(self):
        f0, t0 = self.frame(), time.time()
        time.sleep(0.5)
        df, dt = self.frame() - f0, time.time() - t0
        if df > 5:
            self.fps = df / dt
        self.log(f"game runs at {self.fps:.0f} frames/s")

    def windows(self, me):
        m = re.match(r"(Pl\d+)", (me or {}).get("id") or "")
        if not m:
            return None
        if m.group(1) not in self.wins:
            self.wins[m.group(1)] = MW.Montages(m.group(1))
        return self.wins[m.group(1)]

    def window(self, k, me, f, last_f, reg_mont):
        """((open, close) in game seconds (gt) for step k's input, why) - the
        game's input window in the montage the previous step plays, else the
        route's learned timing; (None, why) while that montage has not
        started yet (a homing lead-in plays first)."""
        mont, mn = me.get("mont"), me.get("mont_n")
        wins = self.windows(me)
        early = f - last_f < round(1.5 * self.fps)
        if wins and wins.available and mont and isinstance(mn, (int, float)):
            start = f - int(mn)
            # The step registers on its command, a frame or two before its
            # montage starts: until then `mont` is still the previous move's
            # (reg_mont), whose windows are not this step's.
            if start < last_f - LAG_F:
                if early:
                    return None, f"waiting for the move after {mont}"
            else:
                w = wins.get(mont)
                if w is not None:
                    st, prev = self.steps[k], self.steps[k - 1]
                    win = MW.pick(w, st.get("press") or st["label"],
                                  prev.get("press") or prev["label"])
                    if win:
                        a, b = win
                        t0 = self.gt(start)
                        return (t0 + a, t0 + b), f"{mont} window {a:.3f}-{b:.3f}s"
                    if not (w["next"] or w["cancel"]) and early:
                        return None, f"{mont} (lead-in)"
                    # has windows, none for this move: learned timing
                elif early:
                    return None, f"{mont} (no data)"
        due = self.gt(last_f) + (self.steps[k]["gap"] - self.lat + self.adj[k]) / self.fps
        return (due - LEARNED_EARLY_S, due + LEARNED_LATE_S), "learned timing"

    def tr(self, **row):
        if self.trace:
            self.trace.write(json.dumps(row) + "\n")

    def attempt(self):
        """One go at the combo. 'LANDED', 'dropped at ...', or None to quit."""
        if self.ui:
            try:
                self.ui.ensure()      # frames run here: a level load drops the widget
            except Exception as e:  # noqa: BLE001
                self.log(f"in-game prompt failed ({e}) - using the overlay")
                self.ui = None
        me, en = self.view()
        prev = me
        k = 0
        last_f = None         # frame the last step registered
        reg_mont = None       # montage playing when it registered
        shown = None
        shown_win = meter_from = None
        guessed = False
        checked = time.time()
        self.pc = None
        self.slow(False)
        self.meter(None)
        while True:
            if self.stopped():
                return None
            f = self.frame()
            me, en = self.view()
            if me is None:
                if shown != "wait" and self.ui:
                    try:
                        self.ui.ensure()
                    except Exception:  # noqa: BLE001
                        pass
                if shown != "wait":
                    self.show("<default2>Waiting for a match...</>", ov=dict(k=-1, banner="Waiting for a match"),
                              fast=False)
                    shown = "wait"
                time.sleep(0.3)
                prev = None
                self.pc = None        # a level load: new PlayerController
                self.dil = None       # (and the new world runs at 1x)
                continue
            if self.ui and (shown == "wait" or (k == 0 and time.time() - checked > CHECK_S)):
                # A level load (menu -> match, or a restart) destroys our
                # widget; writing a prompt into the dead one crashes the game.
                # Frames are running here, so the liveness probe answers.
                try:
                    self.ui.ensure()
                except Exception as e:  # noqa: BLE001
                    self.log(f"in-game prompt failed ({e}) - using the overlay")
                    self.ui = None
                checked = time.time()
                if shown == "wait":
                    shown = None
            if shown != k:
                markup, (need, want, trig) = self.step_markup(k, me, en)
                self.show(markup, ov=dict(k=k, prompt=self.chain[k], buttons=TG.names_of(need),
                                          stick=want, note=TG.KEY_NOTE))
                shown = k
            lab = D.label_of(prev, me)
            if not lab and demo.fresh_attack(prev, me):
                lab = me.get("cmd")
            if k:
                self.tr(f=f, k=k, act=me.get("act"), cmd=me.get("cmd"), push=me.get("ev_push"),
                        mont=me.get("mont"), mont_n=me.get("mont_n"), lab=lab)
            prev = me
            if (lab and k and last_f is not None and f - last_f < DEBOUNCE_F
                    and same(self.steps[k - 1]["label"], lab, self.steps[k - 1])):
                lab = None    # the same press again: command, then action a frame later
            if lab and lab not in demo.NOISE:
                if same(self.steps[k]["label"], lab, self.steps[k]):
                    self.tr(f=f, event="registered", step=k, lab=lab, mont=me.get("mont"))
                    k, last_f, reg_mont = k + 1, f, me.get("mont")
                    shown_win = None
                    self.slow(False)  # the press landed: full speed while it comes out
                    if k == self.n:
                        self.result(True)
                        return "LANDED"
                    continue
                if k > 0 and not same(self.steps[k - 1]["label"], lab, self.steps[k - 1]):
                    self.tr(f=f, event="drop", step=k, why=f"got {lab}")
                    return self.drop(k, f"got {lab}")
            if k == 0:
                time.sleep(POLL_S)
                continue
            if self.read()[0] & TG.BACK_BIT:
                self.quit = True
                return None
            now = self.gt(f)
            # Only what the game does decides: a wrong move, or the window
            # closing without the step coming out. How the person pressed
            # (early into the game's buffer, mashing) is theirs.
            # Locked once found: `start` (frame - mont_n) wobbles by a frame
            # between polls, which kept moving the window and the meter.
            win, why = (shown_win, None) if shown_win else self.window(k, me, f, last_f, reg_mont)
            if win is None:
                if now - self.gt(last_f) > IDLE_S:
                    self.tr(f=f, event="drop", step=k, why="no input")
                    return self.drop(k, "no input")
                self.meter(None)
                time.sleep(POLL_S)
                continue
            if win != shown_win:
                self.tr(f=f, event="window", step=k, open=round(win[0] - now, 3),
                        close=round(win[1] - now, 3), why=why)
                shown_win, meter_from = win, min(now, win[0])
                guessed = why == "learned timing"
            a, b = win
            # A game-data window is the game's own; a learned one is a guess,
            # so there the combo is over only once the move has ended.
            moving = str(me.get("act") or "").startswith("Attack")
            if now > b + LATE_GRACE_S and not (guessed and moving):
                self.tr(f=f, event="drop", step=k, why="too late")
                return self.drop(k, "too late")
            # full speed through the move (at least RESUME_S after a press
            # lands, so it reads as landed), slow motion just before the window
            # (and back to full speed once it closes while the game finishes the move)
            self.slow(a - SLOW_LEAD_S <= now <= b and now - self.gt(last_f) >= RESUME_S)
            import uewidget
            if now < a:
                self.meter((a - now) / max(a - meter_from, 1e-3), uewidget.RED)
            else:
                self.meter(max(b - now, 0.0) / max(b - a, 1e-3), uewidget.GREEN)
            time.sleep(POLL_S)

    def drop(self, k, why):
        self.slow(False)
        self.result(False, f"Dropped at {self.chain[k]} ({why})")
        return f"dropped at step {k} {self.chain[k]}: {why}"

    def result(self, ok, text="LANDED!"):
        self.slow(False)
        self.meter(None)
        self.show(("<default3>%s</>" if ok else "<default1>%s</>") % text,
                  ov=dict(k=self.n if ok else -1, banner=text, good=ok))
        t = time.time()
        while time.time() - t < SHOW_RESULT_S and not self.stopped():
            time.sleep(0.05)

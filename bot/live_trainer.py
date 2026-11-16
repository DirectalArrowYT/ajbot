"""The combo trainer, in the person's own match (practice.py --trainer).

Nothing is started or travelled to: go into your own training match, pick
the character, and play. You control your fighter with your own controller or
keyboard the whole time; the trainer only watches and steps in:

  * the next input of the combo is shown at the bottom of the screen, in the
    game's own key icons for your device (server/uewidget.Prompt);
  * once the combo has started (its first step came out), the game is held on
    the frame each next step is due (the route's learned timing) until you
    press that input - then it runs on and the game reads your press itself;
  * pressing a step early (before the hold) is fine: the game just goes on;
  * a wrong move, or no step for a while, drops the attempt and it starts
    over from the first step.

The overlay window is only used when the in-game prompt cannot be built.
Stop in the GUI, or Backspace while the game is held, ends it.
"""
import os
import time

from ajbot import dataset as D
from ajbot import demo
from ajbot import sampler
from ajbot.agent import plan_press
from ajbot.hook import Hook, HookError
from ajbot.overlay import Overlay, arrow
import trainer_gate as TG

NEUTRAL = (0.0, 0.0)
DASHES = {"Dash", "DashForward", "DashBack"}
LATE_F = 45            # frames after the hold lets go for the step to register
IDLE_F = 150           # combo started but no step for this long: dropped
SHOW_RESULT_S = 1.6
POLL_S = 0.003


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
        self.kb = TG.Keyboard()
        self.ui = self.ov = None
        self.pad_seen = False
        self.quit = False

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

    # ---- the hold ----
    def hold_for(self, k, need, want, trig):
        """Game parked: wait for step k's input, then let it run. False = quit."""
        down = self.read()[0]
        keep = time.time()
        while True:
            if time.time() - keep > 0.8:
                self.hook._send("Z", "0")     # keep the gate holding this frame
                keep = time.time()
                if self.stopped():
                    return False
            btn, lt, rt, sx, sy = self.read()
            if btn & TG.BACK_BIT:
                self.quit = True
                return False
            new = btn & ~down
            if trig:
                ok = lt > 128 or rt > 128
            else:
                ok = bool(new & need) and (btn & need) == need if need else True
            if ok and (not want or arrow((sx, sy)) == arrow(want)):
                return True
            down = btn
            time.sleep(POLL_S)

    def park_at(self, due):
        """Hold the game on frame `due` (now if that has passed)."""
        n = max(0, due - self.frame())
        r = self.hook._send("Z", str(n))
        return not r.startswith("!")

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
                 f"the game holds before each step until you press it")
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
            if self.ui:
                try:
                    self.ui.hide(check=False)
                except Exception:  # noqa: BLE001
                    pass
            if self.ov:
                self.ov.close()
        self.log(f"trainer stopped: {landed}/{tries} landed")
        return 0

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
        released_f = None     # frame the hold for step k let go
        shown = None
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
                continue
            if shown != k:
                markup, (need, want, trig) = self.step_markup(k, me, en)
                self.show(markup, ov=dict(k=k, prompt=self.chain[k], buttons=TG.names_of(need),
                                          stick=want, note=TG.KEY_NOTE))
                shown = k
            lab = D.label_of(prev, me)
            if not lab and demo.fresh_attack(prev, me):
                lab = me.get("cmd")
            prev = me
            if lab and lab not in demo.NOISE:
                if same(self.steps[k]["label"], lab, self.steps[k]):
                    k, last_f, released_f = k + 1, f, None
                    if k == self.n:
                        self.result(True)
                        return "LANDED"
                    continue
                if k > 0 and not same(self.steps[k - 1]["label"], lab, self.steps[k - 1]):
                    return self.drop(k, f"got {lab}")
            if k == 0:
                time.sleep(POLL_S)
                continue
            if released_f is not None:
                if f - released_f > LATE_F:
                    return self.drop(k, "too late")
            elif f - last_f > IDLE_F:
                return self.drop(k, "no input")
            else:
                due = last_f + self.steps[k]["gap"] - self.lat + self.adj[k]
                if f >= due - 1:
                    if not self.park_at(due):
                        continue
                    _, (need, want, trig) = self.step_markup(k, me, en)
                    if not self.hold_for(k, need, want, trig):
                        self.hook._send("Z", "off")
                        return None
                    self.hook._send("Z", "off")
                    released_f = self.frame()
                    continue
            time.sleep(POLL_S)

    def drop(self, k, why):
        self.result(False, f"Dropped at {self.chain[k]} ({why})")
        return f"dropped at step {k} {self.chain[k]}: {why}"

    def result(self, ok, text="LANDED!"):
        self.show(("<default3>%s</>" if ok else "<default1>%s</>") % text,
                  ov=dict(k=self.n if ok else -1, banner=text, good=ok))
        t = time.time()
        while time.time() - t < SHOW_RESULT_S and not self.stopped():
            time.sleep(0.05)

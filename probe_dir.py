"""Live check of the stick-direction sign (FrameTracker): run mid-match with
play2 stopped. Summarize with: for each stick angle, the median distance
change and the mean rside (see agent.FrameTracker.observe).

Hold one pad's stick at a sweep of angles and log how the distance changes,
with the game's lcam / rside. Ground truth for 'which stick angle approaches'.
usage: python probe_dir.py [P1|P2] [hold_s] [step_deg]"""
import json, math, os, sys, time
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")
from ajbot import Pad, sampler
from ajbot.hook import Hook
from ajbot.pad import tick_all

who = sys.argv[1] if len(sys.argv) > 1 else "P1"
HOLD = float(sys.argv[2]) if len(sys.argv) > 2 else 1.2
STEP = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
os.makedirs("logs", exist_ok=True)
out = open(f"logs/probe_dir_{who}.jsonl", "w")

hook = Hook()
sampler.install(hook, every_pumps=2)
sampler.reset(hook)
pads = [Pad(), Pad()]
print("slots", pads[0].slot, pads[1].slot, flush=True)
pad = pads[0] if who == "P1" else pads[1]


def fighters():
    snap = sampler.dedupe(sampler.snapshot(hook))
    return {k: r for k, r in snap.items() if r.get("active") and (r.get("age") or 1e9) < 300}


tick_all(pads, 30)
from ajbot.pad import CANCEL
if not fighters():
    for pp in pads:
        pp.tap(CANCEL, 6, 30)
    tick_all(pads, 60)
    print('closed pause; fighters:', list(fighters()), flush=True)
angles = [i * STEP for i in range(int(360 / STEP))] * 2
for a in angles:
    x, y = math.sin(math.radians(a)), math.cos(math.radians(a))
    pad.stick((x, y))
    t_end = time.time() + HOLD
    while time.time() < t_end:
        tick_all(pads, 3)
        fs = fighters()
        rows = {k: {f: r.get(f) for f in ("id", "enemy", "dist", "lcam", "rside", "lon", "lscale", "spdh", "act", "ground")}
                for k, r in fs.items()}
        out.write(json.dumps({"t": time.time(), "stick_deg": a, "f": rows}) + "\n")
    pad.stick((0.0, 0.0))
    tick_all(pads, 20)
out.close()
for p in pads:
    p.reset()
sampler.stop(hook)
print("done", flush=True)

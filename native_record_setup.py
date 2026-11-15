"""One-shot setup call for native (in-DLL) recording - installs the resident
sampler and starts the hook writing ticks itself, then exits immediately.

    python native_record_setup.py <output-path> [--every N]

This is the ONE piece of the native-recording path that still needs Python:
the resident sampler's Lua source (ajbot.sampler.RESIDENT) is hundreds of
carefully live-verified lines, and duplicating it into the GUI's Rust code
would be a real sync risk for no benefit. Everything else - start/stop/
status - is a plain opcode the GUI (or any other caller) sends directly to
the pipe; see gui/src/hookpipe.rs and ajbot/hook.py.

After this exits, the DLL keeps recording on its own until a stop ('X')
opcode is sent - no Python process needs to stay running.
"""
import argparse
import sys

from ajbot.hook import Hook, HookError
from ajbot import sampler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="output .jsonl path (created if missing)")
    ap.add_argument("--every", type=int, default=2,
                    help="resident chunk cadence, in pumps (lower = fresher, costlier)")
    args = ap.parse_args()

    try:
        hook = Hook(timeout=3.0)
    except HookError as e:
        print(f"!{e}")
        return 1
    if not hook.has_state():
        print("!not in a battle yet")
        return 1

    hook.set_resident(sampler.RESIDENT)
    hook.set_interval(args.every)
    print(hook.start_recording(args.path))
    hook.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

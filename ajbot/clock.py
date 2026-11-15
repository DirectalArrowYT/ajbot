"""The bot's notion of "now", in seconds.

Wall time by default (time.monotonic). In lockstep the game runs as fast as
it can and only advances when the bot steps it, so every timing the agent
keeps (cooldowns, hold windows, reaction delays) has to be counted in GAME
time instead: lockstep.Link calls use_frames() and now() then becomes
frames / 60.
"""
import time

FPS = 60.0
_frame_source = None


def now():
    if _frame_source is not None:
        return _frame_source() / FPS
    return time.monotonic()


def use_frames(source):
    """source() -> current game frame number; None goes back to wall time."""
    global _frame_source
    _frame_source = source


def lockstep():
    return _frame_source is not None

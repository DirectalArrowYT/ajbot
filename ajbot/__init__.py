try:
    from .pad import Pad, NEUTRAL, UP, DOWN, LEFT, RIGHT, FPS
except ImportError:
    # No vgamepad: a training-only machine (the laptop, remote_retrain.py)
    # never drives a pad, and the rest of the package does not need one.
    pass

__all__ = ["Pad", "NEUTRAL", "UP", "DOWN", "LEFT", "RIGHT", "FPS"]

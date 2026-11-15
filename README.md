# AJBot

A learning bot for MY HERO ACADEMIA All's Justice (PC). It reads the match
through a small in-game hook, plays through a virtual controller, and learns
from recordings of human play and from bot-vs-bot matches.

This repository is code only: no recordings, trained models or game assets.
You need your own copy of the game, and trained models from whoever runs
the training.

## Setup (Windows)

1. Install Python 3.12+ and run `python -m pip install -r requirements.txt`.
2. Install the ViGEmBus driver (the virtual controller).
3. Put the game-side files in `HeroGame/Binaries/Win64` (UE4SS and
   `ajhook.asi`, built from `ajhook/` with `cargo build --release` and
   renamed). `python doctor.py` checks the whole install and says what is
   missing.
4. Copy the trained models into `models/`.
5. Run the GUI (`gui/`, `cargo build --release`) or `python play.py`.

## Machine settings

Training servers and game paths live in `ajbot_local.json`. That file is
never committed. Copy `ajbot_local.example.json` to start one. You only need
it if you train remotely or the game is somewhere unusual.

## Updating

`git pull`, then rebuild the GUI and hook if `gui/` or `ajhook/` changed.

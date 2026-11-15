# AJBot

A learning bot for MY HERO ACADEMIA All's Justice (PC). It reads the match
through a small in-game hook, plays through a virtual controller, and learns
from recordings of human play and from bot-vs-bot matches.

This repository is code only: no recordings, trained models or game assets.
You need your own copy of the game. The GUI, the game-side files and (when
shared) trained models are on the Releases page.

## Setup (Windows)

1. Get this folder: `git clone https://github.com/DirectalArrowYT/ajbot`
   (or download the source zip).
2. Download `ajbot-gui.exe` from the latest release into this folder and run it.
3. The **Setup** page checks everything and has a Fix button for each problem:
   Python and its packages, the Visual C++ runtime, the ViGEmBus controller
   driver, finding the game, and installing the game-side files (ASI loader,
   UE4SS with HookLoadMap = 0, the bot's UE4SS mods, `ajhook.asi`). Close the
   game while it installs those. **Extract icons** pulls the character
   icons out of your copy of the game (`tools/iconextract`, a small
   CUE4Parse program built into each release).
4. Put a trained model in `models/` (`models/policy.pkl`).
5. **Play** page: pick a mode and press Start.

Without the GUI: `python tools/doctor.py` prints the same checklist and
`python tools/doctor.py --fix <id>` runs a fix.

## Updating

Setup page > **Update now**, or `python tools/update.py`. It runs `git pull`
and fetches a newer GUI and game files from the latest release.

## Training your own model

Everything needed to train is in this repo; it runs on your own PC.

1. Record matches: start `python bot/record.py` (or **Record me** on the Play page)
   and play normally. Each match lands in `runs/`. Bot-vs-bot matches from
   the Play page are saved to `botruns/` and are used too.
2. Press **Retrain model** on the Train page (or run `training\retrain.bat`). It digests the recordings, mines combos and
   fits a new `models/policy.pkl` (a few minutes once the caches are warm).
   The previous model is moved to `models/prev/`, so a worse retrain can be
   undone. Flags are passed on to `training/train.py`, e.g.
   `training\retrain.bat --cherrypick` for the slower, more careful fit.
3. Compare them: `python bot/play2.py --model-b models/prev/<file>` runs the
   new policy against the old one.

## Machine settings

Training servers and unusual game paths live in `ajbot_local.json`, which
is never committed (copy `ajbot_local.example.json`). With a server listed,
the Train page gets a **Server** tab: push recordings, fetch the league
champion, or run a one-off retrain over there.

## Making a release

The GUI releases itself: every push to `main` that changes `gui/` makes
GitHub Actions build `ajbot-gui.exe` and publish it as a new release
(`.github/workflows/gui-release.yml`; the newest 5 are kept).

The game files only change now and then: `python tools/make_release.py`
builds `ajbot-gamefiles-<ver>.zip` into `dist/`. Attach it to a release by
hand. The updater finds the newest release that has one, so later GUI
releases don't need to carry it.

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
4. Get a model: Train page > **Get latest model** downloads the newest
   shared one from Releases (`python tools/get_model.py`). Or train your own
   (below).
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
3. Compare them: Play page > **Bot vs Bot**, pick the old model under
   "Bot B plays" and tick **Stop after 10 matches, then compare** (Endless
   matches on, so the bots rematch by themselves). When it stops, the page
   shows both models side by side: wins, damage, KOs and combos, with a note
   on whether 10 matches are enough to call it. Without the GUI:
   `python bot/play2.py --auto --model-b models/prev/<file> --matches 10`
   (`python -m ajbot.compare` prints the last result again).

## Combo library

`combos/` holds a shared list of combos per character (`combos/<Character>.json`,
with a readable `combos/<Character>.md` page). Each combo has a tier:

- **Authored**: works every time, checked by people.
- **Potential**: works, with a catch (not near walls, needs a certain spacing,
  inconsistent timing).
- **Hypothetical**: not proven yet. The game's own CPU combo lists and routes
  the bot found go here until someone tests them.

The bot practices Authored and Potential combos alongside what it learns.
To add or report one, use the **Combo** issue form (the GUI's Combos page
opens it prefilled) or send a pull request that edits the JSON.
`python -m ajbot.combodb check` validates the files; see `combos/README.md`
for the notation.

## Shared models

Train page > **Get latest model** (or `python tools/get_model.py`) fetches
the newest `ajbot-model-*.zip` from Releases, checks every file against its
manifest and installs it. The model it replaces is kept in
`models/replaced-<date>/`.

To share a model: run `trainingelease_model.bat`. It packs `models/`
into a zip, shows what it will publish and asks before uploading it as a
`model-<date>` release (the newest 5 are kept). It signs in with your git
login for GitHub, or `GH_TOKEN` if set. `--dry-run` only builds the zip.

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

#!/usr/bin/env bash
# Linux: cross-build ajhook for Windows and install it into the Proton game
# folder as ajhook.asi. The Ultimate ASI Loader loads it, installed as
# dsound.dll: under Proton nothing loads dinput8.dll, but AJGAME.exe imports
# dsound at startup.
#
#   server/build_hook_linux.sh            build + install
#   server/build_hook_linux.sh --build    build only
#
# Needs: rustup target x86_64-pc-windows-gnu, and mingw-w64-gcc for the linker.
# Steam launch options for the game must include:
#   WINEDLLOVERRIDES="dsound,dwmapi=n,b" %command%
# (dwmapi = the UE4SS proxy that runs the CursorExport mod)
set -euo pipefail
cd "$(dirname "$0")/../ajhook"

DEFAULT_GAME="$HOME/.local/share/Steam/steamapps/common/MY HERO ACADEMIA All's Justice"
GAME="${AJ_GAME_DIR:-$DEFAULT_GAME}"
WIN64="$GAME/HeroGame/Binaries/Win64"
TARGET_DIR="${CARGO_TARGET_DIR:-$PWD/target}"

command -v x86_64-w64-mingw32-gcc >/dev/null ||
    { echo "missing x86_64-w64-mingw32-gcc: sudo pacman -S mingw-w64-gcc" >&2; exit 1; }

CARGO_TARGET_DIR="$TARGET_DIR" cargo build --release --target x86_64-pc-windows-gnu
DLL="$TARGET_DIR/x86_64-pc-windows-gnu/release/ajhook.dll"
echo "built $DLL"
[[ "${1:-}" == "--build" ]] && exit 0

if pgrep -f -i 'AJGAME.exe' >/dev/null; then
    echo "the game is running; close it first (Windows keeps the .asi locked)" >&2
    exit 1
fi
[[ -d "$WIN64" ]] || { echo "game folder not found: $WIN64 (set AJ_GAME_DIR)" >&2; exit 1; }
[[ -f "$WIN64/dsound.dll" ]] || echo "warning: no ASI loader (dsound.dll) in $WIN64"
cp "$DLL" "$WIN64/ajhook.asi"
echo "installed $WIN64/ajhook.asi"

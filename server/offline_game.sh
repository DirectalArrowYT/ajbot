#!/usr/bin/env bash
# Run All's Justice with no network access (for the box / headless bot runs).
#
# The game runs under Proton in its own network namespace that has nothing
# but a loopback device, so it cannot reach the internet or the LAN. Only
# loopback-to-loopback traffic crosses, through unix sockets (netbridge.py):
#   - the bot (host 127.0.0.1:47380) -> ajhook's port inside
#   - the game -> the Steam client's own 127.0.0.1 ports on the host
#     (steamclient talks to Steam over those; without them the game's
#     SteamAPI_Init fails and it quits)
#
#   server/offline_game.sh           start the game, returns when it exits
#   AJ_ENDLESS=1 server/offline_game.sh
#                               also run endless.py: boots straight from the
#                               title into random matches, forever (log in
#                               endless.log; extra args in AJ_ENDLESS_ARGS)
#
# Needs a running, signed-in Steam client (the game will not start without
# one) and Proton. Two ways of running, picked automatically:
#   - Flatpak Steam: the game runs as a new instance of the Steam Flatpak
#     that shares Steam's pid namespace (Steam drops game processes it cannot
#     see) with --unshare=network.
#   - Native Steam (or AJ_STEAM_DIR set to a native install): bwrap
#     --unshare-net.
#
# Env overrides: AJ_GAME_DIR, AJ_PROTON, AJ_PREFIX (Proton compat data dir),
# AJ_STEAM_DIR (Steam root), AJHOOK_PORT (host-side port, default 47380),
# AJ_GAME_ARGS (extra game command line, e.g. "-nullrhi" for no rendering or
# "-windowed -ResX=640 -ResY=360").
# Game speed: AJ_ENDLESS_ARGS="--speed 3" (see nextmatch.set_speed).
# AJ_VPADS=2: ajhook serves XInput slots 0 and 1 itself, for the lockstep
# bots (play2.py --lockstep); live from the game's first controller poll.
#
# Several copies at once: AJ_INSTANCE=N (default 0) runs copy N with its own
# prefix (<AJ_PREFIX>-N, cloned from AJ_PREFIX on first use), hook port
# 47380+N, bridge sockets and endless-N.log. The game folder is shared; the
# AJProbe mod then uses cmd-N.lua/out-N.txt. Run the bots for copy N with
# AJ_INSTANCE=N in their environment too (hook.py/nextmatch.py/ueprobe.py
# pick the port and files from it).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # the AJBot folder (this file is in server/)
NB="$HERE/server/netbridge.py"
FP_ID=com.valvesoftware.Steam
FP_STEAM="$HOME/.var/app/$FP_ID/.local/share/Steam"

if [[ -n "${AJ_STEAM_DIR:-}" ]]; then
    STEAM="$AJ_STEAM_DIR"
elif pgrep -f "$FP_STEAM/(ubuntu12_32|steamrt64)/steam " >/dev/null; then
    STEAM="$FP_STEAM"
else
    STEAM="$HOME/.local/share/Steam"
fi
FLATPAK=0
[[ "$STEAM" == "$FP_STEAM" ]] && FLATPAK=1

LIB="$STEAM/steamapps/common"
DEFAULT_GAME="$LIB/MY HERO ACADEMIA All's Justice"
GAME="${AJ_GAME_DIR:-$DEFAULT_GAME}"
PROTON="${AJ_PROTON:-$(dirname "$GAME")/Proton 11.0}"
INST="${AJ_INSTANCE:-0}"
[[ "$INST" =~ ^[0-9]+$ ]] || { echo "AJ_INSTANCE must be a number" >&2; exit 1; }
BASE_PREFIX="${AJ_PREFIX:-$(dirname "$GAME")/aj-offline-prefix}"
SFX=""; (( INST > 0 )) && SFX="-$INST"
PREFIX="$BASE_PREFIX$SFX"
PORT="${AJHOOK_PORT:-$((47380 + INST))}"
EXE="$GAME/HeroGame/Binaries/Win64/AJGAME.exe"
UIDN=$(id -u)
if (( FLATPAK )); then
    RUN="/run/user/$UIDN/app/$FP_ID/ajgame-offline$SFX"   # visible inside the Flatpak too
else
    RUN="${XDG_RUNTIME_DIR:-/run/user/$UIDN}/ajgame-offline$SFX"
fi

for c in python3 ss; do command -v $c >/dev/null || { echo "missing $c" >&2; exit 1; }; done
(( FLATPAK )) || command -v bwrap >/dev/null || { echo "missing bwrap" >&2; exit 1; }
[[ -f "$EXE" ]] || { echo "game not found: $EXE (set AJ_GAME_DIR)" >&2; exit 1; }
[[ -x "$PROTON/proton" ]] || { echo "proton not found: $PROTON (set AJ_PROTON)" >&2; exit 1; }

# The client is ubuntu12_32/steam (32-bit) or steamrt64/steam (64-bit, since
# late 2026; it runs as a wrapper plus the real client). Take the one that
# listens on loopback.
SPID=""
for p in $(pgrep -f "$STEAM/(ubuntu12_32|steamrt64)/steam " || true); do
    if ss -tlnpH | grep -q "pid=$p,"; then SPID=$p; break; fi
done
[[ -n "$SPID" ]] || { echo "Steam is not running (start it and sign in): $STEAM" >&2; exit 1; }
# Steam's loopback listeners; they change every time Steam starts.
STEAM_PORTS=$(ss -tlnpH | awk -v p="pid=$SPID," \
    '$4 ~ /^127\.0\.0\.1:/ && index($0, p) { n = split($4, a, ":"); print a[n] }' | sort -u)
[[ -n "$STEAM_PORTS" ]] || { echo "no Steam loopback ports found" >&2; exit 1; }

if [[ -n "$SFX" && ! -d "$PREFIX" ]]; then
    # Copy N starts from the main prefix (settings, saves). Reflink on btrfs.
    [[ -d "$BASE_PREFIX" ]] || { echo "no prefix to clone: $BASE_PREFIX" >&2; exit 1; }
    echo "cloning $BASE_PREFIX -> $PREFIX"
    cp -a --reflink=auto "$BASE_PREFIX" "$PREFIX"
fi
mkdir -p "$PREFIX" "$RUN"
# One launcher per copy: the lock is held by this shell alone (every child
# below gets `9>&-`). Bridges used to inherit it, so after the game was
# killed (pkill AJGAME.exe) surviving bridges held the lock and every
# relaunch was refused as "already running".
exec 9>"$RUN/lock"
flock -n 9 || { echo "game copy $INST is already running" >&2; exit 1; }
# With the lock ours, any bridge still serving this copy's sockets is a
# leftover from a game that died (it would also hold the hook port).
if pkill -f "netbridge\.py .*$RUN/" 2>/dev/null; then
    echo "stopped leftover bridges of copy $INST"
    sleep 0.3
fi
rm -f "$RUN"/*.sock

# Started from ssh: borrow the logged-in desktop session's display and audio.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$UIDN}"
if [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    export DISPLAY=:0
    auth=$(ls "$XDG_RUNTIME_DIR"/xauth_* 2>/dev/null | head -1 || true)
    [[ -n "$auth" ]] && export XAUTHORITY="$auth"
fi

HOST=(tcp2unix "$PORT" "$RUN/hook.sock")
INSIDE=(unix2tcp "$RUN/hook.sock" 47380)
for p in $STEAM_PORTS; do
    HOST+=(unix2tcp "$RUN/steam-$p.sock" "$p")
    INSIDE+=(tcp2unix "$p" "$RUN/steam-$p.sock")
done
python3 "$NB" "${HOST[@]}" 9>&- &
HOST_BRIDGE=$!
ENDLESS=""
trap 'kill $HOST_BRIDGE $ENDLESS 2>/dev/null; rm -f "$RUN"/*.sock' EXIT
sleep 0.3
if [[ -n "${AJ_ENDLESS:-}" ]]; then
    # shellcheck disable=SC2086  # AJ_ENDLESS_ARGS is a word list on purpose
    AJ_INSTANCE="$INST" AJHOOK_PORT="$PORT" AJ_PREFIX="$BASE_PREFIX" \
        python3 "$HERE/server/endless.py" ${AJ_ENDLESS_ARGS:-} >>"$HERE/endless$SFX.log" 2>&1 9>&- &
    ENDLESS=$!
    echo "endless.py running (pid $ENDLESS, log $HERE/endless$SFX.log)"
fi

ENVS=(
    STEAM_COMPAT_DATA_PATH="$PREFIX"
    STEAM_COMPAT_CLIENT_INSTALL_PATH="$STEAM"
    SteamAppId=2362050 SteamGameId=2362050
    WINEDLLOVERRIDES="dsound,dwmapi=n,b"     # ASI loader + UE4SS proxy
    AJHOOK_PORT=47380                         # inside the namespace
    AJHOOK_VPADS="${AJ_VPADS:-0}"             # virtual XInput pads (lockstep)
    AJ_INSTANCE="$INST"                       # AJProbe's cmd/out file names
)
# Inside: start the bridge, then the game through Proton's steam.exe.
INNER='
nb=$1; exe=$2; proton=$3; gargs=$4; shift 4
python3 "$nb" "$@" &
nbpid=$!
cd "$(dirname "$exe")"
# shellcheck disable=SC2086  # gargs is a word list on purpose
"$proton/proton" run "$exe" $gargs
"$proton/files/bin/wineserver" -w 2>/dev/null || true
kill "$nbpid" 2>/dev/null || true   # the bridge would outlive the game
'
echo "starting game copy $INST offline (prefix $PREFIX; Steam ports: $(echo $STEAM_PORTS); hook on 127.0.0.1:$PORT; args: ${AJ_GAME_ARGS:-none})"
if (( FLATPAK )); then
    FPENV=(); for e in "${ENVS[@]}"; do FPENV+=(--env="$e"); done
    flatpak run --parent-pid="$SPID" --parent-share-pids --unshare=network \
        --die-with-parent "${FPENV[@]}" --command=bash "$FP_ID" \
        -c "$INNER" _ "$NB" "$EXE" "$PROTON" "${AJ_GAME_ARGS:-}" "${INSIDE[@]}" 9>&-
else
    # Hide systemd-resolved's socket too so name lookups fail outright.
    NOLOOKUP=()
    [[ -d /run/systemd/resolve ]] && NOLOOKUP=(--tmpfs /run/systemd/resolve)
    env "${ENVS[@]}" bwrap --dev-bind / / --unshare-net "${NOLOOKUP[@]}" \
        --die-with-parent -- bash -c "$INNER" _ "$NB" "$EXE" "$PROTON" "${AJ_GAME_ARGS:-}" "${INSIDE[@]}" 9>&-
fi

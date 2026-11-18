#!/usr/bin/env bash
# build.sh: build ../ajdirect.asi. Needs a Rust toolchain with the x86_64-pc-windows-gnu target (and mingw-w64 for linking).
# The hook addresses are fixed RVAs for one AJGAME.exe build (see the RVA_* constants in src/lib.rs); on another build
# the mod checks the bytes at each address, patches nothing and logs "NOT applied".
set -euo pipefail
cd "$(dirname "$0")"
export RUSTFLAGS="--remap-path-prefix=$HOME=~ --remap-path-prefix=$PWD=."
cargo build --release --target x86_64-pc-windows-gnu
cp target/x86_64-pc-windows-gnu/release/ajdirect.dll ../ajdirect.asi
echo "built ../ajdirect.asi"

#!/usr/bin/env bash
# build.sh /path/to/AJGAME.exe: regenerate src/blob.bin + src/patches.bin for this game build, then build ajfpfix.asi.
# Needs binutils (objdump, as, ld, objcopy), python3, and a Rust toolchain with the x86_64-pc-windows-gnu target.
# The AMD tables (gen/amd_*.bin) come from a Zen 5 CPU via dump.c + compact.py; verify.c/asmtest.c check emu.h/emu.s.
set -euo pipefail
EXE=$(realpath "$1"); cd "$(dirname "$0")/gen"
export AJ_EXE=$EXE
objdump -d --no-show-raw-insn "$EXE" > aj.dis
python3 parse.py aj.dis
python3 filter_sites.py
python3 gen.py
cp blob.bin patches.bin ../src/
rm -f aj.dis sites.json sites_real.json abs.ld blob.s blob.o blob.elf blob.bin patches.bin gen_report.txt
cd ..; RUSTFLAGS="--remap-path-prefix=$HOME=~ --remap-path-prefix=$PWD=." cargo build --release --target x86_64-pc-windows-gnu
cp target/x86_64-pc-windows-gnu/release/ajfpfix.dll ../ajfpfix.asi
echo "built ../ajfpfix.asi"

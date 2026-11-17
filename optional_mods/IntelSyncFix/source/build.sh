#!/usr/bin/env bash
# build.sh /path/to/AJGAME.exe: regenerate the stub blobs + patch list for this game build, then build both ASIs:
#   ../ajfpfix.asi            for Intel players: Intel computes like AMD (gen/tables/amd, from a Zen 5 CPU)
#   ../ajfpfix_intelmath.asi  for AMD players:   AMD computes like Intel (gen/tables/intel, from an i7-9700K)
# Needs binutils (objdump, as, ld, objcopy), python3, and a Rust toolchain with the x86_64-pc-windows-gnu target.
# Tables come from dump.c + compact.py; verify.c/asmtest.c check emu.h/emu.s against the CPU (run in a tables dir).
set -euo pipefail
EXE=$(realpath "$1"); cd "$(dirname "$0")/gen"
export AJ_EXE=$EXE
objdump -d --no-show-raw-insn "$EXE" > aj.dis
python3 parse.py aj.dis
python3 filter_sites.py
AJ_TABLES=tables/intel python3 gen.py; mv blob.bin ../src/blob_intel.bin; mv patches.bin patches_intel.bin
AJ_TABLES=tables/amd python3 gen.py; cp blob.bin patches.bin ../src/
cmp patches.bin patches_intel.bin     # same stub layout, only the table bytes differ
rm -f aj.dis sites.json sites_real.json abs.ld blob.s blob.o blob.elf blob.bin patches.bin patches_intel.bin gen_report.txt
cd ..
export RUSTFLAGS="--remap-path-prefix=$HOME=~ --remap-path-prefix=$PWD=."
cargo build --release --target x86_64-pc-windows-gnu
cp target/x86_64-pc-windows-gnu/release/ajfpfix.dll ../ajfpfix.asi
cargo build --release --target x86_64-pc-windows-gnu --features intel_math
cp target/x86_64-pc-windows-gnu/release/ajfpfix.dll ../ajfpfix_intelmath.asi
echo "built ../ajfpfix.asi ../ajfpfix_intelmath.asi"

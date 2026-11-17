"""compact.py DIR: shrink dump.c's full tables (DIR/rcp.bin, DIR/rsqrt.bin) to the 4096/8192-entry tables emu.s uses.
rcp depends on the top 12 mantissa bits only; rsqrt on the exponent's low bit plus the top 12 mantissa bits."""
import sys, numpy as np
d = sys.argv[1] if len(sys.argv) > 1 else '.'
r = np.fromfile(f'{d}/rcp.bin', np.uint32); s = np.fromfile(f'{d}/rsqrt.bin', np.uint32)
r[np.arange(4096) << 11].tofile('amd_rcp4096.bin')
i = np.arange(8192); s[((i >> 12) << 23) | ((i & 4095) << 11)].tofile('amd_rsqrt8192.bin')
print('wrote amd_rcp4096.bin amd_rsqrt8192.bin')

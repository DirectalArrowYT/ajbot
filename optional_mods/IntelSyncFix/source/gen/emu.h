// emu.h: rcpps/rsqrtps per-lane results from 4096/8192-entry tables (inputs [1,2) and [1,4)); AMD Zen 5 or Intel tables.
#include <stdint.h>
static uint32_t T_RCP[4096], T_RSQ[8192];
static inline uint32_t emu_rcp(uint32_t x){
  uint32_t s=x&0x80000000u, e=(x>>23)&0xff, m=x&0x7fffff;
  if(e==0) return s|0x7f800000u;                 // zero/denormal -> inf
  if(e==255) return m? (x|0x400000u) : s;        // NaN quiet / inf -> 0
  uint32_t t=T_RCP[m>>11]; int32_t re=(int32_t)((t>>23)&0xff) - ((int32_t)e-127);
  if(re<=0) return s;                            // tiny result flushed
  return s | ((uint32_t)re<<23) | (t&0x7fffff);
}
static inline uint32_t emu_rsq(uint32_t x){
  uint32_t s=x&0x80000000u, e=(x>>23)&0xff, m=x&0x7fffff;
  if(e==0) return s|0x7f800000u;
  if(e==255){ if(m) return x|0x400000u; return s? 0xffc00000u : 0; }
  if(s) return 0xffc00000u;
  int32_t k=((int32_t)e-127)>>1;                 // x = base * 4^k, base in [1,4)
  uint32_t idx=(((e-127)&1)<<12) | (m>>11);
  uint32_t t=T_RSQ[idx];
  return t - ((uint32_t)k<<23);
}

// verify.c: compare emu.h against this CPU over every 32-bit input. Run it inside tables/amd or tables/intel (reads rcp4096.bin, rsqrt8192.bin).
#include <immintrin.h>
#include <stdio.h>
#include <string.h>
#include "emu.h"
int main(){
  FILE*f=fopen("rcp4096.bin","rb"); fread(T_RCP,4,4096,f); fclose(f);
  f=fopen("rsqrt8192.bin","rb"); fread(T_RSQ,4,8192,f); fclose(f);
  unsigned long br=0,bs=0; uint32_t fr=0,fs=0;
  for(uint64_t x=0;x<(1ull<<32);x+=4){
    __m128i v=_mm_add_epi32(_mm_set1_epi32((uint32_t)x),_mm_setr_epi32(0,1,2,3));
    uint32_t r[4],q[4]; _mm_storeu_si128((__m128i*)r,_mm_castps_si128(_mm_rcp_ps(_mm_castsi128_ps(v))));
    _mm_storeu_si128((__m128i*)q,_mm_castps_si128(_mm_rsqrt_ps(_mm_castsi128_ps(v))));
    for(int i=0;i<4;i++){ uint32_t xi=(uint32_t)x+i;
      if(emu_rcp(xi)!=r[i]){ if(!br) fr=xi; br++; }
      if(emu_rsq(xi)!=q[i]){ if(!bs) fs=xi; bs++; } }
  }
  printf("rcp mismatches %lu (first %08x)  rsqrt mismatches %lu (first %08x)\n",br,fr,bs,fs);
  if(br){ printf("  rcp first: emu %08x hw ?\n", emu_rcp(fr)); }
  return 0;
}

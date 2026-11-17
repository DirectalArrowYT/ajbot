#include <stdio.h>
#include "emu_c.h"
extern char a_rcp[] __asm__("emu_rcp"), a_rsq[] __asm__("emu_rsq");
static inline uint32_t call(void*f,uint32_t x){ uint32_t r;
  __asm__ volatile("call *%2" : "=a"(r) : "a"(x), "r"(f) : "rcx","rdx","r8","cc","memory"); return r; }
int main(){ FILE*f=fopen("amd_rcp4096.bin","rb"); fread(C_RCP,4,4096,f); fclose(f);
  f=fopen("amd_rsqrt8192.bin","rb"); fread(C_RSQ,4,8192,f); fclose(f);
  unsigned long a=0,b=0; for(uint64_t x=0;x<(1ull<<32);x++){ uint32_t v=(uint32_t)x;
    if(call(a_rcp,v)!=c_rcp(v)) a++; if(call(a_rsq,v)!=c_rsq(v)) b++; }
  printf("asm vs C: rcp %lu rsqrt %lu mismatches\n",a,b); }

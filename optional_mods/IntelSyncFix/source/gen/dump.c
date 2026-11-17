// dump.c: write the CPU's rcpps results for [1,2) and rsqrtps results for [1,4) as raw uint32, and test exponent scaling.
#include <immintrin.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
static uint32_t f2u(float f){uint32_t u;memcpy(&u,&f,4);return u;}
static float u2f(uint32_t u){float f;memcpy(&f,&u,4);return f;}
static uint32_t rcp(uint32_t x){return f2u(_mm_cvtss_f32(_mm_rcp_ps(_mm_set1_ps(u2f(x)))));}
static uint32_t rsq(uint32_t x){return f2u(_mm_cvtss_f32(_mm_rsqrt_ps(_mm_set1_ps(u2f(x)))));}
int main(int c,char**v){
  uint32_t *a=malloc(4u<<24); FILE*f;
  for(uint32_t m=0;m<(1u<<23);m++) a[m]=rcp(0x3f800000u|m);
  f=fopen("rcp.bin","wb"); fwrite(a,4,1u<<23,f); fclose(f);
  for(uint32_t m=0;m<(1u<<24);m++) a[m]=rsq(0x3f800000u+m);
  f=fopen("rsqrt.bin","wb"); fwrite(a,4,1u<<24,f); fclose(f);
  // exponent check: rcp(x*2^k) == rcp(x)*2^-k, rsqrt(x*4^k) == rsqrt(x)*2^-k, over normal ranges
  long bad=0, n=0; srand(1);
  for(int k=-60;k<=60;k++) for(int t=0;t<20000;t++){
    uint32_t m=((uint32_t)rand()<<8 ^ rand()) & 0x7fffff;
    uint32_t x=((uint32_t)(127+k)<<23)|m, r=rcp(x), e=rcp(0x3f800000u|m);
    uint32_t want=e - ((uint32_t)k<<23); n++; if(r!=want) bad++;
    uint32_t xs=((uint32_t)(127+2*k)<<23)|m, rs=rsq(xs), es=rsq(0x3f800000u|m);
    uint32_t wants=es - ((uint32_t)k<<23); n++; if(rs!=wants) bad++;
  }
  printf("exponent check: %ld/%ld mismatches\n",bad,n);
  return 0;
}

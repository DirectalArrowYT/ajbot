"""parse.py: stream aj.dis -> sites.json (each rcp/rsqrt site with the 4 instructions before and 6 after) + branch target set."""
import re,json,sys
DIS=sys.argv[1] if len(sys.argv)>1 else "aj.dis"  # objdump -d --no-show-raw-insn AJGAME.exe > aj.dis
OPS=re.compile(r'^(v?rcpps|v?rsqrtps|v?rsqrtss|v?rcpss)$')
line_re=re.compile(r'^\s+([0-9a-f]+):\t(.*)$')
insns=[]  # rolling window
targets=set(); sites=[]; pending=[]
cur=None
def flush(ins):
    insns.append(ins)
    if len(insns)>12: insns.pop(0)
    for s in pending[:]:
        s['after'].append(ins)
        if len(s['after'])>=6: sites.append(s); pending.remove(s)
    mn=ins['t'].split()[0] if ins['t'] else ''
    if OPS.match(mn):
        pending.append({'addr':ins['a'],'b':ins['b'],'t':ins['t'],'before':insns[-5:-1],'after':[]})
    m=re.match(r'^(j[a-z]+|call|loop[a-z]*|jrcxz)\s+(?:0x)?([0-9a-f]+)\b',ins['t'] or '')
    if m: targets.add(int(m.group(2),16))
for l in open(DIS,errors='replace'):
    m=line_re.match(l)
    if not m: continue
    a=int(m.group(1),16); t=m.group(2)
    if cur: cur['n']=a-cur['a']; flush(cur)
    cur={'a':a,'b':None,'t':t.strip()}
if cur: flush(cur)
json.dump({'sites':sites,'targets':sorted(targets)},open('sites.json','w'))
print(len(sites),"sites",len(targets),"targets")

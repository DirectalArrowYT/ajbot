"""filter_sites.py: sites.json -> sites_real.json. Linear disassembly also decodes data and the Steam stub (.bind) as code;
patching those breaks the game. Keep only sites inside .text whose neighbours look like real code."""
import json, re
from pe import PE
pe = PE(); d = json.load(open('sites.json'))
name, va, vs, rp, rs = [s for s in pe.secs if s[0] == '.text'][0]
lo, hi = pe.base + va, pe.base + va + vs
bad = re.compile(r'^(in|out|ins|outs|hlt|cli|sti|iret|lods|stos|scas|cmps|int|into|enter|les|lds|ljmp|lcall|lret|cmc|clc|stc|std|cld|loop|\(bad\)|\.byte)\b')
keep, drop = [], []
for s in d['sites']:
    ctx = [x['t'] for x in s.get('before', [])] + [x['t'] for x in s['after']]
    (drop if not (lo <= s['addr'] < hi) or any(bad.match(t) for t in ctx) else keep).append(s)
print(len(keep), 'kept; dropped', [hex(s['addr']) for s in drop])
d['sites'] = keep; json.dump(d, open('sites_real.json', 'w'))

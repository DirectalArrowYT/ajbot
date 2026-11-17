"""gen.py: build the ajfpfix stub blob + site patch list from sites.json.

Each rcp/rsqrt site (plus whole following instructions until >= 5 bytes) is replaced by `jmp stub` (+ nops).
The stub runs the table emulation (tables/amd by default, AJ_TABLES picks another) for the rcp/rsqrt instruction, replays the displaced instructions
(as reassembled from objdump text, RIP-relative operands and branch targets as absolute symbols), and jumps back.
The blob is linked to sit at IMAGE_BASE + BLOB_RVA, so every distance is fixed no matter where the exe loads.
Outputs: blob.bin, patches.bin (records: rva u32, len u8, orig[len], patch[len]), gen_report.txt.
"""
import json, re, struct, subprocess, os, sys
from pe import PE

BASE = 0x140000000
BLOB_RVA = 0xb320000
BLOB = BASE + BLOB_RVA
ADJ = 0x108          # bytes the stub moves rsp down before touching memory operands (lea -0x100 + pushfq)

pe = PE()
d = json.load(open('sites_real.json'))
targets = set(d['targets'])

def split_ops(s):
    out, depth, cur = [], 0, ''
    for ch in s:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            out.append(cur.strip()); cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out

absyms = {}
def fix_rip(text):
    """`0x76b4aff(%rip)        # 0x147759fe0` -> `A_147759fe0(%rip)`; strip comments."""
    m = re.search(r'#\s*0x([0-9a-f]+)', text)
    body = text.split('#')[0].strip()
    if '(%rip)' in body:
        if not m:
            raise ValueError('rip without comment: ' + text)
        sym = 'A_' + m.group(1)
        absyms[sym] = int(m.group(1), 16)
        body = re.sub(r'-?0x[0-9a-f]+\(%rip\)|\(%rip\)', sym + '(%rip)', body)
    return body

def fix_branch(text):
    """objdump `jne 0x140008b3f <sym+0x..>` / `call 0x14...` -> absolute symbol."""
    body = text.split('#')[0].strip()
    body = re.sub(r'\s*<[^>]*>', '', body)
    m = re.match(r'^(j[a-z]+|call|jrcxz|loop[a-z]*)\s+(?:0x)?([0-9a-f]+)$', body)
    if m:
        sym = 'A_' + m.group(2)
        absyms[sym] = int(m.group(2), 16)
        return f'{m.group(1)} {sym}'
    return body

BAD_FOLLOW = re.compile(r'^(?:loop|jrcxz|jecxz|bnd|notrack|\(bad\)|\.byte|rex|data16|addr32)')

def plan(site):
    """Return (start_addr, total_len, displaced_list) or raise."""
    a, n = site['addr'], site['after'][0]['a'] - site['addr'] if site['after'] else None
    seq = [{'a': site['addr'], 't': site['t'], 'n': n}] + site['after']
    # lengths: each entry's n is next.a - a (after entries carry 'n')
    total, used = 0, []
    for i, ins in enumerate(seq):
        if i > 0 and ins['a'] in targets:
            raise ValueError(f"branch target inside patch at {ins['a']:#x}")
        if i > 0 and BAD_FOLLOW.match(ins['t']):
            raise ValueError('odd prefix')
        used.append(ins)
        total += ins['n']
        if total >= 5:
            return used, total
    raise ValueError('not enough room')

def emu_block(t):
    """Assembly for the rcp/rsqrt instruction itself."""
    mn, ops = t.split(None, 1)
    ops = split_ops(t.split(None, 1)[1].split('#')[0])
    vex = mn.startswith('v')
    kind = 'rcp' if 'rcp' in mn else 'rsq'
    ss = mn.endswith('ss')
    src, dst = ops[0], ops[-1]
    v = ops[1] if (vex and ss) else None
    ymm = dst.startswith('%ymm')
    lanes = 1 if ss else (8 if ymm else 4)
    mov = 'vmovdqu' if vex else 'movdqu'
    L = ['lea -0x100(%rsp),%rsp', 'pushfq',
         'mov %rax,0x08(%rsp)', 'mov %rcx,0x10(%rsp)', 'mov %rdx,0x18(%rsp)', 'mov %r8,0x20(%rsp)', 'mov %r9,0x28(%rsp)']
    if src.startswith('%'):
        L += [f'{mov} {src},0x40(%rsp)', 'lea 0x40(%rsp),%r9']
    else:
        mem = fix_rip(src + ('  ' + t[t.index('#'):] if '#' in t else ''))
        if '%rsp' in mem:
            m = re.match(r'^(-?0x[0-9a-f]+|-?\d+)?\(%rsp(.*)$', mem)
            if not m:
                raise ValueError('rsp operand ' + mem)
            disp = int(m.group(1), 0) if m.group(1) else 0
            mem = f'{disp + ADJ:#x}(%rsp{m.group(2)}'
        L += [f'lea {mem},%r9']
    if ss:
        init = v if vex else dst
        L += [f'{mov} {init},0x60(%rsp)']
    for i in range(lanes):
        L += [f'mov {4*i:#x}(%r9),%eax', f'call emu_{kind}', f'mov %eax,{0x60 + 4*i:#x}(%rsp)']
    L += [f'{mov} 0x60(%rsp),{dst}']
    L += ['mov 0x08(%rsp),%rax', 'mov 0x10(%rsp),%rcx', 'mov 0x18(%rsp),%rdx', 'mov 0x20(%rsp),%r8', 'mov 0x28(%rsp),%r9',
          'popfq', 'lea 0x100(%rsp),%rsp']
    return L

def main():
    asm = ['\t.text', '\t.globl _start', '_start:', '\t.include "emu.s"']
    recs, report, skipped = [], [], []
    consumed = set()
    for k, s in enumerate(sorted(d['sites'], key=lambda s: s['addr'])):
        if s['addr'] in consumed:
            continue
        try:
            used, total = plan(s)
            body = emu_block(s['t'])
            for ins in used[1:]:
                tx = ins['t']
                if re.match(r'^v?(rcp|rsqrt)(ps|ss)\b', tx):
                    body += emu_block(tx)       # neighbouring site folded into this stub
                    consumed.add(ins['a'])
                    continue
                tx = fix_rip(tx) if '(%rip)' in tx else fix_branch(tx)
                body.append(tx)
            end = s['addr'] + total
            sym = 'A_%x' % end; absyms[sym] = end
            body.append(f'jmp {sym}')
        except Exception as e:
            skipped.append((s['addr'], s['t'], str(e)))
            continue
        asm.append('\t.balign 16')
        asm.append(f'S_{s["addr"]:x}:')
        asm += ['\t' + x for x in body]
        recs.append((s['addr'], total, [u['t'] for u in used]))
    open('abs.ld', 'w').write(''.join(f'{sym} = {val:#x};\n' for sym, val in sorted(absyms.items())))
    open('blob.s', 'w').write('\n'.join(asm) + '\n')
    r = subprocess.run(['as', '--64', '-I', os.environ.get('AJ_TABLES', 'tables/amd'), '-o', 'blob.o', 'blob.s'], capture_output=True, text=True)
    if r.returncode:
        print(r.stderr[:3000]); sys.exit(1)
    subprocess.run(['ld', '-o', 'blob.elf', f'-Ttext={BLOB:#x}', '-e', '_start', 'blob.o', 'abs.ld'], check=True)
    subprocess.run(['objcopy', '-O', 'binary', '-j', '.text', 'blob.elf', 'blob.bin'], check=True)
    syms = {}
    for l in subprocess.run(['nm', 'blob.elf'], capture_output=True, text=True).stdout.splitlines():
        p = l.split()
        if len(p) == 3 and p[2].startswith('S_'):
            syms[int(p[2][2:], 16)] = int(p[0], 16)
    out = bytearray()
    for addr, total, texts in recs:
        orig = pe.read(addr, total)
        rel = syms[addr] - (addr + 5)
        patch = b'\xe9' + struct.pack('<i', rel) + b'\x90' * (total - 5)
        out += struct.pack('<IB', addr - BASE, total) + orig + patch
        report.append(f'{addr:#x} len {total} -> {syms[addr]:#x}: ' + ' ; '.join(texts))
    open('patches.bin', 'wb').write(out)
    bl = os.path.getsize('blob.bin')
    report.insert(0, f'{len(recs)} sites patched, {len(skipped)} skipped, blob {bl} bytes at rva {BLOB_RVA:#x}')
    report += ['SKIPPED ' + f'{a:#x} {t} : {e}' for a, t, e in skipped]
    open('gen_report.txt', 'w').write('\n'.join(report) + '\n')
    print(report[0]); [print('  skip', f'{a:#x}', t, e) for a, t, e in skipped[:20]]

main()

"""pe.py: tiny PE reader for AJGAME.exe (image base, size, section map, read bytes at VA)."""
import struct
import os
EXE=os.environ.get("AJ_EXE", "AJGAME.exe")  # set AJ_EXE to the game exe
class PE:
    def __init__(s,p=EXE):
        s.d=open(p,'rb').read(); d=s.d
        o=struct.unpack_from('<I',d,0x3c)[0]; ns=struct.unpack_from('<H',d,o+6)[0]; osz=struct.unpack_from('<H',d,o+20)[0]
        opt=o+24; s.base=struct.unpack_from('<Q',d,opt+24)[0]; s.size=struct.unpack_from('<I',d,opt+56)[0]
        s.dllchar=struct.unpack_from('<H',d,opt+70)[0]
        s.secs=[]
        for i in range(ns):
            h=opt+osz+40*i; name=d[h:h+8].rstrip(b'\0').decode(); vs,va,rs,rp=struct.unpack_from('<IIII',d,h+8)
            s.secs.append((name,va,vs,rp,rs))
    def read(s,va,n):
        rva=va-s.base
        for name,sva,vs,rp,rs in s.secs:
            if sva<=rva<sva+max(vs,rs): return s.d[rp+rva-sva:rp+rva-sva+n]
        raise KeyError(hex(va))
if __name__=="__main__":
    p=PE(); print(hex(p.base),hex(p.size),hex(p.dllchar)); [print(x[0],hex(x[1]),hex(x[2])) for x in p.secs]

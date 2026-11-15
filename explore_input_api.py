"""Search the game's own Lua state for an input/button API, the same way
the resident sampler already searches for character-identity tables and
move-speed getters (ajbot/sampler.py's RESIDENT chunk).

Reading "was this button actually pressed" from the game's own Lua state -
if it tracks that at all - would be far more reliable than guessing from
raw XInput: it works the same whether the recorded player is 1P or 2P,
local or online, controller or keyboard, since it reflects what the game
itself already processed rather than something read independently of it.

    python explore_input_api.py

Run this WHILE A MATCH IS LIVE (character on screen, actively playing) -
each eval() lands on whichever character's lua_State happens to be pumping
at that instant, so this polls several times to cover more than one.
Paste the output back - it's the map for deciding what the resident
sampler chunk should actually read going forward.
"""
import time

from ajbot.hook import Hook, HookError

SCAN = r"""
local names = {}
local seen = {}
local function scan(tbl, prefix)
  if type(tbl) ~= 'table' then return end
  for k, v in pairs(tbl) do
    if type(k) == 'string' then
      local lk = k:lower()
      if lk:find('input') or lk:find('button') or lk:find('key') or lk:find('pad')
         or lk:find('press') or lk:find('command') or lk:find('lever') or lk:find('stick')
         or lk:find('trigger') or lk:find('analog') or lk:find('control') then
        local line = prefix .. k .. '=' .. type(v)
        if not seen[line] then
          seen[line] = true
          names[#names+1] = line
        end
      end
    end
  end
end
scan(_G, '')
for _, ns in ipairs({'move','action','damage','animation','input','pad',
                      'controller','key','system','lever','operation'}) do
  scan(rawget(_G, ns), ns .. '.')
end
table.sort(names)
return table.concat(names, '\n')
"""

CHAR_ID = r"""
for k, v in pairs(_G) do
  if type(k) == 'string' and type(v) == 'table' and k:match('^Pl%d+%u') then
    return k
  end
end
return '?'
"""


def main():
    try:
        hook = Hook()
    except HookError as e:
        print(e)
        return 1
    if not hook.has_state():
        print("not in a battle - start or join a live match first, then re-run this")
        return 1

    found = {}
    print("polling across several ticks to see more than one character's state...")
    for i in range(30):
        try:
            who = hook.eval(CHAR_ID)
            out = hook.eval(SCAN)
        except HookError as e:
            print(f"  [{i}] eval failed: {e}")
            time.sleep(0.1)
            continue
        for line in out.splitlines():
            if line.strip():
                found.setdefault(line.strip(), set()).add(who)
        time.sleep(0.1)

    hook.close()
    if not found:
        print("\nnothing matched - no global or known namespace had an "
              "input/button/key/pad/press/command/lever/stick-like name")
        return 0

    print(f"\n{len(found)} candidate(s), with which character(s) they showed up on:")
    for name in sorted(found):
        print(f"  {name:<40} seen on: {', '.join(sorted(found[name]))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

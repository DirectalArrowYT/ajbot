-- AJProbe: run Lua on the Unreal side of the game from outside it.
--
-- ajhook's eval reaches the game's own Lua (character battle scripts, only
-- during a match). Menus, match setup and everything else in Unreal are only
-- reachable through UE4SS, so this mod gives that side the same kind of
-- eval, over files instead of a pipe (works the same under Proton and in the
-- offline sandbox, nothing to bridge):
--
--   write <mod dir>/cmd.lua   a Lua chunk (UE4SS API: FindAllOf,
--                             StaticFindObject, DumpAllObjects, GenerateSDK...)
--   read  <mod dir>/out.txt   its result, once cmd.lua has been consumed
--
-- The chunk runs on the game thread. Inside it, out(...) appends a line to
-- the result; whatever the chunk returns is appended last. ueprobe.py drives
-- this from the host.
local dir = debug.getinfo(1, "S").source:gsub("^@", ""):gsub("[/\\]Scripts[/\\]main%.lua$", "")
-- Several game copies share one game folder (one per AJ_INSTANCE, see
-- offline_game.sh): copy N > 0 uses cmd-N.lua / out-N.txt.
local INST = os.getenv("AJ_INSTANCE") or ""
local SUFFIX = (INST ~= "" and INST ~= "0") and ("-" .. INST) or ""
local CMD = dir .. "\\cmd" .. SUFFIX .. ".lua"
local OUT = dir .. "\\out" .. SUFFIX .. ".txt"
local busy = false

local function write_out(text)
    local tmp = OUT .. ".tmp"
    local f = io.open(tmp, "wb")
    if not f then return end
    f:write(text)
    f:close()
    os.remove(OUT)
    os.rename(tmp, OUT)
end

local function run(src)
    local lines = {}
    local env = setmetatable({
        out = function(...)
            local parts = {}
            for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
            lines[#lines + 1] = table.concat(parts, "\t")
        end,
    }, { __index = _G })
    local chunk, err = load(src, "=cmd", "t", env)
    if not chunk then
        lines[#lines + 1] = "!compile: " .. tostring(err)
    else
        local res = table.pack(pcall(chunk))
        if not res[1] then
            lines[#lines + 1] = "!error: " .. tostring(res[2])
        else
            for i = 2, res.n do lines[#lines + 1] = tostring(res[i]) end
        end
    end
    write_out(table.concat(lines, "\n") .. "\n")
    busy = false
end

LoopAsync(200, function()
    if busy then return false end
    local f = io.open(CMD, "rb")
    if not f then return false end
    local src = f:read("a")
    f:close()
    os.remove(CMD)
    busy = true
    ExecuteInGameThread(function() run(src) end)
    return false
end)

print("[AJProbe] watching " .. CMD .. "\n")

local ok, err = pcall(function() require("trace").start(dir, SUFFIX) end)
if not ok then print("[AJProbe] tracer failed: " .. tostring(err) .. "\n") end

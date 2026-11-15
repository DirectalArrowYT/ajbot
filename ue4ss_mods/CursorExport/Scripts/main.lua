-- Pushes the character-select cursor to ajhook (named pipe, opcode 'P').
local PIPE = [[\\.\pipe\ajhook]]
local latest = "-"
local sent, sent_at = nil, 0
local warned = false

local function num(v)
    if v == true then return 1 end
    if v == false or v == nil then return 0 end
    return tonumber(tostring(v)) or 0
end

local function send(payload)
    local f, err = io.open(PIPE, "r+b")
    if not f then return false, err end
    f:setvbuf("no")
    local body = "P" .. payload
    local ok, e = pcall(function()
        f:write(#body .. "\n" .. body)
        f:flush()
        local hdr = f:read("l")
        if hdr then f:read(tonumber(hdr) or 0) end
    end)
    f:close()
    return ok, e
end

local function sample()
    local ok, objs = pcall(FindAllOf, "VsCharSelectControl")
    if not (ok and objs) then latest = "-"; return end
    for _, o in ipairs(objs) do
        local okw, w = pcall(function() return o.WidgetVsCharSelect end)
        if okw and w and w:IsValid() then
            -- ajhook reads both players' SelectInfo straight from this address
            local oka, addr = pcall(function() return w:GetAddress() end)
            if oka and addr then
                latest = string.format("addr=%.0f", num(addr))
                -- save object address, for debug reads of persisted settings
                local okf, slw = pcall(FindFirstOf, "SaveLoadWork")
                if okf and slw and slw:IsValid() then
                    local oks, sdaddr = pcall(function() return slw.SaveData:GetAddress() end)
                    if oks and sdaddr then latest = latest .. string.format(" sd=%.0f", num(sdaddr)) end
                end
                return
            end
        end
    end
    latest = "-"
end

LoopAsync(100, function()
    ExecuteInGameThread(sample)
    local now = os.time()
    if latest ~= sent or now - sent_at >= 1 then
        if not (latest == "-" and sent == "-" ) or now - sent_at >= 1 then
            local ok, err = send(latest)
            if ok then
                sent, sent_at = latest, now
                warned = false
            elseif not warned then
                warned = true
                print("[CursorExport] pipe send failed: " .. tostring(err) .. "\n")
            end
        end
    end
    return false
end)
print("[CursorExport] loaded\n")

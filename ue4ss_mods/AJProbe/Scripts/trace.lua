-- AJProbe tracer: logs how the game moves between screens and into a match,
-- to <mod dir>\trace.log. Used to learn the battle setup flow (which strategy
-- runs, what the VS select control holds, which map loads with which URL) so
-- a match can later be started without the menus.
--
-- Logged:
--   map      every map load (URL), before and after
--   new      new game-mode strategy / battle game state objects
--   call     VsCharSelectControl and OpenLevel calls
--   state    a snapshot of the screen/VS/battle state, whenever it changes
local M = {}

local function safe(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
    return nil
end

local function cname(o)
    return safe(function() return o:GetClass():GetFName():ToString() end) or "?"
end

local function valid(o)
    return o ~= nil and safe(function() return o:IsValid() end) == true
end

function M.start(dir, suffix)
    local LOG = dir .. "\\trace" .. (suffix or "") .. ".log"
    local t0 = os.clock()

    local function log(kind, ...)
        local parts = {}
        for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
        local f = io.open(LOG, "ab")
        if not f then return end
        f:write(string.format("%s %8.2f %-5s %s\n", os.date("%H:%M:%S"), os.clock() - t0, kind,
            table.concat(parts, " ")))
        f:close()
    end

    local function vs_state()
        local vs = FindFirstOf("VsCharSelectControl")
        if not valid(vs) then return "vs=none" end
        local s = { "vs:" }
        local function add(k, f) s[#s + 1] = k .. "=" .. tostring(safe(f)) end
        add("mode", function() return vs.VsCharMode end)
        add("type", function() return vs.VsCharType end)
        add("st", function() return vs:GetState() end)
        add("conf", function() return vs.bConfirmedVsCharSelect end)
        add("selType", function() return vs.VsCharSelectType end)
        add("team", function() return vs.VsCharSelectTeam end)
        add("stage", function() return vs.SaveStageSelectInfo.StageId end)
        add("ruleStage", function() return vs.SaveRuleSelectInfo.StageId end)
        add("cat", function() return vs.SaveRuleSelectInfo.SelectCategory end)
        add("p1done", function() return vs.SaveCharSelectInfo.bComplete end)
        add("p1rand", function() return vs.SaveCharSelectInfo.bRandom end)
        add("p1banners", function()
            local ids = {}
            vs.SaveCharSelectInfo.SelectBannerList:ForEach(function(_, e) ids[#ids + 1] = tostring(e:get()) end)
            return table.concat(ids, ",")
        end)
        add("fixed", function() return vs.SaveVsCharOption.bUseFixedID end)
        add("cont", function() return vs.SaveVsCharOption.bBattleContinue end)
        add("before", function()
            local ids = {}
            vs.SaveBeforeBattleCharacterId:ForEach(function(_, e) ids[#ids + 1] = tostring(e:get()) end)
            return table.concat(ids, ",")
        end)
        return table.concat(s, " ")
    end

    local function battle_state()
        local gs = FindFirstOf("GameStateBattle")
        if not valid(gs) then return "" end
        local s = { "battle:" }
        local function add(k, f) s[#s + 1] = k .. "=" .. tostring(safe(f)) end
        add("stage", function() return gs.NowSelectedStage end)
        add("sel", function() return gs.SelectedStage end)
        add("rounds", function() return gs.NowBattleRule.VictoryRound end)
        add("time", function() return gs.NowBattleRule.RoundTime end)
        add("chars", function()
            local sides = {}
            gs.NowCharacterInfo:ForEach(function(_, info)
                local ids = {}
                info:get().SettingCharacterDatas:ForEach(function(_, d) ids[#ids + 1] = tostring(d:get().SelectCharacter) end)
                sides[#sides + 1] = table.concat(ids, ",")
            end)
            return table.concat(sides, " vs ")
        end)
        return table.concat(s, " ")
    end

    local function snapshot()
        local gs = FindFirstOf("HeroGameStateBase")
        local where = valid(gs) and (cname(gs) .. "@" .. (safe(function()
            return gs:GetFullName():match("^%S+%s+([^:]+)")
        end) or "?")) or "none"
        local strat = ""
        if valid(gs) then
            local s = safe(function() return gs.GameModeStrategy end)
            if valid(s) then strat = " strategy=" .. cname(s) end
        end
        return where .. strat .. " | " .. vs_state() .. " " .. battle_state()
    end

    -- Function hooks. RegisterHook needs the function to exist already;
    -- script classes do from the start.
    local hooks = {
        "/Script/BattleModule.VsCharSelectControl:OpenVsCharSetting",
        "/Script/BattleModule.VsCharSelectControl:CloseVsCharSetting",
        "/Script/BattleModule.VsCharSelectControl:OnPostLoadMapWithWorld",
        "/Script/BattleModule.VsCharSelectControl:OnExclusiveSubLevelShown",
        "/Script/Engine.GameplayStatics:OpenLevel",
        "/Script/Engine.GameplayStatics:OpenLevelBySoftObjectPtr",
        "/Script/OutgameModule.GameStateOutGame:OnFinishedLoadStreamLevel",
        "/Script/OutgameModule.GameStateOutGame:OnFinishedUnLoadStreamLevel",
    }
    for _, path in ipairs(hooks) do
        local ok, err = pcall(RegisterHook, path, function(ctx, a, b)
            local extra = ""
            if path:find("OpenLevel") then
                extra = tostring(safe(function() return a:get():ToString() end)) .. " "
                    .. tostring(safe(function() return b:get():ToString() end))
            end
            log("call", path:match("[%.:]([%w_]+:[%w_]+)$") or path, extra, "|", snapshot())
        end)
        if not ok then log("err", "hook", path, err) end
    end

    pcall(RegisterLoadMapPreHook, function(engine, world, url)
        log("map", "pre", tostring(safe(function() return url:get().Map:ToString() end)))
    end)
    pcall(RegisterLoadMapPostHook, function(engine, world)
        log("map", "post", tostring(safe(function() return world:get():GetFullName() end)))
    end)

    for _, cls in ipairs({
        "/Script/OutgameModule.GameModeStrategy",
        "/Script/BattleModule.GameStateBattle",
        "/Script/BattleModule.GameModeBattle",
    }) do
        pcall(NotifyOnNewObject, cls, function(o)
            log("new", cname(o), safe(function() return o:GetFullName() end))
        end)
    end

    local last = ""
    LoopAsync(250, function()
        ExecuteInGameThread(function()
            local s = safe(snapshot) or "snapshot failed"
            if s ~= last then
                last = s
                log("state", s)
            end
        end)
        return false
    end)

    log("start", "tracer up")
end

return M

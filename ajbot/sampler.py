"""Resident state sampler.

The `character`/`state`/`damage` tables describe whichever character's action
script ran most recently, so a single poll returns one arbitrary character. A
resident chunk run on the hook's pump cadence sees them all in turn.

Two things drive the design:

* The game runs a **separate lua_State per character**, so anything stashed in
  `_G` is invisible to the others. Aggregation happens in the DLL, keyed by
  state pointer - which doubles as a stable per-character handle, so all six
  fighters get their own record instead of collapsing into role buckets.
* Identity comes from the character's own Lua namespace: a state belonging to
  Deku Rising has `Pl250DekuHalfNaked` in its `_G`. 91 of 113 characters ship
  one; the rest report `id=?` and fall back to the slot handle.

Movement note: `GetCommandId` never reports movement - across 22k recorded
ticks not one movement command appeared, because movement is analog stick, not
a discrete command. It is read here from the lever angle plus velocity.

Two hard rules learned the hard way:

* Direction getters return **userdata** (a bound FVector) with no readable
  components, so nothing here indexes one. Vectors are only ever passed back
  into other API functions untouched.
* Speeds live in the `move` namespace, not `damage` (`move.GetLastSpeedH`).

So 3D state is assembled entirely from scalars: distances (3D and 2D), height
above ground and to fall, the vertical angle to the target, velocity, and the
lever's angle relative to our own facing - which is the only readable form of
movement input.
"""

RESIDENT = """
local function num(f) local ok, v = pcall(f) if ok and type(v) == 'number' then return v end end
local function bit(f) local ok, v = pcall(f) if ok then return v and 1 or 0 end end
local function str(f) local ok, v = pcall(f) if ok then return tostring(v) end end

-- Direction getters return userdata (a bound FVector) with no readable
-- components, so everything below is scalar. GetDeltaAngleFromTwoVector was
-- tried as a way to turn two vectors into a number, but it returned exactly
-- GetAngleUpToTargetDirection in all 112,971 sampled records, so it is not a
-- separate signal and was dropped.

-- Per-state bookkeeping lives in one table so nothing else in _G is touched.
-- Every field is initialised on its own, so a table left behind by an older
-- version of this chunk (states outlive a sampler restart) is upgraded in
-- place instead of breaking it.
local A = rawget(_G, '__ajbot')
if type(A) ~= 'table' then A = {} rawset(_G, '__ajbot', A) end
A.ev = A.ev or {}
A.w = A.w or {}
A.cur = A.cur or 'None'
A.runs = (A.runs or 0) + 1
A.since = (A.since or 0) + 1

-- Action tracking. Every action the game runs - idle, run, guard, just guard,
-- attacks, sidekick entry, being hit - starts through a global hook:
-- OnBeginFunction_Action<Name> or BeginDamage_<Name>. GetCommandId never
-- reports guards, dodges, counters, tags or hitstun, so these wrappers are the
-- only way to see them. Each wrapper counts the call, records it as the
-- current action and tail-calls the original, so behaviour is unchanged.
-- Character scripts can redefine a hook after load, so the scan repeats now
-- and then and re-wraps anything that is not our wrapper.
if A.runs == 1 or A.runs % 240 == 0 then
  for name, fn in pairs(_G) do
    if type(fn) == 'function' and type(name) == 'string' and A.w[name] ~= fn then
      local short = name:match('^OnBeginFunction_Action(.+)$')
      if not short then
        local d = name:match('^BeginDamage_(.+)$')
        if d then short = 'Damage' .. d end
      end
      if not short then short = name:match('^CallCharacter_(On.+)$') end
      if short then
        local orig, key, isact = fn, short, not name:match('^CallCharacter_')
        local w = function(...)
          A.ev[key] = (A.ev[key] or 0) + 1
          -- Change Combo bookkeeping, on the INCOMING partner's own state.
          -- A Change is the partner running SidekickEnter -> SidekickSwitchWait
          -- (ActionSidekick.lua); the C++ side decides the switch, and the
          -- only Lua-visible difference between a neutral Change and one made
          -- during the point fighter's attack is action.IsNeedTimeSlowSidekick
          -- (SwitchWait plays at 0.1/0.06 speed = the slow-mo entry) and, for
          -- the switch forced by a KO, state.IsDeadSidekickChange. Both are
          -- read once, at the instant SwitchWait begins (they describe THIS
          -- switch), and latched as sk_slow/sk_dead for the record.
          if key == 'SidekickSwitchWait' then
            local oks, sv = pcall(function() return action.IsNeedTimeSlowSidekick() end)
            local okd, dv = pcall(function() return state.IsDeadSidekickChange() end)
            A.sk_slow = (oks and sv) and 1 or 0
            A.sk_dead = (okd and dv) and 1 or 0
            if A.sk_slow == 1 then A.ev.ChangeCombo = (A.ev.ChangeCombo or 0) + 1 end
            if A.sk_dead == 1 then A.ev.DeadChange = (A.ev.DeadChange or 0) + 1 end
          end
          if isact then A.cur = key A.since = 0 end
          return orig(...)
        end
        A.w[name] = w
        rawset(_G, name, w)
      end
    end
  end
end

-- Melee/target-combo follow-up presses. The action scripts (see
-- ActionAttackMelee, identical pattern across every character) poll
-- Attack.CheckAnimChangeCustomizeCommand("Push") once per tick inside each
-- combo stage's loop to decide whether to advance to the next hit - it is
-- the game's own "was the button pressed again" signal, confirmed by reading
-- the exported action-script source. Wrapped as a pure pass-through (always
-- forwards to the original and returns its real result unchanged) so this
-- cannot alter combo behaviour even if the check has side effects. Checked on
-- every pump, not just the periodic rescan above: `Attack` is not always
-- present in _G yet on a state's first pump (it shows up once the character
-- is battle-ready), so waiting for the next 240-run rescan window missed it.
-- Re-wrap keyed on FUNCTION IDENTITY (A.push_wrap), not a flag stashed on the
-- Attack table itself: Attack.InitializeActionAttack(n) runs once per combo
-- stage, and if that rebinds CheckAnimChangeCustomizeCommand to a fresh
-- original on the SAME table object, a table-side flag would survive the
-- swap and leave us silently watching a stale, unwrapped function forever
-- (this is exactly what the OnBeginFunction_Action* wrappers above already
-- guard against via `A.w[name] ~= fn`).
do
  local atk = rawget(_G, 'Attack')
  if type(atk) == 'table' and type(rawget(atk, 'CheckAnimChangeCustomizeCommand')) == 'function'
     and rawget(atk, 'CheckAnimChangeCustomizeCommand') ~= A.push_wrap then
    local orig = atk.CheckAnimChangeCustomizeCommand
    local w = function(...)
      local r = orig(...)
      if r then A.push = (A.push or 0) + 1 end
      return r
    end
    atk.CheckAnimChangeCustomizeCommand = w
    A.push_wrap = w
  end
end

-- Discovers projectile names we do not have mapped (see the PROJ lookup
-- below): a pure pass-through wrap on Projectile.GetOwnedProjectileCount /
-- CheckOwned / CheckDeadOwned that logs every string name any character's
-- own script actually passes, live. This is diagnostic only - proj_n never
-- reads A.proj_seen - so a gap in PROJ costs nothing beyond an undercount,
-- never a wrong call. See debug_projectiles.py, which dumps A.proj_seen
-- against the static PROJ table to find characters/names worth adding.
do
  local pj = rawget(_G, 'Projectile')
  if type(pj) == 'table' then
    A.proj_wrap = A.proj_wrap or {}
    for _, fname in ipairs({'GetOwnedProjectileCount', 'CheckOwned', 'CheckDeadOwned'}) do
      local fn = rawget(pj, fname)
      if type(fn) == 'function' and fn ~= A.proj_wrap[fname] then
        local orig = fn
        local w = function(name, ...)
          if type(name) == 'string' then
            A.proj_seen = A.proj_seen or {}
            local cid = A.id or '?'
            A.proj_seen[cid] = A.proj_seen[cid] or {}
            A.proj_seen[cid][name] = true
          end
          return orig(name, ...)
        end
        pj[fname] = w
        A.proj_wrap[fname] = w
      end
    end
  end
end

local id = A.id
if not id then
  id = '?'
  -- A namespace is a TABLE named Pl<digits><Name>. Matching any global that
  -- merely starts with Pl<digits> picked up function names such as
  -- Pl215_OnGuardSuccess_ActionAttackEventAction3 as a character id.
  for k, v in pairs(_G) do
    if type(k) == 'string' and type(v) == 'table' and k:match('^Pl%d+%u') then
      id = k break
    end
  end
  A.id = id
end

-- Identity for the 22 characters with no Pl namespace. Their scripts still
-- name their own animations - "EPl21DekuShootAnimationId::at010_..." - so
-- every function in `animation` and `action` is wrapped to watch for a
-- string argument of that shape. The first character-specific animation a
-- fighter plays gives away who it is. Cheap: two byte compares per call.
if id == '?' and not A.animwrap then
  A.animwrap = true
  for _, tname in ipairs({'animation', 'action'}) do
    local tbl = rawget(_G, tname)
    if type(tbl) == 'table' then
      for fname, fn in pairs(tbl) do
        if type(fn) == 'function' then
          local orig = fn
          tbl[fname] = function(a, ...)
            if type(a) == 'string' and a:byte(1) == 69 and a:sub(2, 3) == 'Pl' then
              local ns = a:match('^E(Pl%d+[%w_]-)AnimationId::')
              if ns then A.anim_ns = ns end
              A.anim = a
            end
            return orig(a, ...)
          end
        end
      end
    end
  end
end
local idsrc = nil
if id == '?' then
  idsrc = A.anim_ns and 'anim' or 'none'
  if A.anim_ns then id = A.anim_ns end
end

-- Projectiles. Projectile.GetOwnedProjectileCount needs the projectile's own
-- name - there is no generic "do I have one out" query, and no exposed
-- spatial threat-detection API either (the game's own AI dodge logic must be
-- pure C++). Names are extracted from each character's own exported action
-- scripts (every Projectile.GetOwnedProjectileCount/CheckOwned/CheckDeadOwned
-- call, by character) - 29 of the roster reference a named projectile this
-- way; the rest either have none or fire-and-forget without tracking it.
-- `A.proj = A.proj or {...}` builds the table once per lua_State (Lua re-runs
-- this whole chunk source every pump - locals do not persist, so the `or`
-- short-circuit is what keeps this from being reallocated every time).
A.proj = A.proj or {
  Pl04 = {'PrjSidekick_Ground_Ride', 'PrjUnique1_Ground_Grow', 'PrjUnique1_Ground_Ride', 'PrjUnique1_Ground_Ride2'},
  Pl07 = {'PrjAdditionalAction1_Ground', 'PrjExUnique1_Ground', 'PrjUnique1_Ground', 'PrjUnique1_Predict', 'PrjUnique2_Ground'},
  Pl10 = {'PrjUnique1_Ground', 'PrjUnique2_Bg', 'PrjUnique2_Sleep'},
  Pl15 = {'PrjExUnique1_Mine'},
  Pl25 = {'PrjExUnique1_Ground1', 'PrjExUnique1_Ground2', 'PrjExUnique1_Air1', 'PrjExUnique1_Air2', 'PrjExUnique1_Awaking_Ground1', 'PrjExUnique1_Awaking_Ground2', 'PrjExUnique1_Awaking_Air1', 'PrjExUnique1_Awaking_Air2', 'PrjUniqueCharge1_Ground1', 'PrjUniqueCharge1_Air1', 'PrjUniqueCharge1_Awaking_Ground1', 'PrjUniqueCharge1_Awaking_Air1', 'PrjUniqueCharge2_Ground1_Left', 'PrjUniqueCharge2_Ground1_Right', 'PrjUniqueCharge2_Air1_Left', 'PrjUniqueCharge2_Air1_Right', 'PrjUniqueCharge2_Awaking_Ground1_Left', 'PrjUniqueCharge2_Awaking_Ground1_Right', 'PrjUniqueCharge2_Awaking_Air1_Left', 'PrjUniqueCharge2_Awaking_Air1_Right'},  -- Nejire (game data, not her scripts)
  Pl17 = {'PrjExUnique2_Mine', 'PrjPlusUltra1', 'PrjSidekick_Controll', 'PrjUnique1_Controll', 'PrjUnique2_Ground_Parent'},
  Pl18 = {'PrjAwaking_Ground'},
  Pl19 = {'PrjUnique1_Knife_Large'},
  Pl30 = {'PrjUnique1_Ground'},
  Pl32 = {'Unique1Charge'},
  Pl35 = {'PrjUnique1', 'PrjUnique2_GroundHit', 'PrjUniqueAir1', 'PrjUniqueAir2'},
  Pl38 = {'PrjUnique1_Air1', 'PrjUnique1_Ground1'},
  Pl39 = {'PrjExUnique1_DamageField'},
  Pl44 = {'PrjUnique1_Trap', 'PrjUnique1_Trap_Love', 'PrjUnique1_Trap_Sidekick', 'PrjUnique3_Ground', 'PrjUnique3_Ground_Ex', 'PrjUnique3_Trampoline', 'PrjUnique3_Trampoline_Ex'},
  Pl45 = {'Prj_Anchor', 'Prj_Anchor2', 'Prj_barrel'},
  Pl57 = {'PrjAdditionalAction1_Ground1', 'PrjUnique1_Ground1'},
  Pl58 = {'ExUnique2Obj'},
  Pl59 = {'PrjUnique2'},
  Pl60 = {'PrjUnique2_Air_LongShot', 'PrjUnique2_Air_Shot', 'PrjUnique2_Air_Shot2', 'PrjUnique2_Air_Wall'},
  Pl61 = {'PrjCurvePunch'},
  Pl91 = {'PrjExUnique1_GroundHit', 'ProjPlusUltra2'},
  Pl203 = {'PrjUnique2_Ground1', 'PrjUnique2_Ground2'},
  Pl208 = {'PrjUnique2_Ground'},
  Pl215 = {'PrjExUnique1_First', 'PrjExUnique1_GroundHit', 'PrjGuardCancel_DamageField'},
  Pl216 = {'PrjExUnique2_Cyclon', 'PrjExUnique2_Parent'},
  Pl221 = {'PrjUnique1_Begin', 'PrjUnique1_Small'},
  Pl243 = {'PrjExUnique1_First', 'PrjExUnique1_GroundHit', 'PrjGuardCancel_DamageField'},
  Pl244 = {'PrjExUnique2_Cyclon', 'PrjExUnique2_Parent'},
  Pl245 = {'PrjExUnique2_Cyclon', 'PrjExUnique2_Parent'},
  Pl248 = {'PrjUnique2_Ground1', 'PrjUnique2_Ground2'},
  Pl252 = {'PrjExUnique2_FallSword', 'PrjExUnique2_Marker', 'PrjExUnique2_StuckSword'},
}
local proj_n = 0
do
  local pid = id and id:match('^(Pl%d+)')
  local names = pid and A.proj[pid]
  if names and type(Projectile) == 'table' and type(Projectile.GetOwnedProjectileCount) == 'function' then
    for _, nm in ipairs(names) do
      local ok3, c = pcall(Projectile.GetOwnedProjectileCount, nm)
      if ok3 and type(c) == 'number' then proj_n = proj_n + c end
    end
  end
end

-- Character-specific "am I currently in my own special stance/transform/
-- power-up state" flag - Kirishima's Hardening, Tetsutetsu's Steel, Toga's
-- Transform, Muscular's Muscle Form, FatGum's power charge, and so on. Each
-- one is a differently-named function on the character's OWN global table
-- (e.g. "Pl47Tetsutetsu.IsSteel()"), never a shared Common API, so this is a
-- per-character lookup exactly like the projectile-name table above -
-- mapped from the exported source, folded into one generic boolean so the
-- roster's dimensionality does not explode into 20+ one-off features.
A.charmode = A.charmode or {
  Pl08 = 'IsHarden',            -- Kirishima
  Pl11 = 'IsDarkShadowMode',    -- Tokoyami
  Pl18 = 'IsTransform',         -- Toga
  Pl20 = 'IsMuscleForm',        -- Muscular
  Pl207 = 'IsLightChargeMode',  -- Hagakure
  Pl216 = 'IsMaxFeatherPower',  -- AwakeAFO
  Pl244 = 'IsMaxFeatherPower',  -- AllForOneYouth
  Pl245 = 'IsMaxFeatherPower',  -- AllForOneBoy
  Pl251 = 'IsSecondForm',       -- AllForOneShigarakiBoss
  Pl31 = 'IsFutureMode',        -- Nighteye
  Pl33 = 'IsGuardCharge',       -- FatGum
  Pl40 = 'IsDarkShadowMode',    -- DarkShadow
  Pl43 = 'IsAirDashAttackStance', -- Hawks
  Pl44 = 'IsPowerUp',           -- Gentle
  Pl47 = 'IsSteel',             -- Tetsutetsu
  Pl90 = 'GetIsPowerCharged',   -- FatGumSkinny
  Pl93 = 'IsPowerUp',           -- LaBrava
}
local char_mode = nil
do
  local pid = id and id:match('^(Pl%d+)')
  local fname = pid and A.charmode[pid]
  if fname then
    local tbl = rawget(_G, id)
    if type(tbl) == 'table' and type(tbl[fname]) == 'function' then
      local ok4, v = pcall(tbl[fname])
      if ok4 then char_mode = v and 1 or 0 end
    end
  end
end

local f = {}
local function put(k, v) if v ~= nil then f[#f + 1] = k .. '=' .. tostring(v) end end

local function ev(...)
  local n = 0
  for _, k in ipairs({...}) do n = n + (A.ev[k] or 0) end
  return n
end
local function ev_prefix(p)
  local n = 0
  for k, v in pairs(A.ev) do if k:sub(1, #p) == p then n = n + v end end
  return n
end

put('id',     id)
-- Only for fighters with no namespace: where the id came from ('anim' once
-- an animation named it, 'none' while still unknown) and the last
-- character-specific animation string seen, for the unknown-character log.
put('idsrc',  idsrc)
if idsrc then put('anim', A.anim) end
put('act',    A.cur)
put('act_n',  A.since)
put('ev_guard', ev('Guard', 'AirGuard'))
put('ev_gok',   ev('OnGuardSuccess', 'OnAirGuardSuccess'))
put('ev_jg',    ev('JustGuard'))
put('ev_js',    ev('JustStep'))
put('ev_ctr',   ev('AttackGuardCancel'))
-- Tag Combo = the cinematic grab finisher (ActionAttackTagCombo /
-- ActionAttackTagComboConfirm: ExecuteTagCombo grabs the target, partners
-- chain in via TagCombo.TryChainNextSidekick, +120 PU). In recordings only
-- the Confirm stage ever shows up as an action, so counting the bare
-- "AttackTagCombo" name read 0 in every file. This is NOT the tutorial's
-- "Change Combo" (a Change during an attack) - that is ev_chg below.
put('ev_tagc',  ev('AttackTagCombo', 'AttackTagComboConfirm'))
-- Change Combo / KO switch, derived at SidekickSwitchWait begin (see the
-- wrapper above). Counted on the partner that ENTERED, not the one that left.
put('ev_chg',   ev('ChangeCombo'))
put('ev_dchg',  ev('DeadChange'))
put('sk_slow',  A.sk_slow)
put('sk_dead',  A.sk_dead)
put('ev_skatk', ev('AttackSidekickAttack'))
put('ev_skin',  ev('SidekickEnter'))
put('ev_swap',  ev('SidekickSwitchWait'))
put('ev_skret', ev('SidekickReturn'))
put('ev_esc',   ev('AttackDamageEscape'))
-- Bakugo's TQ2 (ExUnique2) takes one of four follow-ups chosen by which face
-- button is pressed. The choice is exposed as Pl02Bakugo.GetInclusionType():
-- 1 blast, 2 unguardable, 3 circle-around, 5 fast step, 0 = none pending. It
-- is only readable while that state exists, so it is recorded as `incl` and
-- only for that character; nothing else reads it yet.
do
  local okb, tb = pcall(rawget, _G, 'Pl02Bakugo')
  if okb and type(tb) == 'table' and type(tb.GetInclusionType) == 'function' then
    local oki, iv = pcall(tb.GetInclusionType)
    if oki and type(iv) == 'number' then put('incl', iv) end
  end
end
put('ev_dodge', ev('Dodge', 'AirDodge'))
put('ev_gbrk',  ev('DamageGuardBreak'))
put('ev_unbrk', ev('DamageUnbreakableWeak'))
put('ev_hit',   ev_prefix('Damage'))
put('ev_ub',    ev('AttackUnguardable'))
put('ev_pu',    ev('AttackPlusUltra1', 'AttackPlusUltra2'))
put('ev_push',  A.push or 0)
put('sk',     bit(characterbase and characterbase.IsSidekick))
put('dead',   bit(character.IsDead))
put('hp',     num(character.GetHealthRate))
put('dist',   num(character.GetTargetDistance))
put('dist2',  num(character.GetAdjustTargetDistance2D))
put('gdist',  num(character.GetGroundDistance))
put('fall',   num(character.GetHeightToFall))
put('cmd',    str(state.GetCommandId))
put('atk',    str(state.GetAttackId))
put('lvl',    num(state.GetAttackLevel))
put('ground', bit(state.IsOnGround))
put('wall',   bit(state.IsOnWall))
put('stun',   bit(damage.IsStun))
put('grab',   bit(state.IsGrabbed))
put('grabbing', bit(state.IsGrab))
put('armor_hit', bit(state.IsHitAttackToSuperArmor))
put('proj_n', proj_n)
put('char_mode', char_mode)
-- Fires the instant our own attack connects (used by several characters'
-- own combo-transition checks, e.g. ActionAttackMelee's air variant: "Attack.
-- CheckAnimChangeCommand(...) and state.IsHitMyAttack()"). Distinct from
-- ev_hit (a cumulative count, needs a delta to mean anything): this is a
-- direct instantaneous confirm, useful as its own feature without needing
-- Envelope bookkeeping on the consumer side.
put('hit_confirm', bit(state.IsHitMyAttack))
-- Companion to hit_confirm: our attack connected but was GUARDED, not a
-- clean hit (widely checked together across character scripts, e.g.
-- "state.IsHitMyAttack() and not state.IsBlockedMyAttack()" to gate a
-- combo-continuation on a real hit rather than a blocked one).
put('atk_blocked', bit(state.IsBlockedMyAttack))
-- A non-consuming peek at the same "Push" input buffer
-- Attack.CheckAnimChangeCustomizeCommand consumes into ev_push - this can
-- read true across several ticks while a press is buffered, rather than
-- firing once at confirmation, so it is a distinct, complementary signal
-- ("is a follow-up currently queued") not a duplicate of ev_push ("a
-- follow-up was just confirmed").
put('push_avail', bit(function() return command.IsEnableCustomizeCommand('Push') end))
-- True at the instant a held charge button is released (not while holding -
-- "IsHold = not(Attack.IsReleaseHold())" is the pattern character scripts
-- use to derive the opposite, currently-holding flag).
put('charge_release', bit(Attack.IsReleaseHold))
put('kb',     bit(state.IsKnockBack))
-- Magnitude/duration of the knockback from the hit just taken (ActionGuard's
-- own blocked-hit reaction reads these to drive the pushback). `kb` above is
-- only ever a boolean; a small chip-push and a full launcher both read the
-- same until now.
put('kb_dist', num(damage.GetKnockBackDistance))
put('kb_time', num(damage.GetKnockBackTime))
-- The per-hit bonus hitstun/launch-time a raw (unguarded) hit adds - read
-- once at the moment that reaction begins in nearly every Damage/ variant
-- (e.g. ActionDamageStrong: "AccelTime = AccelTime_Strong +
-- damage.GetDamageAdditionalTime()"). Kb_dist/kb_time above only ever fire
-- from a GUARDED hit's reaction; this is the equivalent signal for an
-- actual unguarded hit's severity.
put('dmg_addtime', num(damage.GetDamageAdditionalTime))
put('dodge',  bit(character.IsTargetDodge))
put('enemy',  bit(character.IsEnemyCharacter))
put('ai',     bit(character.IsAICharacter))
put('active', bit(character.IsActiveCharacter))
put('awake',  bit(Awaking.IsAwaking))

-- 3D geometry, all scalar. `angleup` is the vertical angle to the target.
put('angleup', num(character.GetAngleUpToTargetDirection))
put('rside',   bit(character.IsLeverOnTargetRightSide))

-- Velocity lives in `move`, not `damage`.
put('spdh', num(move.GetLastSpeedH))
put('spdv', num(move.GetLastSpeedV))
put('grav', num(move.GetLastGravitySpeed))
put('tgnd', bit(move.IsTouchGround))
put('twal', bit(move.IsTouchWall))

-- Movement input: the stick, as an angle relative to our own facing plus a
-- magnitude. This is the only readable form of movement.
put('lang',   num(character.GetAngleLeverToCharacterForward))
put('lcam',   num(character.GetAngleLeverToCameraForward))
put('lscale', num(command.GetLeverScale))

-- tbear/vbear (bearings of the opponent and the camera via
-- GetDeltaAngleFromTwoVector on GetTargetDirection / GetViewDirection) were
-- tried here on 2026-10-24 and crashed the game on entering a real match:
-- this chunk runs in EVERY lua state, and some (benched partners, sidekicks,
-- projectiles) have no target/camera behind those getters - a native crash
-- pcall cannot catch. The stick frame now comes from the player side plus an
-- online check of which way 'toward' actually moves (agent.FrameTracker).
put('lon',    bit(command.IsEnableLever))

-- Tag / Plus-Ultra signals. There is NO getter anywhere in the Lua API that
-- returns a gauge VALUE, so the meter can only be observed through these
-- threshold and state flags.
-- Verified live: IsUsableFullAwaking works, ReadyForSuperPlusUltra returns
-- nil (a notifier, not a getter) and IsSidekick does not exist at all.
put('awk_full', bit(Awaking.IsUsableFullAwaking))
put('awk_pow',  bit(Awaking.IsEnableAdditionalPower))
put('awk_str',  bit(Awaking.IsStrongerUniqueAttack))
put('sk_tag',   bit(character.IsTagComboSidekick))
put('sk_pu',    bit(character.IsSidekickPlusUltra))
put('sk_resc',  bit(character.IsRescueSidekick))
put('atkcount', num(Attack.GetAttackCount))
put('uniq_idx', num(state.GetUniqueStateIndex))
-- Which moves are usable RIGHT NOW. This is the single most informative
-- thing available: it folds together meter, cooldowns and state without
-- needing a gauge getter (there is none - ReadyForSuperPlusUltra returns nil,
-- it is a notifier rather than a query).
local function usable(id)
  local f = action and action.IsValidAttack
  if type(f) ~= 'function' then return end
  local ok, v = pcall(f, 'EUseAttackId::' .. id)
  if ok then return v and 1 or 0 end
end
put('ok_melee',   usable('Melee'))
put('ok_exmelee', usable('ExMelee'))
put('ok_u1',      usable('Unique1'))
put('ok_u2',      usable('Unique2'))
put('ok_exu1',    usable('ExUnique1'))
put('ok_exu2',    usable('ExUnique2'))
put('ok_ub',      usable('Unguardable'))
put('ok_pu1',     usable('PlusUltra1'))
put('ok_pu2',     usable('PlusUltra2'))
put('ok_spu',     usable('SuperPlusUltra'))
put('ok_awk',     usable('Awaking'))
put('ok_extreme', usable('ExtremeAction'))   -- the Rising special (Bakugo's shot lunge)
-- Emergency dodge. Read as a move like the rest; whether it drops to 0 once
-- the stock is spent is being checked against the recordings.
put('ok_esc',     usable('DamageEscape'))

-- Abnormal statuses. "Erase" is Aizawa's Erasure (ExUnique2 damage data:
-- status Erase, 8 s): the quirk buttons do nothing while it lasts. It used to
-- probe "Emptiness", which is Kaminari's short-circuit action, not a status -
-- so erasure was never seen. A short-circuited Kaminari has every attack
-- disabled too (ActionEmptiness), so that state still counts.
local function status(id)
  local ok, v = pcall(state.IsValidAbnormalStatus, 'EAbnormalStatusId::' .. id)
  if ok then return v and 1 or 0 end
end
local erase_s = status('Erase')
if A.cur == 'Emptiness' then erase_s = 1 end
put('erased',   erase_s)
put('st_stun',  status('Stun'))
put('st_burn',  status('Burn'))
put('st_freeze',status('Freeze'))
put('st_elec',  status('Electric'))
put('st_black', status('BlackOut'))
-- Momo's Lucky Bag pickup buff (attack up + heal); nil if the id is unknown.
put('st_lucky', status('YaoyorozuLuckyBag'))

-- Guarding. Just-guard is the guard button timed onto the incoming hit; it
-- is the highest-leverage defensive option because it opens a counter window.
put('autoguard', bit(state.IsAutoGuard))
put('jg_off',    bit(state.IsInputDisableJustGuard))
put('juststep',  bit(state.IsJustStepSuccess))
put('guardpow',  num(damage.GetGuardPower))
put('dmg_pow',   num(damage.GetDamagePower))

-- Slow motion ("the game goes slow mode"): a just guard hit-slows the
-- attacker 0.6 s, a counter 0.7 s, a rock-paper-scissors clash slows both
-- sides (the loser more), an escape slows 0.4 s, a Tag Combo confirm 1.0 s
-- (ActionParam ヒットスロー / RPS). Whoever is NOT slowed has a free punish.
-- All self-only getters the character scripts call themselves (state.
-- IsHitSlow 16x, GetCustomTimeDilation in a Pl script), and only read while a
-- fighter action is live so benched / projectile states never call them.
if A.cur ~= 'None' then
  put('hslow', bit(state.IsHitSlow))
  put('tdil',  num(character.GetCustomTimeDilation))
end

return table.concat(f, ' ')
"""

# Python-side mirror of the `A.proj` table embedded in RESIDENT above - keep
# the two in sync by hand if either changes. Used only to tell a genuinely
# new projectile name apart from one we already know about; RESIDENT's own
# copy is the one that actually drives proj_n.
PROJ_NAMES = {
    "Pl04": {"PrjSidekick_Ground_Ride", "PrjUnique1_Ground_Grow", "PrjUnique1_Ground_Ride", "PrjUnique1_Ground_Ride2"},
    "Pl07": {"PrjAdditionalAction1_Ground", "PrjExUnique1_Ground", "PrjUnique1_Ground", "PrjUnique1_Predict", "PrjUnique2_Ground"},
    "Pl10": {"PrjUnique1_Ground", "PrjUnique2_Bg", "PrjUnique2_Sleep"},
    "Pl15": {"PrjExUnique1_Mine"},
    "Pl17": {"PrjExUnique2_Mine", "PrjPlusUltra1", "PrjSidekick_Controll", "PrjUnique1_Controll", "PrjUnique2_Ground_Parent"},
    "Pl18": {"PrjAwaking_Ground"},
    "Pl19": {"PrjUnique1_Knife_Large"},
    "Pl30": {"PrjUnique1_Ground"},
    "Pl32": {"Unique1Charge"},
    "Pl35": {"PrjUnique1", "PrjUnique2_GroundHit", "PrjUniqueAir1", "PrjUniqueAir2"},
    "Pl25": {"PrjExUnique1_Ground1", "PrjExUnique1_Ground2", "PrjExUnique1_Air1", "PrjExUnique1_Air2", "PrjExUnique1_Awaking_Ground1", "PrjExUnique1_Awaking_Ground2", "PrjExUnique1_Awaking_Air1", "PrjExUnique1_Awaking_Air2", "PrjUniqueCharge1_Ground1", "PrjUniqueCharge1_Air1", "PrjUniqueCharge1_Awaking_Ground1", "PrjUniqueCharge1_Awaking_Air1", "PrjUniqueCharge2_Ground1_Left", "PrjUniqueCharge2_Ground1_Right", "PrjUniqueCharge2_Air1_Left", "PrjUniqueCharge2_Air1_Right", "PrjUniqueCharge2_Awaking_Ground1_Left", "PrjUniqueCharge2_Awaking_Ground1_Right", "PrjUniqueCharge2_Awaking_Air1_Left", "PrjUniqueCharge2_Awaking_Air1_Right"},
    "Pl38": {"PrjUnique1_Air1", "PrjUnique1_Ground1"},
    "Pl39": {"PrjExUnique1_DamageField"},
    "Pl44": {"PrjUnique1_Trap", "PrjUnique1_Trap_Love", "PrjUnique1_Trap_Sidekick", "PrjUnique3_Ground", "PrjUnique3_Ground_Ex", "PrjUnique3_Trampoline", "PrjUnique3_Trampoline_Ex"},
    "Pl45": {"Prj_Anchor", "Prj_Anchor2", "Prj_barrel"},
    "Pl57": {"PrjAdditionalAction1_Ground1", "PrjUnique1_Ground1"},
    "Pl58": {"ExUnique2Obj"},
    "Pl59": {"PrjUnique2"},
    "Pl60": {"PrjUnique2_Air_LongShot", "PrjUnique2_Air_Shot", "PrjUnique2_Air_Shot2", "PrjUnique2_Air_Wall"},
    "Pl61": {"PrjCurvePunch"},
    "Pl91": {"PrjExUnique1_GroundHit", "ProjPlusUltra2"},
    "Pl203": {"PrjUnique2_Ground1", "PrjUnique2_Ground2"},
    "Pl208": {"PrjUnique2_Ground"},
    "Pl215": {"PrjExUnique1_First", "PrjExUnique1_GroundHit", "PrjGuardCancel_DamageField"},
    "Pl216": {"PrjExUnique2_Cyclon", "PrjExUnique2_Parent"},
    "Pl221": {"PrjUnique1_Begin", "PrjUnique1_Small"},
    "Pl243": {"PrjExUnique1_First", "PrjExUnique1_GroundHit", "PrjGuardCancel_DamageField"},
    "Pl244": {"PrjExUnique2_Cyclon", "PrjExUnique2_Parent"},
    "Pl245": {"PrjExUnique2_Cyclon", "PrjExUnique2_Parent"},
    "Pl248": {"PrjUnique2_Ground1", "PrjUnique2_Ground2"},
    "Pl252": {"PrjExUnique2_FallSword", "PrjExUnique2_Marker", "PrjExUnique2_StuckSword"},
}

# Pulls A.proj_seen (populated by RESIDENT's pass-through wrap on
# Projectile.GetOwnedProjectileCount/CheckOwned/CheckDeadOwned - see the
# "Discovers projectile names" block above) off whichever lua_State this
# particular eval happens to land on, and returns only the (character_id,
# name) pairs that are NOT already in PROJ_NAMES for that character.
_UNKNOWN_PROJ_QUERY = r"""
local ok, out = pcall(function()
  local A = rawget(_G, '__ajbot')
  if type(A) ~= 'table' or type(A.proj_seen) ~= 'table' then return '' end
  local lines = {}
  for cid, names in pairs(A.proj_seen) do
    for nm in pairs(names) do
      lines[#lines + 1] = cid .. '|' .. nm
    end
  end
  return table.concat(lines, '\n')
end)
return ok and out or ''
"""


_TEXT = {"id", "cmd", "atk", "act"}

# Event counters written by the action wrappers. Cumulative per lua_State, so a
# consumer reads the DIFFERENCE between ticks.
EVENTS = ("ev_guard", "ev_gok", "ev_jg", "ev_js", "ev_ctr", "ev_tagc",
          "ev_chg", "ev_dchg",
          "ev_skatk", "ev_skin", "ev_swap", "ev_skret", "ev_esc", "ev_dodge",
          "ev_gbrk", "ev_unbrk", "ev_hit", "ev_ub", "ev_pu", "ev_push")

# Current action (the `act` field) folded into a handful of tactical classes.
# Order matters: the first matching prefix wins.
ACT_CLASSES = (
    # A successful tech/breakfall (ukemi) - BeginDamage_Recover/DownRecover/
    # NearGroundRecover/RingOutAutoRecover in the exported source. Checked
    # before the generic "damage" catch-all below, which every one of these
    # would otherwise also match: a tech is the game handing control back
    # (Attack.SetReturnToNormal() fires partway through it), not more
    # helpless hitstun, and the two looked identical to the model until now.
    ("recovered", ("DamageRecover", "DamageDownRecover", "DamageNearGroundRecover",
                   "DamageRingOutAutoRecover")),
    ("damage",  ("Damage",)),
    ("guard",   ("Guard", "AirGuard", "JustGuard")),
    ("counter", ("AttackGuardCancel",)),
    ("unblock", ("AttackUnguardable",)),
    ("pu",      ("AttackPlusUltra", "AttackSuperPlusUltra", "AttackBlowDownPlusUltra",
                 "AttackExSuperPlusUltra")),
    ("escape",  ("AttackDamageEscape",)),
    ("assist",  ("AttackSidekick", "AttackRescueSidekick", "AttackTagCombo",
                 "Sidekick", "SummonSidekick", "Rescue")),
    # AirChase* is Gran Torino's homing jet-dash inside his Melee string
    # (cmd/atk stay Melee, horizontal speed ~4000) - part of the combo.
    ("combo",   ("AttackMelee", "AttackExMelee", "AirChase")),       # target combo
    ("rising",  ("AttackAwaking", "Awaking")),
    ("attack",  ("Attack",)),                             # quirk skills etc.
    ("dodge",   ("Dodge", "AirDodge", "JustStep", "AwakingDodge")),
    ("move",    ("Run", "Dash", "Jump", "AirDash", "AirJump", "DashJump",
                 "AirRun", "Chase", "SlideWalk", "FastFall", "AirFallDown",
                 "AirRiseUp", "Land", "WallLand",
                 "Hover")),            # Nejire's flight (Hover, HoverMove)
    # WinDemo is the victory pose: the match is over and the fighter does
    # nothing (its cmd stays whatever it last pressed).
    ("idle",    ("Idle", "AirIdle", "None", "WinDemo")),
)

# `act` strings that are deliberately left in "other": not a tactical
# decision, so they should not keep appearing in logs/unknown_actions.jsonl.
# "Emptiness" is the quirk-erasure status (already the `erased` feature).
KNOWN_OTHER_ACTS = frozenset({"Emptiness"})
ACT_CLASS_NAMES = tuple(name for name, _ in ACT_CLASSES) + ("other",)


def act_class(act):
    """Tactical class of an `act` string ('damage', 'guard', 'combo', ...)."""
    if not act or not isinstance(act, str):
        return "other"
    for name, prefixes in ACT_CLASSES:
        for p in prefixes:
            if act.startswith(p):
                return name
    return "other"


def install(hook, every_pumps=8):
    """Start sampling. Lower `every_pumps` = fresher data, more Lua per frame."""
    hook.set_resident(RESIDENT)
    hook.set_interval(every_pumps)


def stop(hook):
    hook.set_resident("")


def _parse(line):
    rec = {}
    for f in line.split(" "):
        if "=" not in f:
            continue
        k, v = f.split("=", 1)
        if k in _TEXT:
            rec[k] = v.split("::", 1)[1] if "::" in v else v
        else:
            try:
                rec[k] = float(v)
            except ValueError:
                rec[k] = v
    return rec


def snapshot(hook):
    """Every character seen, keyed by slot handle ('c0', 'c1', ...).

    Each record carries `id` (character namespace), `ai`, `active`, `enemy`
    and `age`. Use `teams()` to turn this into my-party / enemy-party.
    """
    out = {}
    for line in hook.samples().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        slot, _, rest = line.partition(" ")
        if rest:
            out[slot] = _parse(rest)
    return out


# Toga (Pl18Toga) transforms by spawning a SUB-CHARACTER: a separate lua_State
# that runs under the copied fighter's own id ("Pl47Tetsutetsu"). Measured
# across 76 recorded transforms it always:
#   * reads the same `enemy` flag and the same `hp` as Toga (health is shared),
#   * reports act='None' and active=1 (Toga's own state still runs the
#     actions - under generic names - and reads active=0 meanwhile).
# Left alone it was filed under the copied fighter's name: copying an
# opponent put it on THEIR team (and made it `enemy` 75 of 266 ticks - the
# bot aimed at itself), copying her own partner (Plus Ultra) made it `me` and
# overwrote that partner's health with Toga's. Its state also never goes away,
# so the real opponent's key flipped between X and X#1 after the first
# transform. Slots identified as a copy are remembered (lua_State pointers are
# stable) and left out of dedupe() for good; Toga's record carries the copy:
#   copy_of  - the copied fighter's id while transformed
#   copy_pu  - 1 when it is her own partner (the Plus Ultra transform: that
#              partner's whole moveset), 0 when it is an opponent (blood
#              transform: their jabs, counter and unblockable only)
TOGA_ID = "Pl18Toga"
_COPY_SLOTS = {}          # slot -> id of the copy state living there
_COPY_PU = {}             # slot -> 1 if that copy came from her Plus Ultra


def forget_copies():
    _COPY_SLOTS.clear()
    _COPY_PU.clear()


def _mark_toga_copies(snap):
    """Find live Toga copies in a raw snapshot; returns {toga_slot: copy_slot}."""
    togas = [(s, r) for s, r in snap.items()
             if str(r.get("id", "")).startswith(TOGA_ID)
             and r.get("char_mode") == 1 and r.get("age", 1e9) <= 30]
    found = {}
    for ts, tg in togas:
        thp = tg.get("hp")
        if not isinstance(thp, (int, float)):
            continue
        best = None
        for s, r in snap.items():
            cid = str(r.get("id", ""))
            if s == ts or not cid.startswith("Pl") or cid.startswith(TOGA_ID):
                continue
            if bool(r.get("enemy")) != bool(tg.get("enemy")):
                continue
            if r.get("act") not in (None, "None") or not r.get("active"):
                continue
            if r.get("age", 1e9) > 30:
                continue
            hp = r.get("hp")
            if not isinstance(hp, (int, float)) or abs(hp - thp) > 0.01:
                continue
            d = abs(float(r.get("dist") or 0) - float(tg.get("dist") or 0))
            if best is None or d < best[0]:
                best = (d, s)
        if best is not None:
            cs = best[1]
            if _COPY_SLOTS.get(cs) != snap[cs].get("id"):
                _COPY_PU.pop(cs, None)          # a new transform
            found[ts] = cs
            _COPY_SLOTS[cs] = snap[cs].get("id")
            # Plus Ultra: she copies her own partner. Seen as the PU act at
            # the start of the transform, or (live) as that partner's own
            # state still present on her side under the same name.
            cid = snap[cs].get("id")
            if tg.get("act") == "AttackPlusUltra1" or any(
                    s != cs and r.get("id") == cid
                    and bool(r.get("enemy")) == bool(tg.get("enemy"))
                    and _COPY_SLOTS.get(s) != cid
                    for s, r in snap.items()):
                _COPY_PU[cs] = 1
            else:
                _COPY_PU.setdefault(cs, 0)
    return found


def dedupe(snap):
    """Collapse slots that describe the same character.

    There are more lua_States than fighters - a character can own several
    (auxiliary scripts, projectiles, sidekick logic), so slots are not 1:1
    with characters. Keep the freshest record per character id. Records with
    no namespace (`id='?'`) keep their slot handle, since they cannot be
    told apart by name.

    A MIRROR MATCH (both sides pick the same character) reuses the same id
    on both sides' lua_States. Collapsing by id alone made the second copy
    vanish entirely - keeping whichever ranked better and discarding the
    other, so a mirrored opponent was invisible for the whole match. States
    sharing an id are only merged when they ALSO agree on `enemy`: a genuine
    duplicate of one fighter (the case above) is always on the one team: a
    same-tick disagreement means two different fighters share this name, and
    the second one gets a distinct key instead of disappearing. Which side
    keeps the plain key is deterministic (enemy=False first) rather than
    insertion order, so the same physical fighter keeps the same key from
    tick to tick regardless of snapshot ordering; teams() resolves actual
    mine/theirs identity from there, including the orientation case where
    enemy=False is not "mine".
    """
    by_id = {}
    out = {}
    live_copy = _mark_toga_copies(snap)
    for s in list(_COPY_SLOTS):
        r = snap.get(s)
        # Reused by someone else, or it ACTS - a copy never runs actions, so
        # that is a real fighter and must not be hidden.
        if r is not None and (r.get("id") != _COPY_SLOTS[s]
                              or r.get("act") not in (None, "None")):
            del _COPY_SLOTS[s]
            _COPY_PU.pop(s, None)
    copy_slots = {s for s in _COPY_SLOTS if s in snap}
    for ts, cs in live_copy.items():
        tg, cid = snap[ts], snap[cs].get("id")
        tg["copy_of"] = cid
        tg["copy_pu"] = _COPY_PU.get(cs, 0)
        # She is the point fighter; her own state reads active=0 while the
        # copy is out, which handed `me` to the copy or a benched partner.
        tg["active"] = 1.0
    for slot, rec in snap.items():
        if slot in copy_slots:
            continue
        cid = rec.get("id", "?")
        if cid == "?":
            cur = out.get(slot)
            if cur is None or _rank(rec) < _rank(cur):
                out[slot] = rec
            continue
        by_id.setdefault(cid, []).append((slot, rec))

    for cid, entries in by_id.items():
        groups = {}
        for slot, rec in entries:
            groups.setdefault(bool(rec.get("enemy")), []).append((slot, rec))
        for i, side in enumerate(sorted(groups)):
            _, rec = min(groups[side], key=lambda sr: _rank(sr[1]))
            out[cid if i == 0 else f"{cid}#{i}"] = rec
    return out


# Mirrors dataset.BENCH_AGE - nothing downstream of a recording ever reads a
# chars[] entry past this age unless it is named by me/enemy/mine/theirs for
# that tick (see tools/prune_stale_chars.py's audit of every consumer). Duplicated
# here rather than imported: dataset.py already imports FROM this module.
STALE_AGE = 3000


def prune_stale(chars, keep_keys):
    """Drop dedupe() entries nothing downstream will ever read.

    The DLL side of the hook aggregates every lua_State it has EVER seen,
    keyed by pointer - "aggregation happens in the DLL, keyed by state
    pointer" (see the module docstring) - and does not appear to forget one
    once its character leaves the match. Across a long play2.py --auto
    session this means EVERY later tick carries every earlier match's
    fighters too: one real session reached 78 "characters" per tick instead
    of ~6, ballooning one recording to 12GB. Cutting it here, once, before
    the tick is ever written, is cheaper and safer than a corpus-wide
    cleanup after the fact.
    """
    return {k: r for k, r in chars.items()
           if k in keep_keys or r.get("age", 1e9) <= STALE_AGE}


def _rank(rec):
    """Lower is better when two states claim one character.

    Freshness first, but an auxiliary state that never runs actions reports
    act='None' forever; at a near-tie in age the state actually running the
    fighter's actions wins, so `act` and the event counters do not flicker
    between the two.
    """
    age = rec.get("age", 1e9)
    if "act" in rec and rec.get("act") in (None, "None"):
        age += 50
    return age


class Envelope:
    """Per-(character, counter) monotone deltas.

    Event counters are cumulative per lua_State, and a character can own two
    states that dedupe alternates between - reading raw differences would
    report a burst of fake events every flip. Only rises above the highest
    value already seen count.
    """

    def __init__(self):
        self.top = {}

    def delta(self, key, field, value):
        if not isinstance(value, (int, float)) or key is None:
            return 0.0
        k = (key, field)
        top = self.top.get(k)
        self.top[k] = value if top is None else max(top, value)
        if top is None or value <= top:
            return 0.0
        d = value - top
        return d if d <= 5 else 0.0      # a larger jump is a state switch


class Roster:
    """Tracks the current match's two line-ups.

    The per-tick `enemy` flag is accurate - checked against a real recording it
    gives a clean 3v3 at any tick. What corrupts it is *stale* records: a
    lua_State outlives its match, so a character from the previous game keeps
    being sampled with its old allegiance. Filtering on `age` removes them.

    Sides are decided by majority over fresh sightings within the match, not
    by the latest one: a character's flag reads correctly ~97% of the time, so
    taking the most recent value lets a rare minority reading flip it
    permanently. Once decided a character keeps its side even while benched,
    since a benched fighter stops running scripts and its age climbs.

    Note ids are not fighters: a transformation (StripeAtmosphere) and a paired
    sidekick (LaBrava with Gentle) each get their own id, so a 3v3 legitimately
    shows more than three ids a side.

    Automatic match-boundary reset is OFF by default. The heuristic (everyone
    back to full health) fired spuriously mid-match and wiped the votes,
    turning a correct 3v3 into 6v1. Record one match per run instead, or call
    reset() yourself between matches.

    Ids below `min_confidence` are withheld rather than guessed - a
    transformation can legitimately read as both sides (StripeAtmosphere sat
    at 0.51).
    """

    def __init__(self, fresh_age=300, reset_on_new_match=False,
                 min_votes=20, min_confidence=0.6):
        self.fresh_age = fresh_age
        self.reset_on_new_match = reset_on_new_match
        self.min_votes = min_votes
        self.min_confidence = min_confidence
        self.votes = {}
        self.pinned = {}
        self._was_damaged = False
        # Which value of the enemy flag means "their side". The flag splits
        # the roster into two teams reliably, but which team it calls enemy is
        # relative to a viewing perspective. Evidence accumulates here:
        # +1 says enemy=1 is theirs (the usual reading), -1 says it is flipped.
        self.orient = 0.0
        # When both teams are known exactly (pinned by name), anything that
        # matches no pin - fighters left over from an earlier match, stray
        # companion states - is left out of the teams instead of being put on
        # a side by the enemy flag. Stale states from the previous match were
        # landing on our side and swelling a 3-fighter team to 8.
        self.exclusive = False
        # Wall-clock time each character was last seen running scripts. A
        # benched partner stops running them and its `age` climbs past any
        # staleness cut-off within seconds; this is how it stays on the team.
        self.last_fresh = {}

    def recent(self, key, within=600.0):
        """Seen live during this session within the last `within` seconds."""
        import time as _time
        t = self.last_fresh.get(key)
        return t is not None and _time.monotonic() - t <= within

    @property
    def side_of(self):
        return {cid: self.side(cid) for cid in self.votes if self.side(cid)}

    @staticmethod
    def _looks_like_match_start(fresh):
        """Everyone at full health, after someone had been hurt."""
        return len(fresh) >= 4 and all(r.get("hp", 0) >= 0.995 for _, r in fresh)

    def observe(self, snap):
        # Keyed by character id, or by slot handle for the 22 of 113
        # characters that ship no Lua namespace (Deku Shoot Style/Pl21 among
        # them). Dropping the unnamed ones lost a whole team member.
        fresh = [(k, r) for k, r in dedupe(snap).items()
                 if r.get("age", 1e9) <= self.fresh_age]
        if not fresh:
            return
        import time as _time
        now = _time.monotonic()
        for k, _ in fresh:
            self.last_fresh[k] = now

        if self.reset_on_new_match:
            if self._looks_like_match_start(fresh):
                if self._was_damaged:
                    self.votes.clear()
                    self._was_damaged = False
            elif any(r.get("hp", 1.0) < 0.95 for _, r in fresh):
                self._was_damaged = True

        for key, rec in fresh:
            v = self.votes.setdefault(key, {"theirs": 0, "mine": 0})
            v["theirs" if rec.get("enemy") else "mine"] += 1

    def assign(self, ids, side="mine"):
        """Pin characters to a side, overriding the vote.

        `IsEnemyCharacter` is relative to a viewing perspective, and when the
        bot drives Player 1 that perspective flips: every character - its own
        team included - can report enemy=1. Voting then puts the whole roster
        on one side, `mine` comes back empty and the bot idles. When the team
        was chosen by us, we simply know the answer.

        Names are roster ids ("Bakugo"); sampler keys are Lua namespaces
        ("Pl02Bakugo"). They are matched by suffix, because pinning the bare
        roster id matched nothing at all.
        """
        for cid in ids:
            self.pinned[cid] = side

    def _pin_name_for(self, cid):
        """Which PINNED NAME (the roster key itself, e.g. 'Bakugo') this
        identity matches, or None - not the side. Shared by _pin_for and
        teams()'s mirror-collision arbitration, which needs to know WHEN two
        different fighters both match the same pin, not just that one does.
        """
        if cid in self.pinned:
            return cid
        for name in self.pinned:
            # "Pl02Bakugo" endswith "Bakugo". Guard against a bare prefix
            # match so "Nomu" does not also claim "HighendNomu1".
            if cid.endswith(name) and cid[:-len(name)].rstrip("0123456789") == "Pl":
                return name
        # Alternate forms carry their own id (Pl247DabiFullBodyBurn); follow
        # them to the pinned base character. Also how a mirror-collision key
        # (dedupe's "Pl02Bakugo#1") still resolves: base_name's own prefix
        # fallback strips the suffix the same way it strips a form suffix.
        from .roster import base_name
        base = base_name(cid)
        if base and base in self.pinned:
            return base
        return None

    def _pin_for(self, cid):
        name = self._pin_name_for(cid)
        return self.pinned[name] if name else None

    def pin_of(self, cid):
        """The pinned side for a character, or None when no pin matches.

        Characters that ship no Lua namespace are keyed by slot handle and can
        never match a name pin, so a caller that relies on pinning both teams
        needs to know which ones fell through.
        """
        return self._pin_for(cid)

    @property
    def flipped(self):
        """True when the evidence says enemy=0 marks the opposing team."""
        return self.orient < -3.0

    def side(self, cid, allow_pin=True):
        """`allow_pin=False` skips straight to the vote/flag reading, for a
        mirror-collision key that teams() has already decided must not win
        the pin - see its docstring."""
        pin = self._pin_for(cid) if allow_pin else None
        if pin:
            return pin
        v = self.votes.get(cid)
        if not v or (v["theirs"] + v["mine"]) < self.min_votes:
            return None
        if self.confidence(cid) < self.min_confidence:
            return None
        flagged = v["theirs"] > v["mine"]
        if self.flipped:
            flagged = not flagged
        return "theirs" if flagged else "mine"

    def observe_control(self, snap, stick=None, bot_is_ai=False):
        """Accumulate evidence for which enemy-flag value is OUR team.

        Replaces pinning by lever. The CPU drives a lever too - in the random
        runs recorded so far enemy=1/ai=1 characters read a live lever as often
        as the bot's own, so "whoever has a live lever is ours" pinned the
        CPU's fighters onto our team one by one until `theirs` was empty and
        the bot played 90-99% of every match with no opponent at all.

        Two signals that do hold:
          * `ai=1` is the CPU. Unless the bot is itself flagged AI, an ai=1
            fighter is theirs, so its enemy flag names the opposing value.
          * a lever that points where the bot just pushed the stick is ours.
        Evidence is per TEAM (one orientation), never per character, so a
        stray reading cannot move one fighter across.
        """
        import math
        want = None
        if stick is not None and math.hypot(stick[0], stick[1]) > 0.5:
            want = math.degrees(math.atan2(stick[0], stick[1]))
        for key, rec in dedupe(snap).items():
            if rec.get("age", 1e9) > 30 or not rec.get("active"):
                continue
            flag = 1 if rec.get("enemy") else -1
            if rec.get("ai") and not bot_is_ai:
                # This fighter is theirs; its flag value is the "theirs" value.
                self.orient += 0.05 * flag
            elif want is not None and rec.get("lon") and not rec.get("ai"):
                lcam = rec.get("lcam")
                if (isinstance(lcam, (int, float))
                        and float(rec.get("lscale") or 0.0) > 30
                        and abs((lcam - want + 180) % 360 - 180) < 25):
                    # Ours: its flag value is the "mine" value.
                    self.orient -= 0.2 * flag
        self.orient = max(-50.0, min(50.0, self.orient))

    def confidence(self, cid):
        v = self.votes.get(cid)
        n = (v["theirs"] + v["mine"]) if v else 0
        return max(v["theirs"], v["mine"]) / n if n else 0.0

    def reset(self):
        """Forget the previous match's sightings. Pins and the enemy-flag
        orientation survive: both are properties of the session, not of one
        match."""
        self.votes.clear()
        self.last_fresh.clear()
        self._was_damaged = False
        forget_copies()

    def sides(self):
        """Observed characters by side. Pin patterns that matched nothing are
        not reported - only characters actually seen in the match."""
        out = {"mine": [], "theirs": []}
        for cid in self.votes:
            s = self.side(cid)
            if s:
                out[s].append(cid)
        return out

    def infer_from_lever(self, snap, stick=None):
        """Deprecated: kept so older callers still run. Pins nothing.

        It used to pin any fighter with a live lever as ours, which is wrong -
        the CPU drives a lever as well - and it was the cause of the 90-99%
        blind random runs. Now it only feeds orientation evidence.
        """
        self.observe_control(snap, stick)
        return None


def teams(snap, max_age=800, roster=None):
    """Split a snapshot into {'me','mine','enemy','theirs'}.

    'me'/'enemy' are the active fighters; 'mine'/'theirs' are whole parties
    ordered with the active one first. Stale records are kept but flagged by
    their `age`, since a benched character legitimately stops updating.
    """
    mine, theirs = [], []
    deduped = dedupe(snap)

    # A mirror match (both sides pick the same character) can put two
    # DIFFERENT fighters' dedupe keys ("Pl02Bakugo" and its collision
    # partner "Pl02Bakugo#1" - see dedupe()) under the SAME pinned name,
    # since pin matching goes by name alone. Letting both claim the pin
    # would fold the opponent's identically-named fighter into our own
    # team. Only the one whose (orientation-corrected) enemy flag actually
    # reads as ours may use it; the other is resolved the normal unpinned
    # way instead (see the roster.pinned fallback below), same as any other
    # opponent.
    pin_blocked = set()
    if roster is not None and roster.pinned:
        by_pin_name = {}
        for key, rec in deduped.items():
            name = roster._pin_name_for(key)
            if name:
                by_pin_name.setdefault(name, []).append((key, rec))
        for name, cands in by_pin_name.items():
            if len(cands) <= 1:
                continue
            def _reads_as_mine(kr):
                flagged = bool(kr[1].get("enemy"))
                if roster.flipped:
                    flagged = not flagged
                return 1 if flagged else 0    # 0 sorts first: reads as ours
            cands.sort(key=_reads_as_mine)
            for key, _ in cands[1:]:
                pin_blocked.add(key)

    for key, rec in deduped.items():
        # Stale records are lua_States left over from an earlier match; they
        # still report their old allegiance and will corrupt the split. But a
        # BENCHED partner goes stale too - it stops running scripts - and
        # dropping it left the team list holding only the point fighter for
        # most of every match, so the tag policy had nobody to switch to. A
        # stale record is kept when the roster saw it live this session.
        if rec.get("age", 1e9) > max_age:
            if roster is None or not roster.recent(key):
                continue
        # A roster (if supplied) remembers benched team-mates whose age has
        # climbed; otherwise this tick's flag is used, which is reliable.
        # Stamp the dedupe key so callers can refer back to this record.
        # Unnamed characters share id='?', so the key is the only usable
        # handle for them.
        rec["key"] = key
        # Known teams: drop only STALE records that match no pin (leftovers
        # from an earlier match). A LIVE unmatched record is kept and sided by
        # the flag - Kaminari on point runs as Pl07KaminariState, and dropping
        # it left a bot with no fighter at all, standing still.
        if (roster is not None and roster.exclusive and roster.pin_of(key) is None
                and rec.get("age", 1e9) > 60):
            continue
        side = roster.side(key, allow_pin=key not in pin_blocked) if roster else None
        if side is None:
            if roster is not None and roster.pinned:
                # We know our own team by name (--pick already checked and
                # failed to match this key via roster.side above) - anything
                # else is the opponent's, full stop. The enemy-flag fallback
                # below is only trustworthy once orientation evidence has
                # accumulated, and its main signal (ai=1 marking the CPU) has
                # nothing to key off at all when the opponent is a human, not
                # a CPU (Human vs Bot) - theirs stayed empty forever and the
                # match never left "waiting".
                side = "theirs"
            else:
                # roster.side() withheld because confidence sat under its
                # threshold (min_confidence=0.6) - a deliberate "do not
                # guess" for a character that can legitimately read as
                # either side. But the fallback used to be the RAW, SINGLE-
                # TICK enemy flag, which is far noisier than the vote
                # margin it just gave up on (one match saw it flip 100-300
                # times) - so a character sitting at, say, 55/45 would still
                # get sided almost coin-flip per tick, drifting from mine to
                # theirs and back over the course of a match. The vote
                # majority, even below the confidence bar, is far steadier;
                # only fall back to the instantaneous flag for a character
                # with no votes at all yet (just appeared this tick).
                v = roster.votes.get(key) if roster is not None else None
                if v and (v["theirs"] + v["mine"]) > 0:
                    flagged = v["theirs"] > v["mine"]
                else:
                    flagged = bool(rec.get("enemy"))
                    if roster is not None and roster.flipped:
                        flagged = not flagged
                side = "theirs" if flagged else "mine"
        (theirs if side == "theirs" else mine).append(rec)
    # Toga's copy: a Plus Ultra copies her own partner, a blood transform an
    # opponent. Decide by which team the copied name belongs to - pinned
    # names first (the bot knows both line-ups), else whichever team list
    # holds a fighter of that name.
    from .roster import base_name as _bn
    for team in (mine, theirs):
        for rec in team:
            cid = rec.get("copy_of")
            if not cid or rec.get("copy_pu"):
                continue
            own = None
            if roster is not None and roster.pinned:
                pc, pt = roster.pin_of(cid), roster.pin_of(rec.get("key") or "")
                if pc and pt:
                    own = pc == pt
            if own is None:
                cb = _bn(cid)
                own = bool(cb) and any(r is not rec and _bn(r.get("key")) == cb
                                       for r in team)
            if own:
                rec["copy_pu"] = 1
    # The point character is active and NOT a sidekick: a partner called in
    # for an assist also reports active=1 for a moment, and ranking by age
    # alone let `me` jump to the assist and back.
    # A benched record keeps whatever `active` it last reported, so staleness
    # ranks first: the point fighter is always a live one.
    # A fighter leaving the stage (SidekickReturn: the outgoing one of a
    # Change, or a partner going back after an assist) still reads active for
    # a while: 10% of farm point switches put `me` on it, and the bot chose
    # attacks for a fighter that was walking off while the newcomer stood.
    key = lambda r: (1 if r.get("age", 1e9) > max_age else 0,
                     1 if r.get("act") == "SidekickReturn" else 0,
                     0 if r.get("active") else 1, 1 if r.get("sk") else 0,
                     1 if r.get("dead") else 0, r.get("age", 1e9))
    mine.sort(key=key)
    theirs.sort(key=key)
    live = lambda xs: [r for r in xs if r.get("age", 0) <= max_age]
    point = lambda xs: (live(xs) or [None])[0]
    return {
        "me": point(mine),
        "enemy": point(theirs),
        "mine": mine,
        "theirs": theirs,
        "mine_live": live(mine),
        "theirs_live": live(theirs),
    }


def reset(hook):
    return hook.clear_samples()


def states_seen(hook):
    lines = hook.samples().splitlines()
    if not lines:
        return 0
    for tok in lines[0].lstrip("#").split():
        if tok.startswith("states="):
            return int(tok.split("=", 1)[1])
    return 0

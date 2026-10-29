-- 16-beat loop
-- energy: 1
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local LOOP_BARS = 4
local DRIFT_SHARE = 14 / 16
local WASH_LEVEL = 0.08
local MOVER_LEVEL = 0.4
local SPREAD_DEGREES = 30

local function smooth(t) return t * t * (3 - 2 * t) end

local function spread(phase)
    if phase < DRIFT_SHARE then return smooth(phase / DRIFT_SHARE) end
    return 1 - smooth((phase - DRIFT_SHARE) / (1 - DRIFT_SHARE))
end

return {
    render = function(ctx)
        local energy = sf.energy(ctx)
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local width = SPREAD_DEGREES * sf.lerp(0.5, 1, energy)
            * spread(sf.phrase_phase(ctx.beat_position, LOOP_BARS))
        return sf.scene {
            sf.colour(sf.group("wash"), palette.cold),
            sf.dimmer(sf.group("wash"), WASH_LEVEL * level),
            sf.colour(sf.group("strip"), palette.black),
            sf.dimmer(sf.group("strip"), 0),
            sf.colour(sf.group("movers"), palette.cold),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * level),
            sf.phaser(sf.group("movers"), ctx, { shape = "fan", size = width })
        }
    end
}

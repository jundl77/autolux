-- 16-beat loop
-- energy: 1
-- family: cold
-- movement: sweep
local palette = sf.palette("techno")
local LOOP_BARS = 4
local DRIFT_SHARE = 14 / 16
local WASH_LEVEL = 0.08
local MOVER_LEVEL = 0.4
local CENTRE_PAN, TILT = 0.5, 0.6
local PAN_SWING = 0.3

local function smooth(t) return t * t * (3 - 2 * t) end

local function spread(phase)
    if phase < DRIFT_SHARE then return smooth(phase / DRIFT_SHARE) end
    return 1 - smooth((phase - DRIFT_SHARE) / (1 - DRIFT_SHARE))
end

return {
    render = function(ctx)
        local energy = ctx.normalized_rms_level or 0.6
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local pan = PAN_SWING * sf.lerp(0.5, 1, energy)
            * spread(sf.phrase_phase(ctx.beat_position, LOOP_BARS))
        return sf.scene {
            sf.colour(sf.group("wash"), palette.cold),
            sf.dimmer(sf.group("wash"), WASH_LEVEL * level),
            sf.colour(sf.group("strip"), palette.black),
            sf.dimmer(sf.group("strip"), 0),
            sf.colour(sf.group("movers"), palette.cold),
            sf.each(sf.group("movers"), function(mover, index)
                return {
                    sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * pan, TILT),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

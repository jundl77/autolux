-- 16-beat loop
-- energy: 1
-- family: accent
-- movement: still
local palette = sf.palette("techno")
local LOOP_BARS = 4
local IGNITE_SHARE = 1 / 32
local WASH_PEAK, STRIP_PEAK = 0.6, 0.18
local MOVER_LEVEL = 0.2
local CENTRE_PAN, TILT = 0.5, 0.6

local function glow(phase)
    if phase < IGNITE_SHARE then return phase / IGNITE_SHARE end
    local fade = 1 - (phase - IGNITE_SHARE) / (1 - IGNITE_SHARE)
    return fade * fade
end

return {
    render = function(ctx)
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local envelope = glow(sf.phrase_phase(ctx.beat_position, LOOP_BARS)) * level
        return sf.scene {
            sf.colour(sf.all(), palette.accent),
            sf.dimmer(sf.group("wash"), WASH_PEAK * envelope),
            sf.dimmer(sf.group("strip"), STRIP_PEAK * envelope),
            sf.pan_tilt(sf.group("movers"), CENTRE_PAN, TILT),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * level)
        }
    end
}

-- 16-beat loop, no beat pulses or strobe.
-- energy: 2
-- family: accent
-- movement: still
-- wash: accent, ignites over the first half beat of the phrase, then fades out across
-- the rest of the 16 beats.
-- strip: all pixels mirror the wash envelope at a low level.
-- movers: accent, dim, still at centre. Palette slots: accent.
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

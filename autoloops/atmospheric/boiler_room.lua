-- 16-beat loop, no beat pulses or strobe.
-- wash: primary around 0.2, slow breathe with its own phase per wash so the room never pulses as one.
-- strip: amber ember flicker from two slow sines per pixel at different periods.
-- movers: primary, low and close, parked mirrored. Palette slots: primary, secondary.
local palette = sf.palette("techno")
local LOOP_BEATS = 16
local WASH_LOW, WASH_HIGH = 0.14, 0.26
local EMBER_LOW, EMBER_HIGH = 0.04, 0.24
local MOVER_LEVEL = 0.22
local CENTRE_PAN, PARK_PAN, TILT = 0.5, 0.1, 0.68
-- Both periods divide 16, so the flicker repeats exactly with the loop.
local EMBER_SLOW, EMBER_FAST = LOOP_BEATS / 2, LOOP_BEATS / 3

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            sf.each(sf.group("wash"), function(wash, index, count)
                local breathe = sf.osc.sine(beat, LOOP_BEATS, WASH_LOW, WASH_HIGH, (index - 1) / count)
                return sf.dimmer(wash, breathe * level)
            end),
            sf.colour(sf.group("strip"), palette.secondary),
            sf.each(sf.group("strip"), function(pixel, index)
                -- Unrelated offsets per pixel keep neighbours out of step.
                local ember = 0.6 * sf.osc.sine(beat, EMBER_SLOW, 0, 1, index * 0.37)
                    + 0.4 * sf.osc.sine(beat, EMBER_FAST, 0, 1, index * 0.61)
                return sf.dimmer(pixel, sf.lerp(EMBER_LOW, EMBER_HIGH, ember) * level)
            end),
            sf.colour(sf.group("movers"), palette.primary),
            sf.each(sf.group("movers"), function(mover, index)
                return {
                    sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * PARK_PAN, TILT),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

-- 16-beat loop
-- energy: 1
-- movement: still
-- texture: wash
local palette = sf.palette()
local LOOP_BEATS = 16
local BAR_BEATS = 4
local WASH_LOW, WASH_HIGH = 0.05, 0.35
local EMBER_LOW, EMBER_HIGH = 0.04, 0.25
local EMBER_PIXELS = { [4] = true, [5] = true }
local MOVER_LEVEL = 0.2
local CENTRE_PAN, TILT = 0.5, 0.6

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local ember = sf.osc.sine(beat, LOOP_BEATS / 2, EMBER_LOW, EMBER_HIGH)
        return sf.scene {
            sf.colour(sf.all(), palette.accent),
            sf.dimmer(sf.group("wash"), sf.osc.sine(beat, BAR_BEATS, WASH_LOW, WASH_HIGH) * level),
            sf.each(sf.group("strip"), function(pixel, index)
                return sf.dimmer(pixel, EMBER_PIXELS[index] and ember * level or 0)
            end),
            sf.pan_tilt(sf.group("movers"), CENTRE_PAN, TILT),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * level)
        }
    end
}

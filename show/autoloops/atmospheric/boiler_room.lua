-- 16-beat loop
-- energy: 2
-- movement: still
-- texture: mixed
local palette = sf.palette()
local LOOP_BEATS = 16
local WASH_LOW, WASH_HIGH = 0.14, 0.26
local EMBER_LOW, EMBER_HIGH = 0.04, 0.24
local MOVER_LEVEL = 0.22
local PARK_DEGREES = 8
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
                local ember = 0.6 * sf.osc.sine(beat, EMBER_SLOW, 0, 1, index * 0.37)
                    + 0.4 * sf.osc.sine(beat, EMBER_FAST, 0, 1, index * 0.61)
                return sf.dimmer(pixel, sf.lerp(EMBER_LOW, EMBER_HIGH, ember) * level)
            end),
            sf.colour(sf.group("movers"), palette.primary),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * level),
            sf.phaser(sf.group("movers"), ctx, { shape = "fan", size = PARK_DEGREES })
        }
    end
}

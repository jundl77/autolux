-- 16-beat loop
-- energy: 1
-- movement: sweep
-- texture: mixed
local palette = sf.palette()
local TIDE_BEATS = 16
local TIDE_DEGREES = 8
local WASH_LOW, WASH_HIGH = 0.05, 0.22
local STRIP_LOW, STRIP_HIGH, MOVER_LEVEL = 0.02, 0.24, 0.3

local function crest(beat, index, count)
    return sf.osc.sine(beat, TIDE_BEATS, 0, 1, -(index - 1) / (2 * count))
end

return {
    render = function(ctx)
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local beat = sf.section_beats(ctx)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            sf.each(sf.group("wash"), function(wash, index, count)
                return sf.dimmer(wash, sf.lerp(WASH_LOW, WASH_HIGH, crest(beat, index, count)) * level)
            end),
            sf.colour(sf.group("strip"), palette.secondary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                return sf.dimmer(pixel, sf.lerp(STRIP_LOW, STRIP_HIGH, crest(beat, index, count)) * level)
            end),
            sf.colour(sf.group("movers"), palette.secondary),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * level),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "tilt_wave", tilt_size = TIDE_DEGREES, measure = TIDE_BEATS, phase = 0.5
            })
        }
    end
}

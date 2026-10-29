-- 8-beat loop
-- energy: 1
-- movement: still
-- texture: pixel
local palette = sf.palette()
local BREATH_BEATS = 8
local PIXEL_LOW, PIXEL_HIGH = 0.04, 0.45

-- Negative space: one cell breathing in a dark room resets the eye before the next build.
return {
    render = function(ctx)
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local breath = sf.osc.sine(ctx.beat_position, BREATH_BEATS, PIXEL_LOW, PIXEL_HIGH)
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), 0),
            sf.dimmer(sf.group("movers"), 0),
            sf.phaser(sf.group("movers"), ctx, { shape = "fan", size = 0 }),
            sf.each(sf.group("strip"), function(pixel, index, count)
                return sf.dimmer(pixel, math.abs(index - (count + 1) / 2) < 1 and breath * level or 0)
            end)
        }
    end
}

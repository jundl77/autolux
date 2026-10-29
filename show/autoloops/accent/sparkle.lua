-- Seeded sparkle on the strip in the second colour on every off-beat eighth, over the groove.
-- strobe: no
-- intensity: htp
-- movement: still
-- texture: pixel
local palette = sf.palette()
local SPARK_SHARE, SPARK_WIDTH_BEATS, PEAK = 0.35, 0.3, 0.9

return {
    render = function(ctx)
        local offbeat = ctx.beat_position - 0.5
        local spark = sf.on_beat(offbeat, SPARK_WIDTH_BEATS)
        local scatter = math.floor(offbeat)
        return sf.scene {
            sf.colour(sf.group("strip"), palette.secondary),
            sf.each(sf.group("strip"), function(pixel, index)
                local lit = sf.random(ctx, index, scatter) < SPARK_SHARE and spark or 0
                return sf.dimmer(pixel, PEAK * lit)
            end)
        }
    end
}

-- Seeded sparkle on the strip in the second colour, a fresh scatter every sixteenth.
-- strobe: no
-- intensity: ltp
local palette = sf.palette()
local SPARK_SHARE, SPARK_WIDTH_BEATS = 0.25, 0.2
local FLOOR, PEAK = 0.03, 0.9

return {
    render = function(ctx)
        local sixteenth = math.floor(ctx.beat_position * 4)
        local spark = sf.on_beat(ctx, SPARK_WIDTH_BEATS, 0.25)
        return sf.scene {
            sf.colour(sf.group("strip"), palette.secondary),
            sf.each(sf.group("strip"), function(pixel, index)
                local lit = sf.random(ctx, index, sixteenth) < SPARK_SHARE and spark or 0
                return sf.dimmer(pixel, sf.lerp(FLOOR, PEAK, lit))
            end)
        }
    end
}

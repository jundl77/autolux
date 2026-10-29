-- 8-beat loop
-- energy: 2
-- movement: sweep
-- texture: wash
local palette = sf.palette()
local BREATH_BEATS = 8
local FAN_DEGREES, LOW_DEGREES, CEILING_DEGREES = 10, 5, 60
local STRIP_TOP, STRIP_START = 0.9, 0.2

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local washes, roll = sf.wash_build(ctx, build, { pattern = "saw" })
        local breath = sf.lerp(sf.osc.sine(sf.section_beats(ctx), BREATH_BEATS), 1, build.finale)
        local movers = sf.group("movers")
        return sf.scene {
            washes,
            sf.colour(sf.group("strip"), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local reach = (breath - sf.from_centre(index, count)) * count / 2 + 1
                return sf.dimmer(pixel, STRIP_TOP * sf.lerp(STRIP_START, 1, roll.climb) * math.max(0, math.min(1, reach)))
            end),
            sf.colour(movers, roll.mover_colour),
            sf.dimmer(movers, roll.movers),
            sf.phaser(movers, ctx, {
                shape = "fan", size = FAN_DEGREES, measure = build.measure,
                centre_tilt = sf.lerp(LOW_DEGREES, CEILING_DEGREES, roll.climb)
            })
        }
    end
}

-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: wash
local FAN_DEGREES, RISE_DEGREES = 20, 15

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local washes, roll = sf.wash_build(ctx, build, { pattern = "roll" })
        local strip, movers = sf.group("strip"), sf.group("movers")
        return sf.scene {
            washes,
            sf.colour(strip, roll.colour),
            sf.dimmer(strip, roll.peak * roll.flash),
            sf.colour(movers, roll.mover_colour),
            sf.dimmer(movers, roll.movers),
            sf.phaser(movers, ctx, {
                shape = "fan", size = FAN_DEGREES, measure = build.measure, centre_tilt = RISE_DEGREES * roll.climb
            })
        }
    end
}

-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: mixed
local palette = sf.palette()
local STRIP_LOW, STRIP_TOP, MOVER_WAIT = 0.5, 0.9, 0.15
local FAN_DEGREES, LIFT_DEGREES = 15, 35

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local washes, roll = sf.wash_build(ctx, build, { pattern = "roll", stab = true })
        local strip_in, stabs_in = build.stage >= 1 or roll.last_bar, build.stage >= 2 or roll.last_bar
        local strip_level = roll.last_bar and STRIP_TOP or sf.lerp(STRIP_LOW, STRIP_TOP, roll.climb)
        local movers = sf.group("movers")
        return sf.scene {
            washes,
            sf.colour(sf.group("strip"), palette.primary),
            sf.dimmer(sf.group("strip"), strip_in and strip_level or 0),
            sf.colour(movers, roll.mover_colour),
            sf.dimmer(movers, stabs_in and roll.movers or MOVER_WAIT),
            sf.phaser(movers, ctx, {
                shape = "fan", size = FAN_DEGREES, measure = build.measure, centre_tilt = LIFT_DEGREES * roll.climb
            })
        }
    end
}

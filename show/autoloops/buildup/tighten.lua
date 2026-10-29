-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local WIDE_DEGREES, TIGHT_DEGREES, LIFT_DEGREES = 25, 3, 20
local STRIP_TOP, STRIP_GLOW, DOT_SHARE = 0.6, 0.25, 0.19

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local washes, roll = sf.wash_build(ctx, build, { pattern = "saw" })
        local converge = sf.lerp(roll.climb, 1, build.finale)
        local movers = sf.group("movers")
        return sf.scene {
            washes,
            sf.colour(sf.group("strip"), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local dot = math.max(STRIP_GLOW, sf.mirrored_spot(index, count, 0, DOT_SHARE))
                return sf.dimmer(pixel, STRIP_TOP * build.drive * dot)
            end),
            sf.colour(movers, roll.mover_colour),
            sf.dimmer(movers, roll.movers),
            sf.phaser(movers, ctx, {
                shape = "circle", size = sf.lerp(WIDE_DEGREES, TIGHT_DEGREES, converge), measure = build.measure,
                at = sf.shared_focus(movers), centre_tilt = LIFT_DEGREES * converge
            })
        }
    end
}

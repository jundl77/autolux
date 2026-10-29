-- 8-beat loop
-- energy: 2
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local HOLD_UNTIL = 0.5
local NARROW_DEGREES, WIDE_DEGREES, RISE_DEGREES = 8, 45, 15
local STRIP_TOP, MOVER_LOW, MOVER_TOP = 0.9, 0.6, 0.85
local LAP_BEATS, TAIL_SHARE, STRIP_START = 2, 0.5, 0.2

local function letting_go(build)
    local open = math.max(0, (build.tension - HOLD_UNTIL) / (1 - HOLD_UNTIL))
    return open + (1 - open) * build.finale
end

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local washes, roll = sf.wash_build(ctx, build, { pattern = "chase", stab = true })
        local open = letting_go(build)
        local head = ctx.beat_position / LAP_BEATS % 1
        local movers = sf.group("movers")
        return sf.scene {
            washes,
            sf.colour(sf.group("strip"), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local dot = sf.is_middle(index, count) and 1 or 0
                local chase = sf.chase(index, count, head, TAIL_SHARE)
                return sf.dimmer(pixel, STRIP_TOP * sf.lerp(STRIP_START, 1, roll.climb) * sf.lerp(dot, chase, open))
            end),
            sf.colour(movers, roll.mover_colour),
            sf.dimmer(movers, roll.movers or MOVER_TOP * sf.lerp(MOVER_LOW, 1, roll.climb)),
            sf.phaser(movers, ctx, {
                shape = "fan", size = sf.lerp(NARROW_DEGREES, WIDE_DEGREES, roll.climb), measure = build.measure,
                centre_tilt = RISE_DEGREES * roll.climb
            })
        }
    end
}

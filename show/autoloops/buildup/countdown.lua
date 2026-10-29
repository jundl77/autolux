-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: pixel
local palette = sf.palette()
local COUNT_BEATS = 8
local STRIP_TOP, MOVER_LOW, MOVER_TOP = 0.9, 0.6, 0.85
local FAN_DEGREES, LIFT_DEGREES = 15, 35

local function filled(build)
    local meter = 0.5 * build.step
    if not sf.drop_within(build, COUNT_BEATS) then return meter end
    local steps = math.max(1, math.min(COUNT_BEATS, math.ceil(build.span)))
    local counted = math.max(0, steps - math.ceil(build.left) + 1) / steps
    return meter + counted * (1 - meter)
end

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local washes, roll = sf.wash_build(ctx, build, { pattern = "chase" })
        local fill = filled(build)
        local movers = sf.group("movers")
        return sf.scene {
            washes,
            sf.colour(sf.group("strip"), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local ranks = math.ceil(count / 2)
                local from_end = ranks - 1 - math.floor(math.abs(index - (count + 1) / 2))
                return sf.dimmer(pixel, STRIP_TOP * math.max(0, math.min(1, fill * ranks - from_end)))
            end),
            sf.colour(movers, roll.mover_colour),
            sf.dimmer(movers, roll.movers or MOVER_TOP * sf.lerp(MOVER_LOW, 1, roll.climb)),
            sf.phaser(movers, ctx, {
                shape = "fan", size = FAN_DEGREES, measure = build.measure, centre_tilt = LIFT_DEGREES * roll.climb
            })
        }
    end
}

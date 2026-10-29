-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: pixel
local palette = sf.palette()
local COUNT_BEATS = 8
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85
local TICK_WIDTH_BEATS = 0.45
local FAN_DEGREES, LIFT_DEGREES = 12, 35

local function meter_and_count(build)
    local meter = 0.5 * build.step
    if not sf.drop_within(build, COUNT_BEATS) then return meter, 0 end
    local steps = math.max(1, math.min(COUNT_BEATS, math.ceil(build.span)))
    local counted = math.max(0, steps - math.ceil(build.left) + 1) / steps
    return meter, counted * (1 - meter)
end

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local meter, closing = meter_and_count(build)
        local counting = sf.drop_within(build, COUNT_BEATS)
        local tick = sf.on_beat(ctx, TICK_WIDTH_BEATS, counting and 1 or ctx.beats_per_bar)
        local landing = 1 - sf.approach(build, ctx.beats_per_bar)
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_PEAK * build.drive * sf.lerp(0.3, 1, tick)),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local ranks = math.ceil(count / 2)
                local rank = math.floor(math.abs(index - (count + 1) / 2))
                local from_meter = math.max(0, math.min(1, meter * ranks - rank))
                local from_ends = math.max(0, math.min(1, closing * ranks - (ranks - 1 - rank)))
                local lit = math.min(1, from_meter + from_ends)
                return sf.dimmer(pixel, STRIP_PEAK * lit)
            end),
            sf.dimmer(sf.group("movers"), MOVER_PEAK * build.drive * sf.lerp(0.5, 1, tick)),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "fan", size = FAN_DEGREES * landing, measure = build.measure,
                centre_tilt = LIFT_DEGREES * build.drive * landing
            })
        }
    end
}

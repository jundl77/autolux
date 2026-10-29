-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: pixel
local palette = sf.palette()
local COUNT_BEATS, TOP_UP_BEATS = 8, 16
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85
local TICK_WIDTH_BEATS = 0.45
local LIFT_DEGREES = 35

-- The bar fills from the centre one step per downbeat as the build climbs, tops up while a
-- cued drop approaches, then counts back one step per beat so the last cell goes at the dip.
local function lit_share(build)
    local left = build.left
    if left == nil or left > TOP_UP_BEATS then return build.step end
    local count = math.ceil(left)
    if count <= COUNT_BEATS then return count / COUNT_BEATS end
    return build.step + (1 - build.step) * (TOP_UP_BEATS - count) / (TOP_UP_BEATS - COUNT_BEATS)
end

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local share = lit_share(build)
        local counting = build.left ~= nil and build.left <= COUNT_BEATS
        local tick = sf.on_beat(ctx, TICK_WIDTH_BEATS, counting and 1 or ctx.beats_per_bar)
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_PEAK * build.drive * sf.lerp(0.3, 1, tick)),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local rank = math.floor(math.abs(index - (count + 1) / 2))
                local lit = math.max(0, math.min(1, share * math.ceil(count / 2) - rank))
                return sf.dimmer(pixel, STRIP_PEAK * lit * sf.lerp(0.5, 1, build.drive))
            end),
            sf.dimmer(sf.group("movers"), MOVER_PEAK * build.drive * sf.lerp(0.5, 1, tick)),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "fan", size = 0, centre_tilt = LIFT_DEGREES * build.drive
            })
        }
    end
}

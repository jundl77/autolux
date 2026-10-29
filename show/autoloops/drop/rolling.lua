-- 8-beat loop
-- energy: 2
-- movement: sweep
-- texture: pixel
local palette = sf.palette()
local LAP_BEATS = 2
local TAIL_SHARE = 0.375
local KICK_WIDTH_BEATS, ROLL_WIDTH_BEATS, ROLL_LEVEL = 0.4, 0.2, 0.35
local FLOOR_QUIET, FLOOR_LOUD = 0.05, 0.2
local WASH_PEAK, STRIP_PEAK, MOVER_FLOOR, MOVER_PEAK = 0.9, 0.9, 0.6, 0.85
local CIRCLE_DEGREES, CIRCLE_MEASURE = 10, 8

return {
    render = function(ctx)
        local gain, floor, energy = sf.drop_levels(ctx, FLOOR_QUIET, FLOOR_LOUD)
        local eighth = sf.on_beat(ctx, ROLL_WIDTH_BEATS, 0.5)
        local kick = math.max(sf.on_beat(ctx, KICK_WIDTH_BEATS), ROLL_LEVEL * eighth)
        local head = ctx.beat_position / LAP_BEATS % 1
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, WASH_PEAK, kick) * gain),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local lit, behind = sf.chase(index, count, head, TAIL_SHARE)
                return {
                    sf.colour(pixel, behind < 1 and palette.secondary or palette.primary),
                    sf.dimmer(pixel, STRIP_PEAK * lit * gain)
                }
            end),
            sf.colour(sf.group("movers"), palette.secondary),
            sf.dimmer(sf.group("movers"), sf.lerp(MOVER_FLOOR, MOVER_PEAK, eighth) * gain),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "circle", size = CIRCLE_DEGREES * sf.lerp(0.6, 1, energy), measure = CIRCLE_MEASURE
            })
        }
    end
}

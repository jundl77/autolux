-- The washes take turns on the eighths, rolling left to right and back in the second colour.
-- strobe: no
-- intensity: ltp
local palette = sf.palette()
local HIT_WIDTH_BEATS = 0.35
local FLOOR, PEAK = 0.1, 0.85

return {
    render = function(ctx)
        local eighth = math.floor(ctx.beat_position * 2)
        local hit = sf.on_beat(ctx, HIT_WIDTH_BEATS, 0.5)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.secondary),
            sf.each(sf.group("wash"), function(wash, index, count)
                local lap = math.max(1, 2 * (count - 1))
                local step = eighth % lap
                local lit = (step < count and step + 1 or lap - step + 1) == index
                return sf.dimmer(wash, sf.lerp(FLOOR, PEAK, lit and hit or 0))
            end)
        }
    end
}

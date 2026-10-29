-- On each off-beat eighth one wash flashes the second colour, rolling left to right and back.
-- strobe: no
-- intensity: htp
-- movement: still
-- texture: wash
local palette = sf.palette()
local HIT_WIDTH_BEATS, PEAK = 0.35, 0.9

return {
    render = function(ctx)
        local offbeat = ctx.beat_position - 0.5
        local hit = sf.on_beat(offbeat, HIT_WIDTH_BEATS)
        local step = math.floor(offbeat)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.secondary),
            sf.each(sf.group("wash"), function(wash, index, count)
                local lap = math.max(1, 2 * (count - 1))
                local at = step % lap
                local lit = (at < count and at + 1 or lap - at + 1) == index
                return sf.dimmer(wash, lit and PEAK * hit or 0)
            end)
        }
    end
}

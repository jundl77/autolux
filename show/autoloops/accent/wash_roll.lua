-- On each off-beat a quarter of the washes flash the second colour, rolling across and back.
-- strobe: no
-- intensity: htp
-- movement: still
-- texture: wash
local palette = sf.palette()
local HIT_WIDTH_BEATS, PEAK = 0.35, 0.9
local LAP_STEPS, LIT_SHARE = 8, 0.25

return {
    render = function(ctx)
        local offbeat = ctx.beat_position - 0.5
        local hit = sf.on_beat(offbeat, HIT_WIDTH_BEATS)
        local phase = math.floor(offbeat) % LAP_STEPS / LAP_STEPS
        local across = 1 - math.abs(2 * phase - 1)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.secondary),
            sf.each(sf.group("wash"), function(wash, index, count)
                local width = math.ceil(LIT_SHARE * count)
                local first = math.floor(across * (count - width) + 0.5)
                local lit = index > first and index <= first + width
                return sf.dimmer(wash, lit and PEAK * hit or 0)
            end)
        }
    end
}

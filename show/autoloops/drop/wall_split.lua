-- 8-beat loop
-- energy: 3
-- movement: snap
-- texture: wash
local palette = sf.palette()
local KICK_WIDTH_BEATS, WIPE_PIXELS = 0.4, 1.5
local FLOOR_QUIET, FLOOR_LOUD = 0.06, 0.25
local WASH_PEAK, STRIP_PEAK, MOVER_FLOOR, MOVER_PEAK = 0.9, 0.9, 0.35, 0.85
local CROSS_DEGREES, CROSS_MEASURE = 18, 4
local OUTER_FROM = 0.5

local function from_centre(index, count)
    return count > 1 and math.abs(2 * (index - 1) / (count - 1) - 1) or 0
end

-- Outer members carry the second colour and inner ones the first, so the wall reads as two
-- layers; the strip wipes outside-in over each bar to tie the halves together.
local function split(index, count)
    return from_centre(index, count) > OUTER_FROM and palette.secondary or palette.primary
end

return {
    render = function(ctx)
        local energy = sf.energy(ctx)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local kick = sf.on_beat(ctx, KICK_WIDTH_BEATS)
        local wipe = 1 - sf.phrase_phase(ctx, 1)
        return sf.scene {
            sf.each(sf.group("wash"), function(wash, index, count)
                local level = sf.lerp(floor, WASH_PEAK, kick) * gain
                return { sf.colour(wash, split(index, count)), sf.dimmer(wash, level) }
            end),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local distance = math.abs(from_centre(index, count) - wipe) * (count - 1) / 2
                local lit = math.max(0, 1 - distance / WIPE_PIXELS)
                return { sf.colour(pixel, split(index, count)), sf.dimmer(pixel, STRIP_PEAK * lit * gain) }
            end),
            sf.colour(sf.group("movers"), palette.secondary),
            sf.dimmer(sf.group("movers"), sf.lerp(MOVER_FLOOR, MOVER_PEAK, kick) * gain),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "cross", size = CROSS_DEGREES * sf.lerp(0.6, 1, energy), measure = CROSS_MEASURE
            })
        }
    end
}

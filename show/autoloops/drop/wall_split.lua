-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: wash
local palette = sf.palette()
local KICK_WIDTH_BEATS, WIPE_SHARE = 0.4, 0.19
local FLOOR_QUIET, FLOOR_LOUD = 0.06, 0.25
local WASH_PEAK, STRIP_PEAK, MOVER_FLOOR, MOVER_PEAK = 0.9, 0.9, 0.6, 0.85
local CROSS_DEGREES, CROSS_MEASURE = 18, 4
local OUTER_FROM = 0.5

local function outer_in_b(target)
    return sf.each(target, function(member, index, count)
        if sf.from_centre(index, count) > OUTER_FROM then return sf.colour(member, palette.secondary) end
    end)
end

return {
    render = function(ctx)
        local energy = sf.energy(ctx)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local kick = sf.on_beat(ctx, KICK_WIDTH_BEATS)
        local wipe = 1 - sf.phrase_phase(ctx, 1)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            outer_in_b(sf.group("wash")),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, WASH_PEAK, kick) * gain),
            sf.colour(sf.group("strip"), palette.primary),
            outer_in_b(sf.group("strip")),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local distance = math.abs(sf.from_centre(index, count) - wipe) * (count - 1) / 2
                local lit = math.max(0, 1 - distance / sf.pixels(WIPE_SHARE, count))
                return sf.dimmer(pixel, STRIP_PEAK * lit * gain)
            end),
            sf.colour(sf.group("movers"), palette.secondary),
            sf.dimmer(sf.group("movers"), sf.lerp(MOVER_FLOOR, MOVER_PEAK, kick) * gain),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "cross", size = CROSS_DEGREES * sf.lerp(0.6, 1, energy), measure = CROSS_MEASURE
            })
        }
    end
}

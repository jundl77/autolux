-- The bar-16 fill, bigger than bar 8: every group rolls, the strip wipes in, the beams cross.
-- strobe: no
-- intensity: ltp
-- movement: sweep
-- texture: mixed
local palette = sf.palette()
local ROLL_WIDTH_SHARE, WIPE_SHARE = 0.5, 0.19
local CROSS_DEGREES = 25
local FLOOR, PEAK = 0.15, 0.9

-- Eighths on the washes and beams, the strip doubling to sixteenths for the second half (a
-- sixteenth on a big source is a strobe); the last eighth stays dark so the next phrase's one
-- reads as a landing, not a continuation.
local function roll(ctx, remaining, step)
    if remaining <= 0.5 then return 0 end
    return sf.lerp(FLOOR, PEAK, sf.on_beat(ctx, ROLL_WIDTH_SHARE * step, step))
end

return {
    render = function(ctx)
        local progress, left = sf.overlay_progress(ctx)
        local level = roll(ctx, left, 0.5)
        local strip_level = roll(ctx, left, progress < 0.5 and 0.5 or 0.25)
        local movers = sf.group("movers")
        return sf.scene {
            sf.colour(sf.group("wash"), palette.accent),
            sf.dimmer(sf.group("wash"), level),
            sf.colour(sf.group("strip"), palette.accent),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local distance = math.abs(sf.from_centre(index, count) - (1 - progress)) * (count - 1) / 2
                local wiped = math.max(0, 1 - distance / sf.pixels(WIPE_SHARE, count))
                return sf.dimmer(pixel, strip_level * wiped)
            end),
            sf.colour(movers, palette.accent),
            sf.dimmer(movers, level),
            sf.phaser(movers, ctx, { shape = "cross", size = CROSS_DEGREES * math.sin(math.pi * progress) })
        }
    end
}

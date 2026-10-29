-- The bar-16 fill, bigger than bar 8: every group rolls, the strip wipes in, the beams cross.
-- strobe: no
-- intensity: ltp
-- movement: sweep
-- texture: mixed
local palette = sf.palette()
local ROLL_WIDTH_SHARE, WIPE_PIXELS = 0.5, 1.5
local CROSS_DEGREES = 25
local FLOOR, PEAK = 0.15, 0.9

-- Eighths, then sixteenths; the last eighth stays dark so the next phrase's one reads as a
-- landing, not a continuation.
local function roll(ctx, progress, remaining)
    if remaining <= 0.5 then return 0 end
    local step = progress < 0.5 and 0.5 or 0.25
    return sf.lerp(FLOOR, PEAK, sf.on_beat(ctx, ROLL_WIDTH_SHARE * step, step))
end

return {
    render = function(ctx)
        local progress, left = sf.overlay_progress(ctx)
        local level = roll(ctx, progress, left)
        local movers = sf.group("movers")
        return sf.scene {
            sf.colour(sf.group("wash"), palette.secondary),
            sf.dimmer(sf.group("wash"), level),
            sf.colour(sf.group("strip"), palette.secondary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local distance = math.abs(sf.from_centre(index, count) - (1 - progress)) * (count - 1) / 2
                return sf.dimmer(pixel, level * math.max(0, 1 - distance / WIPE_PIXELS))
            end),
            sf.colour(movers, palette.secondary),
            sf.dimmer(movers, level),
            sf.phaser(movers, ctx, { shape = "cross", size = CROSS_DEGREES * math.sin(math.pi * progress) })
        }
    end
}

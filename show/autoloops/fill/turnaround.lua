-- The bar-16 fill, bigger than bar 8: every group rolls, the strip wipes in, the beams cross.
-- strobe: no
-- intensity: ltp
-- movement: sweep
-- texture: mixed
local palette = sf.palette()
local ROLL_WIDTH_SHARE, WIPE_SHARE = 0.5, 0.19
local CROSS_DEGREES = 25
local FLOOR, PEAK = 0.15, 0.9
local EIGHTH, SIXTEENTH, DARK_LANDING_BEATS = 0.5, 0.25, 0.5

-- Only the strip doubles to sixteenths: on a big source a sixteenth reads as a strobe.
local function roll(ctx, remaining, step)
    if remaining <= DARK_LANDING_BEATS then return 0 end
    return sf.lerp(FLOOR, PEAK, sf.on_beat(ctx, ROLL_WIDTH_SHARE * step, step))
end

return {
    render = function(ctx)
        local progress, left = sf.overlay_progress(ctx)
        local level = roll(ctx, left, EIGHTH)
        local strip_level = roll(ctx, left, progress < 0.5 and EIGHTH or SIXTEENTH)
        local movers = sf.group("movers")
        return sf.scene {
            sf.colour(sf.group("wash"), palette.accent),
            sf.dimmer(sf.group("wash"), level),
            sf.colour(sf.group("strip"), palette.accent),
            sf.each(sf.group("strip"), function(pixel, index, count)
                return sf.dimmer(pixel, strip_level * sf.mirrored_spot(index, count, 1 - progress, WIPE_SHARE))
            end),
            sf.colour(movers, palette.accent),
            sf.dimmer(movers, level),
            sf.phaser(movers, ctx, { shape = "cross", size = CROSS_DEGREES * math.sin(math.pi * progress) })
        }
    end
}

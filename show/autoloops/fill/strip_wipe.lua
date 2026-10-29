-- The bar-8 fill: a wipe in the second colour runs outside-in along the strip over the fill.
-- strobe: no
-- intensity: ltp
-- movement: sweep
-- texture: pixel
local palette = sf.palette()
local PEAK, WIPE_SHARE = 0.9, 0.19

return {
    render = function(ctx)
        local progress = sf.overlay_progress(ctx)
        return sf.scene {
            sf.colour(sf.group("strip"), palette.secondary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local distance = math.abs(sf.from_centre(index, count) - (1 - progress)) * (count - 1) / 2
                return sf.dimmer(pixel, PEAK * math.max(0, 1 - distance / sf.pixels(WIPE_SHARE, count)))
            end)
        }
    end
}

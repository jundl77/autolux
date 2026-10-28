-- The bar-8 fill: a wipe in the second colour runs outside-in along the strip over the fill.
-- strobe: no
-- intensity: ltp
local palette = sf.palette()
local PEAK, WIPE_PIXELS = 0.9, 1.5

return {
    render = function(ctx)
        local progress = math.min(1, ctx.autoloop_elapsed_beats / (ctx.overlay_length_beats or 1))
        return sf.scene {
            sf.colour(sf.group("strip"), palette.secondary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local from_centre = count > 1 and math.abs(2 * (index - 1) / (count - 1) - 1) or 0
                local distance = math.abs(from_centre - (1 - progress)) * (count - 1) / 2
                return sf.dimmer(pixel, PEAK * math.max(0, 1 - distance / WIPE_PIXELS))
            end)
        }
    end
}

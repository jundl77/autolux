-- The strip fills in the punch colour from both ends, closing at full where the dip starts.
-- strobe: no
-- intensity: htp
-- movement: still
-- texture: pixel
local palette = sf.palette()
local EDGE_SHARE = 0.125

return {
    render = function(ctx)
        local progress = sf.overlay_progress(ctx)
        return sf.scene {
            sf.colour(sf.group("strip"), palette.accent),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local edge = count > 1 and 2 * sf.pixels(EDGE_SHARE, count) / (count - 1) or 1
                local reached = (sf.from_centre(index, count) - 1 + progress * (1 + edge)) / edge
                return sf.dimmer(pixel, math.max(0, math.min(1, reached)))
            end)
        }
    end
}

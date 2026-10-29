-- The strip fills white from both ends towards the centre, closing at full where the dip starts.
-- strobe: no
-- intensity: htp
-- movement: still
-- texture: pixel
local EDGE_PIXELS = 1

return {
    render = function(ctx)
        local progress = sf.overlay_progress(ctx)
        return sf.scene {
            sf.colour(sf.group("strip"), sf.white()),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local edge = count > 1 and 2 * EDGE_PIXELS / (count - 1) or 1
                local reached = (sf.from_centre(index, count) - 1 + progress * (1 + edge)) / edge
                return sf.dimmer(pixel, math.max(0, math.min(1, reached)))
            end)
        }
    end
}

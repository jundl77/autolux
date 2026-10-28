-- The strip fills from both ends towards the centre over the pre-roll, closing on the drop.
-- strobe: no
-- intensity: htp
local PEAK = 0.9
local EDGE_PIXELS = 1

return {
    render = function(ctx)
        local progress = math.min(1, ctx.autoloop_elapsed_beats / (ctx.overlay_length_beats or 1))
        return sf.scene {
            sf.colour(sf.group("strip"), sf.white()),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local from_centre = count > 1 and math.abs(2 * (index - 1) / (count - 1) - 1) or 0
                local edge = count > 1 and 2 * EDGE_PIXELS / (count - 1) or 1
                local reached = (from_centre - 1 + progress * (1 + edge)) / edge
                return sf.dimmer(pixel, PEAK * math.max(0, math.min(1, reached)))
            end)
        }
    end
}

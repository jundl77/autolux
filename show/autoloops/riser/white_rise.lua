-- The Balanced riser: the beams rise in white to full where the dip starts, still; the washes keep the build's dark floor.
-- strobe: no
-- intensity: htp
-- movement: still
-- texture: beam
local MOVER_PEAK = 1

return {
    render = function(ctx)
        local progress = sf.overlay_progress(ctx)
        local level = progress * progress
        local movers = sf.group("movers")
        return sf.scene {
            sf.colour(movers, sf.white()),
            sf.dimmer(movers, MOVER_PEAK * level)
        }
    end
}

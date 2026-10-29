-- The beams punch white through the open gobo where they already point, decaying over the hit.
-- strobe: no
-- intensity: htp
-- movement: still
-- texture: beam
local OPEN_SLOT = 0

-- No position: an overlay owns pan and tilt from the moment it fires, so aiming here would
-- swing the heads in light on the one.
return {
    render = function(ctx)
        local progress = sf.overlay_progress(ctx)
        local movers = sf.group("movers")
        return sf.scene {
            sf.colour(movers, sf.white()),
            sf.gobo(movers, OPEN_SLOT),
            sf.dimmer(movers, (1 - progress) ^ 2)
        }
    end
}

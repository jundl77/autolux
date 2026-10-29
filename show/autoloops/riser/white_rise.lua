-- The Balanced riser: washes and beams rise in white to full where the dip starts, still.
-- strobe: no
-- intensity: htp
-- movement: still
-- texture: wash
local WASH_PEAK, MOVER_PEAK = 1, 1

return {
    render = function(ctx)
        local progress = sf.overlay_progress(ctx)
        local level = progress * progress
        local movers = sf.group("movers")
        return sf.scene {
            sf.colour(sf.group("wash"), sf.white()),
            sf.dimmer(sf.group("wash"), WASH_PEAK * level),
            sf.colour(movers, sf.white()),
            sf.dimmer(movers, MOVER_PEAK * level)
        }
    end
}

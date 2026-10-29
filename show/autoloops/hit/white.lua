-- The white hit on the drop's one: washes, strip and beams full white, the beams through the open gobo.
-- strobe: no
-- intensity: htp
-- movement: still
-- texture: mixed
local OPEN_SLOT = 0
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 1, 0.9, 1

-- No position: an overlay owns pan and tilt from the moment it fires, so aiming here would
-- swing the heads in light on the one; they punch where the drop already points them.
return {
    render = function(ctx)
        local decay = (1 - sf.overlay_progress(ctx)) ^ 2
        local movers = sf.group("movers")
        return sf.scene {
            sf.colour(sf.group("wash"), sf.white()),
            sf.dimmer(sf.group("wash"), WASH_PEAK * decay),
            sf.colour(sf.group("strip"), sf.white()),
            sf.dimmer(sf.group("strip"), STRIP_PEAK * decay),
            sf.colour(movers, sf.white()),
            sf.gobo(movers, OPEN_SLOT),
            sf.dimmer(movers, MOVER_PEAK * decay)
        }
    end
}

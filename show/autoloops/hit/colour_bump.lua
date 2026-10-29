-- The Calm entry hit: a bump in the palette's punch colour, no white and no strobe.
-- strobe: no
-- intensity: htp
-- movement: still
-- texture: wash
local palette = sf.palette()
local WASH_PEAK, STRIP_PEAK = 0.9, 0.8

return {
    render = function(ctx)
        local progress = sf.overlay_progress(ctx)
        local decay = (1 - progress) ^ 2
        return sf.scene {
            sf.colour(sf.group("wash"), palette.accent),
            sf.dimmer(sf.group("wash"), WASH_PEAK * decay),
            sf.colour(sf.group("strip"), palette.accent),
            sf.dimmer(sf.group("strip"), STRIP_PEAK * decay)
        }
    end
}

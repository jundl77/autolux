-- White blinder on the drop's one: washes and strip at full from the first frame, decaying.
-- strobe: no
-- intensity: htp
-- movement: still
-- texture: wash
local WASH_PEAK, STRIP_PEAK = 1, 0.9

return {
    render = function(ctx)
        local progress = sf.overlay_progress(ctx)
        local decay = (1 - progress) ^ 2
        return sf.scene {
            sf.colour(sf.group("wash"), sf.white()),
            sf.dimmer(sf.group("wash"), WASH_PEAK * decay),
            sf.colour(sf.group("strip"), sf.white()),
            sf.dimmer(sf.group("strip"), STRIP_PEAK * decay)
        }
    end
}

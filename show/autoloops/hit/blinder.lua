-- White blinder on the drop's one: washes and strip at full, decaying over the hit.
-- strobe: no
-- intensity: htp
local WASH_PEAK, STRIP_PEAK = 1, 0.9

return {
    render = function(ctx)
        local progress = math.min(1, ctx.autoloop_elapsed_beats / (ctx.overlay_length_beats or 1))
        local decay = (1 - progress) ^ 2
        return sf.scene {
            sf.colour(sf.group("wash"), sf.white()),
            sf.dimmer(sf.group("wash"), WASH_PEAK * decay),
            sf.colour(sf.group("strip"), sf.white()),
            sf.dimmer(sf.group("strip"), STRIP_PEAK * decay)
        }
    end
}

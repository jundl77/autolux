-- The Calm entry hit: a bump in the palette's punch colour, no white and no strobe.
-- strobe: no
-- intensity: htp
local palette = sf.palette()
local WASH_PEAK, STRIP_PEAK = 0.8, 0.7

return {
    render = function(ctx)
        local progress = math.min(1, ctx.autoloop_elapsed_beats / (ctx.overlay_length_beats or 1))
        local decay = (1 - progress) ^ 2
        return sf.scene {
            sf.colour(sf.group("wash"), palette.accent),
            sf.dimmer(sf.group("wash"), WASH_PEAK * decay),
            sf.colour(sf.group("strip"), palette.accent),
            sf.dimmer(sf.group("strip"), STRIP_PEAK * decay)
        }
    end
}

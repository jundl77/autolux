-- The Balanced riser: the beams lift off their focus in full white while the washes swell.
-- strobe: no
-- intensity: htp
-- movement: sweep
-- texture: beam
local LIFT_DEGREES, WASH_CAP = 40, 0.6

return {
    render = function(ctx)
        local _, left = sf.overlay_progress(ctx)
        local length, bar = ctx.overlay_length_beats or 1, ctx.beats_per_bar
        local rising = math.min(1, ctx.autoloop_elapsed_beats / math.max(1, length - bar))
        local level = 1 - (1 - rising) ^ 2
        local landing = math.max(0, math.min(1, left / bar))
        local movers = sf.group("movers")
        return sf.scene {
            sf.dimmer(sf.group("wash"), WASH_CAP * level),
            sf.colour(movers, sf.white()),
            sf.dimmer(movers, level),
            sf.phaser(movers, ctx, {
                shape = "fan", size = 0, centre_tilt = LIFT_DEGREES * rising * rising * landing
            })
        }
    end
}

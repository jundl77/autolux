-- The Balanced riser: beams lift off their focus into the air while washes wash out to white.
-- strobe: no
-- intensity: htp
local LIFT_DEGREES = 40
local WASH_PEAK, MOVER_PEAK = 0.6, 0.9

return {
    render = function(ctx)
        local progress = math.min(1, ctx.autoloop_elapsed_beats / (ctx.overlay_length_beats or 1))
        local movers = sf.group("movers")
        return sf.scene {
            sf.colour(sf.group("wash"), sf.white()),
            sf.dimmer(sf.group("wash"), WASH_PEAK * progress * progress),
            sf.colour(movers, sf.white()),
            sf.dimmer(movers, MOVER_PEAK * progress),
            sf.phaser(movers, ctx, { shape = "fan", size = 0, centre_tilt = LIFT_DEGREES * progress * progress })
        }
    end
}

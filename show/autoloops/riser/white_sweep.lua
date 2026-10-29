-- The Balanced riser: beams lift off their focus while washes and beams rise to full white.
-- strobe: no
-- intensity: htp
-- movement: sweep
-- texture: beam
local LIFT_DEGREES = 40

-- The level eases out so the white clears a loud build well before the dip.
return {
    render = function(ctx)
        local progress = sf.overlay_progress(ctx)
        local level = 1 - (1 - progress) ^ 2
        local movers = sf.group("movers")
        return sf.scene {
            sf.colour(sf.group("wash"), sf.white()),
            sf.dimmer(sf.group("wash"), level),
            sf.colour(movers, sf.white()),
            sf.dimmer(movers, level),
            sf.phaser(movers, ctx, {
                shape = "fan", size = 0, centre_tilt = LIFT_DEGREES * progress * progress
            })
        }
    end
}

-- The beams trace mirrored figure-eights through a gobo in the second colour, pulsing.
-- strobe: no
-- intensity: ltp
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local BREAKUP_SLOT = 2
local FIGURE_DEGREES, FIGURE_TILT_DEGREES, FIGURE_MEASURE = 20, 14, 8
local PULSE_WIDTH_BEATS, FLOOR, PEAK = 0.45, 0.45, 0.85

return {
    render = function(ctx)
        local movers = sf.group("movers")
        return sf.scene {
            sf.colour(movers, palette.secondary),
            sf.gobo(movers, BREAKUP_SLOT),
            sf.dimmer(movers, sf.lerp(FLOOR, PEAK, sf.on_beat(ctx, PULSE_WIDTH_BEATS))),
            sf.phaser(movers, ctx, {
                shape = "figure8", size = FIGURE_DEGREES, tilt_size = FIGURE_TILT_DEGREES,
                measure = FIGURE_MEASURE
            })
        }
    end
}

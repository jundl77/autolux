-- The beams trace mirrored figure-eights through a gobo, pulsing, opposite the washes' colour.
-- strobe: no
-- intensity: ltp
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local BREAKUP_SLOT = 2
local FIGURE_DEGREES, FIGURE_TILT_DEGREES, FIGURE_MEASURE = 20, 14, 8
local PULSE_WIDTH_BEATS, FLOOR, PEAK = 0.45, 0.45, 0.85
-- The same two-bar line on which hammer and crossfire swap their washes between A and B.
local COLOUR_HALF_BEATS = 8

return {
    render = function(ctx)
        local movers = sf.group("movers")
        local second_half = math.floor(sf.section_beats(ctx) / COLOUR_HALF_BEATS) % 2 == 1
        return sf.scene {
            sf.colour(movers, second_half and palette.primary or palette.secondary),
            sf.gobo(movers, BREAKUP_SLOT),
            sf.dimmer(movers, sf.lerp(FLOOR, PEAK, sf.on_beat(ctx, PULSE_WIDTH_BEATS))),
            sf.phaser(movers, ctx, {
                shape = "figure8", size = FIGURE_DEGREES, tilt_size = FIGURE_TILT_DEGREES,
                measure = FIGURE_MEASURE
            })
        }
    end
}

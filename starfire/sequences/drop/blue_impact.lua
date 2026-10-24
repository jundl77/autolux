local PULSE_PERIOD_BEATS = 2
local MOVEMENT_PERIOD_BEATS = 8
local LOW_LEVEL, HIGH_LEVEL = 0.55, 0.78
local BLUE_HUE, VIOLET_HUE = 240, 275
local SATURATION = 0.85
local PAN_MIN, PAN_MAX = 0.28, 0.72
local TILT_MIN, TILT_MAX = 0.28, 0.56

return {
    render = function(ctx)
        return sf.scene {
            sf.dimmer(sf.all(), sf.osc.sine(ctx.sequence_speed_scaled_elapsed_beats, PULSE_PERIOD_BEATS, LOW_LEVEL, HIGH_LEVEL)),
            sf.each(sf.group("wash"), function(target, index, count)
                local spread = (index - 1) / count
                return sf.colour(target, sf.hsv(
                    sf.osc.sine(ctx.sequence_speed_scaled_elapsed_beats, MOVEMENT_PERIOD_BEATS, BLUE_HUE, VIOLET_HUE, spread),
                    SATURATION, 1))
            end),
            sf.colour(sf.group("movers"), sf.hsv(VIOLET_HUE, SATURATION, 1)),
            sf.each(sf.group("movers"), function(target, index, count)
                local spread = (index - 1) / count
                return sf.pan_tilt(target,
                    sf.osc.sine(ctx.sequence_speed_scaled_elapsed_beats, MOVEMENT_PERIOD_BEATS, PAN_MIN, PAN_MAX, spread),
                    sf.osc.sine(ctx.sequence_speed_scaled_elapsed_beats, MOVEMENT_PERIOD_BEATS, TILT_MIN, TILT_MAX, spread))
            end)
        }
    end
}

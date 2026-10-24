local WASH_LEVEL = 0.18
local MOVER_LEVEL = 0.22
local COLOUR_PERIOD_BEATS = 64
local MOVEMENT_PERIOD_BEATS = 64
local BLUE_HUE = 240
local VIOLET_HUE = 275
local SATURATION = 0.85
local PAN_MIN, PAN_MAX = 0.42, 0.58
local TILT_MIN, TILT_MAX = 0.38, 0.48

return {
    render = function(ctx)
        local hue = sf.osc.sine(ctx.sequence_speed_scaled_elapsed_beats, COLOUR_PERIOD_BEATS, BLUE_HUE, VIOLET_HUE)
        return sf.scene {
            sf.colour(sf.all(), sf.hsv(hue, SATURATION, 1)),
            sf.dimmer(sf.group("wash"), WASH_LEVEL),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL),
            sf.each(sf.group("movers"), function(target, index, count)
                local spread = (index - 1) / count
                return sf.pan_tilt(target,
                    sf.osc.sine(ctx.sequence_speed_scaled_elapsed_beats, MOVEMENT_PERIOD_BEATS, PAN_MIN, PAN_MAX, spread),
                    sf.osc.sine(ctx.sequence_speed_scaled_elapsed_beats, MOVEMENT_PERIOD_BEATS, TILT_MIN, TILT_MAX, spread))
            end)
        }
    end
}

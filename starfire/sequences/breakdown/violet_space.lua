local DMX_BYTE_MAX = 255
local MOVEMENT_MAX = 65535
local LOOP_BEATS = 64
local HALF_LOOP_BEATS = LOOP_BEATS / 2
local COLOUR_STEP_BEATS = 4
local BASE_LEVEL = 37 / DMX_BYTE_MAX
local REST_POSITION = 32767 / MOVEMENT_MAX
local MIN_AUDIO_BRIGHTNESS, AUDIO_BRIGHTNESS_RANGE = 0.65, 0.35
local BLUE = sf.rgb(32 / DMX_BYTE_MAX, 57 / DMX_BYTE_MAX, 230 / DMX_BYTE_MAX)
local VIOLET = sf.rgb(119 / DMX_BYTE_MAX, 36 / DMX_BYTE_MAX, 230 / DMX_BYTE_MAX)
local MOVER_BLUE = sf.rgb(0, 0, 1)

local function chase_colour(beat, fixture_index)
    local step = math.floor(beat / COLOUR_STEP_BEATS)
    if beat >= HALF_LOOP_BEATS then return step % 2 == 1 and VIOLET or BLUE end
    return step == fixture_index - 1 and VIOLET or BLUE
end

return {
    render = function(ctx)
        local beat = ctx.sequence_speed_scaled_elapsed_beats % LOOP_BEATS
        local brightness = MIN_AUDIO_BRIGHTNESS + AUDIO_BRIGHTNESS_RANGE * ctx.normalized_rms_level
        local wash_count = #sf.members(sf.group("wash"))
        return sf.scene {
            sf.dimmer(sf.all(), BASE_LEVEL * brightness),
            sf.colour(sf.group("movers"), MOVER_BLUE),
            sf.pan_tilt(sf.group("movers"), REST_POSITION, REST_POSITION),
            sf.each(sf.group("wash"), function(target, index)
                return sf.colour(target, chase_colour(beat, index))
            end),
            sf.each(sf.group("strip"), function(target, index)
                return sf.colour(target, chase_colour(beat, wash_count + index))
            end)
        }
    end
}

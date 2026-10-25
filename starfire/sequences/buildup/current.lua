local DMX_BYTE_MAX = 255
local LOOP_BEATS = 64
local HALF_LOOP_BEATS = LOOP_BEATS / 2
local CHASE_STEP_BEATS, ALTERNATE_STEP_BEATS = 2, 4
local PULSE_LEVEL = 144 / DMX_BYTE_MAX
local PULSE_HOLD_BEATS = 0.5
local MIN_AUDIO_BRIGHTNESS, AUDIO_BRIGHTNESS_RANGE = 0.65, 0.35
local BLUE = sf.rgb(32 / DMX_BYTE_MAX, 57 / DMX_BYTE_MAX, 230 / DMX_BYTE_MAX)
local VIOLET = sf.rgb(119 / DMX_BYTE_MAX, 36 / DMX_BYTE_MAX, 230 / DMX_BYTE_MAX)
local MOVER_BLUE = sf.rgb(0, 0, 1)
local CENTER_POSITION = 0.5
local START_PAN_RADIUS, END_PAN_RADIUS = 0.01, 0.10
local TILT_RADIUS_MULTIPLIER = 2
local START_PERIOD_BEATS, END_PERIOD_BEATS = 50 / 3, 10 / 3
local FULL_TURN = 2 * math.pi

local function chase_colour(beat, fixture_index, fixture_count)
    if beat >= HALF_LOOP_BEATS then
        return math.floor(beat / ALTERNATE_STEP_BEATS) % 2 == 1 and VIOLET or BLUE
    end
    local step = math.floor(beat / CHASE_STEP_BEATS) % fixture_count
    return step == fixture_index - 1 and VIOLET or BLUE
end

return {
    render = function(ctx)
        local beat = ctx.sequence_speed_scaled_elapsed_beats % LOOP_BEATS
        local brightness = MIN_AUDIO_BRIGHTNESS + AUDIO_BRIGHTNESS_RANGE * ctx.normalized_rms_level
        local pulse_phase = ctx.beat_position % 1
        local level = PULSE_LEVEL
        if pulse_phase > PULSE_HOLD_BEATS then
            level = level * (1 - pulse_phase) / (1 - PULSE_HOLD_BEATS)
        end
        local pan, tilt = CENTER_POSITION, CENTER_POSITION
        if beat < HALF_LOOP_BEATS then
            local amount = beat / HALF_LOOP_BEATS
            local radius = sf.lerp(START_PAN_RADIUS, END_PAN_RADIUS, amount)
            local period = sf.lerp(START_PERIOD_BEATS, END_PERIOD_BEATS, amount)
            local angle = FULL_TURN * beat / period
            pan = CENTER_POSITION + radius * math.cos(angle)
            tilt = CENTER_POSITION - TILT_RADIUS_MULTIPLIER * radius * math.sin(angle)
        end
        local wash_count = #sf.members(sf.group("wash"))
        local fixture_count = wash_count + #sf.members(sf.group("strip"))
        return sf.scene {
            sf.dimmer(sf.all(), level * brightness),
            sf.colour(sf.group("movers"), MOVER_BLUE),
            sf.pan_tilt(sf.group("movers"), pan, tilt),
            sf.each(sf.group("wash"), function(target, index)
                return sf.colour(target, chase_colour(beat, index, fixture_count))
            end),
            sf.each(sf.group("strip"), function(target, index)
                return sf.colour(target, chase_colour(beat, wash_count + index, fixture_count))
            end)
        }
    end
}

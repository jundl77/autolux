local DMX_BYTE_MAX = 255
local MOVEMENT_MAX = 65535
local LOOP_BEATS = 64
local BEATS_PER_BAR = 4
local PHRASE_BEATS = 16
local LOW_LEVEL, HIGH_LEVEL = 124 / DMX_BYTE_MAX, 198 / DMX_BYTE_MAX
local PULSE_GRID_STEPS_PER_BEAT = 600
local PULSE_KEYFRAME_STEPS = {
    1214, 2400, 3595, 4800, 6019, 7199, 8400, 9600,
    10824, 12000, 13248, 14399, 15628, 16800, 18052, 19200,
    20433, 21599, 22857, 24000, 25238, 26400, 27662, 28799,
    30086, 31200, 32466, 33600, 34890, 36000, 37271, 38400
}
local ENTRY_HOLD_BEATS = PULSE_KEYFRAME_STEPS[1] / PULSE_GRID_STEPS_PER_BEAT
local ACCENT_LEVEL, ACCENT_SUSTAIN = 245 / DMX_BYTE_MAX, 110 / DMX_BYTE_MAX
local ACCENT_START, ACCENT_END = PHRASE_BEATS, 2 * PHRASE_BEATS
local MIN_AUDIO_BRIGHTNESS, AUDIO_BRIGHTNESS_RANGE = 0.65, 0.35
local BLUE = sf.rgb(32 / DMX_BYTE_MAX, 57 / DMX_BYTE_MAX, 230 / DMX_BYTE_MAX)
local VIOLET = sf.rgb(119 / DMX_BYTE_MAX, 36 / DMX_BYTE_MAX, 230 / DMX_BYTE_MAX)
local DEEP_BLUE = sf.rgb(21 / DMX_BYTE_MAX, 0, 1)
local MOVER_BLUE = sf.rgb(0, 0, 1)
local MOVEMENT_START_BEAT = 24
local MOVEMENT_PERIOD_BEATS = 32 / 3
local PAN_RADIUS, TILT_RADIUS = 0.055, 0.11
local PAN_FREQUENCY_MULTIPLIER = 3
local FULL_TURN = 2 * math.pi
local MOVER_POSITIONS = {
    {
        start_pan = 15393 / MOVEMENT_MAX, start_tilt = 51373 / MOVEMENT_MAX,
        center_pan = 21032 / MOVEMENT_MAX, center_tilt = 56044 / MOVEMENT_MAX
    },
    {
        start_pan = 15241 / MOVEMENT_MAX, start_tilt = 51223 / MOVEMENT_MAX,
        center_pan = 24385 / MOVEMENT_MAX, center_tilt = 51976 / MOVEMENT_MAX
    }
}

local function blend(first, last, amount)
    return sf.rgb(sf.lerp(first.red, last.red, amount),
        sf.lerp(first.green, last.green, amount), sf.lerp(first.blue, last.blue, amount))
end

local function colour(beat)
    if beat < PHRASE_BEATS then return blend(DEEP_BLUE, VIOLET, beat / (2 * PHRASE_BEATS)) end
    if beat < 2 * PHRASE_BEATS then return blend(VIOLET, DEEP_BLUE, (beat - PHRASE_BEATS) / PHRASE_BEATS) end
    return BLUE
end

local function right_accent(beat)
    local elapsed = beat - ACCENT_START
    if elapsed < 1 then return ACCENT_SUSTAIN * elapsed end
    local phase = elapsed % 2
    if phase < 1 then return ACCENT_SUSTAIN end
    return sf.lerp(ACCENT_LEVEL, ACCENT_SUSTAIN, phase - 1)
end

local function pulse_level(beat)
    local step = (beat % LOOP_BEATS) * PULSE_GRID_STEPS_PER_BEAT
    local previous_step, previous_level = PULSE_KEYFRAME_STEPS[1], LOW_LEVEL
    for index = 2, #PULSE_KEYFRAME_STEPS do
        local next_step = PULSE_KEYFRAME_STEPS[index]
        local next_level = index % 2 == 0 and HIGH_LEVEL or LOW_LEVEL
        if step < next_step then
            local amount = math.max(0, (step - previous_step) / (next_step - previous_step))
            return sf.lerp(previous_level, next_level, amount)
        end
        previous_step, previous_level = next_step, next_level
    end
    return HIGH_LEVEL
end

return {
    render = function(ctx)
        local beat = ctx.sequence_speed_scaled_elapsed_beats % LOOP_BEATS
        local brightness = MIN_AUDIO_BRIGHTNESS + AUDIO_BRIGHTNESS_RANGE * (ctx.normalized_rms_level or 1)
        local level = LOW_LEVEL
        if ctx.sequence_elapsed_beats >= ENTRY_HOLD_BEATS then
            local sequence_start_beat = ctx.beat_position - ctx.sequence_elapsed_beats
            local pulse_origin_beat = math.floor(sequence_start_beat / BEATS_PER_BAR) * BEATS_PER_BAR
            level = pulse_level(ctx.beat_position - pulse_origin_beat)
        end
        local accent_active = beat >= ACCENT_START and beat < ACCENT_END
        local wash_level = accent_active and ACCENT_LEVEL or level
        return sf.scene {
            sf.colour(sf.all(), colour(beat)),
            sf.colour(sf.group("movers"), MOVER_BLUE),
            sf.dimmer(sf.all(), wash_level * brightness),
            sf.each(sf.group("movers"), function(target, index)
                local side = (index - 1) % #MOVER_POSITIONS + 1
                local position = MOVER_POSITIONS[side]
                local pan, tilt = position.start_pan, position.start_tilt
                if beat >= 3 * PHRASE_BEATS then
                    local angle = FULL_TURN * (beat - 3 * PHRASE_BEATS) / MOVEMENT_PERIOD_BEATS
                    pan = position.center_pan + PAN_RADIUS * math.cos(PAN_FREQUENCY_MULTIPLIER * angle)
                    tilt = position.center_tilt - TILT_RADIUS * math.sin(angle)
                elseif beat >= 2 * PHRASE_BEATS then
                    pan, tilt = position.center_pan, position.center_tilt
                elseif beat >= MOVEMENT_START_BEAT then
                    local amount = (beat - MOVEMENT_START_BEAT) / (2 * PHRASE_BEATS - MOVEMENT_START_BEAT)
                    pan = sf.lerp(pan, position.center_pan, amount)
                    tilt = sf.lerp(tilt, position.center_tilt, amount)
                end
                local mover_level = side == 2 and accent_active and right_accent(beat) or level
                return { sf.pan_tilt(target, pan, tilt), sf.dimmer(target, mover_level * brightness) }
            end)
        }
    end
}

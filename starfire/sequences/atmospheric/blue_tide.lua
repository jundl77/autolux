local DMX_BYTE_MAX = 255
local MOVEMENT_MAX = 65535
local LOOP_BEATS = 64
local PHRASE_BEATS = 16
local BASE_LEVEL = 63 / DMX_BYTE_MAX
local ACCENT_HIGH, ACCENT_LOW = 245 / DMX_BYTE_MAX, 73 / DMX_BYTE_MAX
local ACCENT_PERIOD_BEATS = 8
local MIN_AUDIO_BRIGHTNESS, AUDIO_BRIGHTNESS_RANGE = 0.65, 0.35
local BLUE = sf.rgb(32 / DMX_BYTE_MAX, 57 / DMX_BYTE_MAX, 230 / DMX_BYTE_MAX)
local VIOLET = sf.rgb(119 / DMX_BYTE_MAX, 36 / DMX_BYTE_MAX, 230 / DMX_BYTE_MAX)
local DEEP_BLUE = sf.rgb(21 / DMX_BYTE_MAX, 0, 1)
local MOVER_BLUE = sf.rgb(0, 0, 1)
local MOVEMENT_PERIOD_BEATS = 40 / 3
local PAN_RADIUS, TILT_RADIUS = 0.055, 0.11
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
    if beat < 2 * PHRASE_BEATS then
        local fade_beat = beat % ACCENT_PERIOD_BEATS
        if fade_beat < ACCENT_PERIOD_BEATS / 2 then
            return blend(VIOLET, BLUE, fade_beat / (ACCENT_PERIOD_BEATS / 2))
        end
        return blend(DEEP_BLUE, VIOLET, beat / (2 * PHRASE_BEATS))
    elseif beat < 3 * PHRASE_BEATS then
        return blend(BLUE, DEEP_BLUE, (beat - 2 * PHRASE_BEATS) / PHRASE_BEATS)
    end
    return blend(DEEP_BLUE, BLUE, (beat - 3 * PHRASE_BEATS) / PHRASE_BEATS)
end

return {
    render = function(ctx)
        local beat = ctx.sequence_speed_scaled_elapsed_beats % LOOP_BEATS
        local brightness = MIN_AUDIO_BRIGHTNESS + AUDIO_BRIGHTNESS_RANGE * (ctx.normalized_rms_level or 1)
        return sf.scene {
            sf.colour(sf.all(), colour(beat)),
            sf.colour(sf.group("movers"), MOVER_BLUE),
            sf.dimmer(sf.all(), BASE_LEVEL * brightness),
            sf.each(sf.group("movers"), function(target, index)
                local side = (index - 1) % #MOVER_POSITIONS + 1
                local position = MOVER_POSITIONS[side]
                local pan, tilt = position.start_pan, position.start_tilt
                if beat >= 2 * PHRASE_BEATS then
                    local angle = FULL_TURN * (beat - 2 * PHRASE_BEATS) / MOVEMENT_PERIOD_BEATS
                    pan = position.center_pan + PAN_RADIUS * math.cos(angle)
                    tilt = position.center_tilt - TILT_RADIUS * math.sin(angle)
                elseif beat >= PHRASE_BEATS then
                    local amount = (beat - PHRASE_BEATS) / PHRASE_BEATS
                    pan = sf.lerp(pan, position.center_pan, amount)
                    tilt = sf.lerp(tilt, position.center_tilt, amount)
                end
                local level = BASE_LEVEL
                if side == 2 and beat >= 2 * PHRASE_BEATS and beat < 3 * PHRASE_BEATS then
                    level = sf.lerp(ACCENT_HIGH, ACCENT_LOW, (beat % ACCENT_PERIOD_BEATS) / ACCENT_PERIOD_BEATS)
                end
                return { sf.pan_tilt(target, pan, tilt), sf.dimmer(target, level * brightness) }
            end)
        }
    end
}

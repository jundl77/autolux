-- 8-beat loop
-- energy: 3
-- movement: snap
-- texture: pixel
local palette = sf.palette()
local LOOP_BEATS = 8
local BAR_BEATS = 4
local STEPS_PER_BEAT = 4
local KICK_WIDTH_BEATS = 0.4
local FLOOR_QUIET, FLOOR_LOUD = 0.05, 0.3
local WASH_PEAK, HEAD_LEVEL, BASE_LEVEL, MOVER_LEVEL = 0.9, 0.9, 0.12, 0.85
local CENTRE_PAN = 0.5
local POSITIONS = { { pan_offset = 0.1, tilt = 0.6 }, { pan_offset = 0.2, tilt = 0.55 } }

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = sf.energy(ctx)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local kick = sf.on_beat(beat, KICK_WIDTH_BEATS)
        local step = math.floor(beat * STEPS_PER_BEAT)
        local position = POSITIONS[math.floor(beat / BAR_BEATS) % 2 + 1]
        local reach = sf.lerp(0.5, 1, energy)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, WASH_PEAK, kick) * gain),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local behind = (step - (index - 1)) % count
                if behind > 1 then
                    return { sf.colour(pixel, palette.primary), sf.dimmer(pixel, BASE_LEVEL * gain) }
                end
                local level = behind == 0 and HEAD_LEVEL or HEAD_LEVEL / 2
                return { sf.colour(pixel, palette.accent), sf.dimmer(pixel, level * gain) }
            end),
            sf.colour(sf.group("movers"), palette.primary),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * gain),
            sf.each(sf.group("movers"), function(mover, index)
                local pan = CENTRE_PAN + sf.side(index) * position.pan_offset * reach
                return sf.pan_tilt(mover, pan, position.tilt)
            end)
        }
    end
}

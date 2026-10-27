-- 16-beat loop
-- energy: 2
-- movement: snap
-- texture: pixel
local palette = sf.palette()
local LOOP_BEATS = 16
local HOLD_BEATS = 8
local STEPS_PER_BEAT = 2
local KICK_WIDTH_BEATS = 0.4
local FLOOR_QUIET, FLOOR_LOUD = 0.05, 0.25
local WASH_PEAK, HEAD_LEVEL, MOVER_LEVEL = 0.9, 0.9, 0.85
local TAIL = { [0] = 1, [1] = 0.45, [2] = 0.15 }
local CENTRE_PAN = 0.5
local POSITIONS = { { pan_offset = 0.18, tilt = 0.55 }, { pan_offset = 0.06, tilt = 0.68 } }

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local kick = sf.on_beat(beat, KICK_WIDTH_BEATS)
        local step = math.floor(beat * STEPS_PER_BEAT)
        local position = POSITIONS[math.floor(beat / HOLD_BEATS) % 2 + 1]
        local reach = sf.lerp(0.5, 1, energy)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.accent),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, WASH_PEAK, kick) * gain),
            sf.colour(sf.group("strip"), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local behind = (step - (index - 1)) % count
                return sf.dimmer(pixel, HEAD_LEVEL * (TAIL[behind] or 0) * gain)
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

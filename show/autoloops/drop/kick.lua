-- 8-beat loop
-- energy: 3
-- movement: snap
-- texture: wash
local palette = sf.palette()
local LOOP_BEATS = 8
local HALF_BAR_BEATS = 2
local KICK_WIDTH_BEATS = 0.4
local FLOOR_QUIET, FLOOR_LOUD = 0.05, 0.3
local STRIP_LEVEL, MOVER_LEVEL = 0.8, 0.9
local CENTRE_PAN = 0.5
local POSITIONS = { { pan_offset = 0.12, tilt = 0.55 }, { pan_offset = -0.06, tilt = 0.7 } }

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local half_bar = math.floor(beat / HALF_BAR_BEATS)
        local position = POSITIONS[half_bar % 2 + 1]
        local reach = sf.lerp(0.5, 1, energy)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, 1, sf.on_beat(beat, KICK_WIDTH_BEATS)) * gain),
            sf.colour(sf.group("strip"), half_bar % 2 == 0 and palette.accent or palette.primary),
            sf.dimmer(sf.group("strip"), STRIP_LEVEL * gain),
            sf.colour(sf.group("movers"), palette.primary),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * gain),
            sf.each(sf.group("movers"), function(mover, index)
                local side = index % 2 == 1 and -1 or 1
                return sf.pan_tilt(mover, CENTRE_PAN + side * position.pan_offset * reach, position.tilt)
            end)
        }
    end
}

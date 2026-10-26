-- 8-beat loop
-- energy: 3
-- movement: snap
-- texture: wash
local palette = sf.palette()
local LOOP_BEATS = 8
local KICK_WIDTH_BEATS, GHOST_WIDTH_BEATS = 0.4, 0.3
local GHOST_AT_IN_BAR, GHOST_OFFSET_BEATS = 3.5, 0.5
local GHOST_LEVEL = 0.5
local FLOOR_QUIET, FLOOR_LOUD = 0.04, 0.2
local WASH_PEAK, STRIP_PEAK, MOVER_LEVEL = 0.9, 0.85, 0.85
local CENTRE_PAN = 0.5
local POSITIONS = { { pan_offset = 0.15, tilt = 0.58 }, { pan_offset = 0.07, tilt = 0.7 } }

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local hit = sf.on_beat(beat, KICK_WIDTH_BEATS)
        local in_bar = sf.beat_in_bar(beat)
        if in_bar >= GHOST_AT_IN_BAR then
            hit = math.max(hit, GHOST_LEVEL * sf.on_beat(in_bar - GHOST_OFFSET_BEATS, GHOST_WIDTH_BEATS))
        end
        local position = POSITIONS[sf.bar(beat) % 2 + 1]
        local reach = sf.lerp(0.5, 1, energy)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, WASH_PEAK, hit) * gain),
            sf.colour(sf.group("strip"), palette.primary),
            sf.dimmer(sf.group("strip"), STRIP_PEAK * hit * gain),
            sf.colour(sf.group("movers"), palette.primary),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * gain),
            sf.each(sf.group("movers"), function(mover, index)
                local pan = CENTRE_PAN + sf.side(index) * position.pan_offset * reach
                return sf.pan_tilt(mover, pan, position.tilt)
            end)
        }
    end
}

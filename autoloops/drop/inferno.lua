-- 8-beat loop, no strobe (Autolux adds a short one on the drop itself).
-- energy: 2
-- family: accent
-- movement: snap
-- wash: accent, kick hit on every beat over a glowing floor (0.25 before loudness gain),
-- so the room never drops out of red.
-- strip: primary pulse on every "and", white sparks between the red kicks.
-- movers: accent, snap between two mirrored positions on beat 1 and beat 3.
-- Palette slots: accent, primary.
local palette = sf.palette("techno")
local LOOP_BEATS = 8
local HALF_BAR_BEATS = 2
local KICK_WIDTH_BEATS, SPARK_WIDTH_BEATS = 0.4, 0.3
local OFFBEAT_SHIFT_BEATS = 0.5
local WASH_FLOOR, WASH_PEAK, STRIP_PEAK, MOVER_LEVEL = 0.25, 0.9, 0.8, 0.9
local CENTRE_PAN = 0.5
-- Pan is the offset from centre; movers mirror it left/right.
local POSITIONS = { { pan = 0.16, tilt = 0.52 }, { pan = 0.04, tilt = 0.72 } }

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local kick = sf.on_beat(beat, KICK_WIDTH_BEATS)
        local spark = sf.on_beat(beat + OFFBEAT_SHIFT_BEATS, SPARK_WIDTH_BEATS)
        local position = POSITIONS[math.floor(beat / HALF_BAR_BEATS) % 2 + 1]
        local reach = sf.lerp(0.5, 1, energy)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.accent),
            sf.dimmer(sf.group("wash"), sf.lerp(WASH_FLOOR, WASH_PEAK, kick) * gain),
            sf.colour(sf.group("strip"), palette.primary),
            sf.dimmer(sf.group("strip"), STRIP_PEAK * spark * gain),
            sf.colour(sf.group("movers"), palette.accent),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * gain),
            sf.each(sf.group("movers"), function(mover, index)
                local pan = CENTRE_PAN + sf.side(index) * position.pan * reach
                return sf.pan_tilt(mover, pan, position.tilt)
            end)
        }
    end
}

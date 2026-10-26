-- 8-beat loop, no strobe (Autolux adds a short one on the drop itself).
-- energy: 3
-- family: cold
-- movement: snap
-- wash: cold, kick hit on every beat over an RMS-scaled floor.
-- strip: low accent pulse on every off-beat, answering the kick.
-- movers: primary, snap each beat between two mirrored diagonals (wide-low, crossed-high),
-- so the beams cross and uncross on the kick.
-- Palette slots: cold, accent, primary.
local palette = sf.palette("techno")
local LOOP_BEATS = 8
local KICK_WIDTH_BEATS = 0.4
local OFFBEAT_DUTY, OFFBEAT_PHASE = 0.3, 0.5
local FLOOR_QUIET, FLOOR_LOUD = 0.04, 0.2
local WASH_PEAK, STRIP_LEVEL, MOVER_LEVEL = 0.9, 0.35, 0.85
local CENTRE_PAN = 0.5
-- Pan is the offset from centre; movers mirror it left/right (negative crosses the beams).
local POSITIONS = { { pan = 0.16, tilt = 0.5 }, { pan = -0.08, tilt = 0.72 } }

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local kick = sf.on_beat(beat, KICK_WIDTH_BEATS)
        local offbeat = sf.osc.pulse(beat, 1, 0, STRIP_LEVEL, OFFBEAT_DUTY, OFFBEAT_PHASE)
        local position = POSITIONS[math.floor(beat) % 2 + 1]
        local reach = sf.lerp(0.5, 1, energy)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.cold),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, WASH_PEAK, kick) * gain),
            sf.colour(sf.group("strip"), palette.accent),
            sf.dimmer(sf.group("strip"), offbeat * gain),
            sf.colour(sf.group("movers"), palette.primary),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * gain),
            sf.each(sf.group("movers"), function(mover, index)
                local pan = CENTRE_PAN + sf.side(index) * position.pan * reach
                return sf.pan_tilt(mover, pan, position.tilt)
            end)
        }
    end
}

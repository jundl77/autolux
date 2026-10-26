-- 8-beat loop, no strobe (Autolux adds a short one on the drop itself).
-- energy: 3
-- family: warm
-- movement: still
-- wash: primary, all four hit together on every beat with a short decay over an
-- RMS-scaled floor: one solid wall of light per kick.
-- strip: steady secondary at half level, the warm horizon behind the wall.
-- movers: primary, held static in a wide mirrored V.
-- Palette slots: primary, secondary.
local palette = sf.palette("techno")
local LOOP_BEATS = 8
local KICK_WIDTH_BEATS = 0.3
local FLOOR_QUIET, FLOOR_LOUD = 0.03, 0.15
local WASH_PEAK, STRIP_LEVEL, MOVER_LEVEL = 0.9, 0.5, 0.8
local CENTRE_PAN, WIDE_PAN, TILT = 0.5, 0.22, 0.6

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local kick = sf.on_beat(beat, KICK_WIDTH_BEATS)
        local reach = sf.lerp(0.5, 1, energy)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, WASH_PEAK, kick) * gain),
            sf.colour(sf.group("strip"), palette.secondary),
            sf.dimmer(sf.group("strip"), STRIP_LEVEL * gain),
            sf.colour(sf.group("movers"), palette.primary),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * gain),
            sf.each(sf.group("movers"), function(mover, index)
                return sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * WIDE_PAN * reach, TILT)
            end)
        }
    end
}

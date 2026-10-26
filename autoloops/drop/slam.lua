-- 8-beat loop, no strobe (Autolux adds a short one on the drop itself).
-- wash: primary hit on the one with a full-beat decay, accent hit on beat 3 with a
-- half-beat decay, over an RMS-scaled floor; beats 2 and 4 rest.
-- strip: primary, odd and even pixels trading every bar.
-- movers: cold, snap between two mirrored positions on beat 1 and beat 3.
-- Palette slots: primary, accent, cold.
local palette = sf.palette("techno")
local LOOP_BEATS = 8
local HALF_BAR_BEATS = 2
local ONE_WIDTH_BEATS, THREE_WIDTH_BEATS = 1.0, 0.5
local FLOOR_QUIET, FLOOR_LOUD = 0.03, 0.15
local WASH_PEAK, STRIP_LEVEL, MOVER_LEVEL = 0.9, 0.7, 0.9
local CENTRE_PAN = 0.5
-- Pan is the offset from centre; movers mirror it left/right.
local POSITIONS = { { pan = 0.15, tilt = 0.55 }, { pan = 0.05, tilt = 0.7 } }

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local in_bar = sf.beat_in_bar(beat)
        local half_bar = math.floor(beat / HALF_BAR_BEATS)
        local hit = 0
        if in_bar < 1 then
            hit = sf.on_beat(beat, ONE_WIDTH_BEATS)
        elseif in_bar >= 2 and in_bar < 3 then
            hit = sf.on_beat(beat, THREE_WIDTH_BEATS)
        end
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local position = POSITIONS[half_bar % 2 + 1]
        local reach = sf.lerp(0.5, 1, energy)
        local bar = sf.bar(beat)
        return sf.scene {
            sf.colour(sf.group("wash"), half_bar % 2 == 0 and palette.primary or palette.accent),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, WASH_PEAK, hit) * gain),
            sf.colour(sf.group("strip"), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index)
                return sf.dimmer(pixel, (index + bar) % 2 == 0 and STRIP_LEVEL * gain or 0)
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * gain),
            sf.each(sf.group("movers"), function(mover, index)
                local pan = CENTRE_PAN + sf.side(index) * position.pan * reach
                return sf.pan_tilt(mover, pan, position.tilt)
            end)
        }
    end
}

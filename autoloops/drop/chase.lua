-- 8-beat loop, no strobe (Autolux adds a short one on the drop itself).
-- wash: primary, kick hit on every beat over an RMS-scaled floor.
-- strip: accent head running up the strip one pixel per 16th note (four laps per loop)
-- with a half-level tail, over a dim primary base.
-- movers: primary, alternate between two mirrored positions every bar.
-- Palette slots: primary, accent.
local palette = sf.palette("techno")
local LOOP_BEATS = 8
local BAR_BEATS = 4
local STEPS_PER_BEAT = 4
local KICK_WIDTH_BEATS = 0.4
local FLOOR_QUIET, FLOOR_LOUD = 0.05, 0.3
local WASH_PEAK, HEAD_LEVEL, BASE_LEVEL, MOVER_LEVEL = 0.9, 0.9, 0.12, 0.85
local CENTRE_PAN = 0.5
-- Pan is the offset from centre; movers mirror it left/right.
local POSITIONS = { { pan = 0.1, tilt = 0.6 }, { pan = 0.2, tilt = 0.55 } }

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
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
                local pan = CENTRE_PAN + sf.side(index) * position.pan * reach
                return sf.pan_tilt(mover, pan, position.tilt)
            end)
        }
    end
}

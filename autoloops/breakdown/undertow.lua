-- 16-beat loop, no beat pulses or strobe.
-- wash: off.
-- strip: cold, lit everywhere except a soft gap travelling the strip once per loop
-- (wrapping, so the restart is seamless).
-- movers: cold, still at centre. Palette slots: cold, black.
local palette = sf.palette("techno")
local LOOP_BEATS = 16
local STRIP_LEVEL = 0.3
local GAP_WIDTH = 1.5
local MOVER_LEVEL = 0.3
local CENTRE_PAN, TILT = 0.5, 0.6

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.black),
            sf.dimmer(sf.group("wash"), 0),
            sf.colour(sf.group("strip"), palette.cold),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local distance = math.abs(beat / LOOP_BEATS * count - (index - 1))
                distance = math.min(distance, count - distance)
                local gap = math.max(0, 1 - distance / GAP_WIDTH)
                return sf.dimmer(pixel, STRIP_LEVEL * (1 - gap) * level)
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.pan_tilt(sf.group("movers"), CENTRE_PAN, TILT),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * level)
        }
    end
}

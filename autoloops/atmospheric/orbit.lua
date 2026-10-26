-- 16-beat loop, no beat pulses or strobe.
-- energy: 1
-- family: cold
-- movement: sweep
-- wash: cold, steady at 0.15. strip: off.
-- movers: cold, small slow mirrored circular orbit (sine pan, cosine tilt) once per loop.
-- Palette slots: cold, black.
local palette = sf.palette("techno")
local LOOP_BEATS = 16
local WASH_LEVEL = 0.15
local MOVER_LEVEL = 0.32
local CENTRE_PAN, PARK_PAN = 0.5, 0.15
local CENTRE_TILT, RADIUS = 0.6, 0.05

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local angle = 2 * math.pi * beat / LOOP_BEATS
        return sf.scene {
            sf.colour(sf.group("wash"), palette.cold),
            sf.dimmer(sf.group("wash"), WASH_LEVEL * level),
            sf.colour(sf.group("strip"), palette.black),
            sf.dimmer(sf.group("strip"), 0),
            sf.colour(sf.group("movers"), palette.cold),
            sf.each(sf.group("movers"), function(mover, index)
                -- Mirrored pan, shared tilt: the two beams circle in opposite directions.
                -- Fixed radius: in a parked-wide orbit a loudness-scaled radius would swing closer to centre when loud.
                local pan = CENTRE_PAN + sf.side(index) * (PARK_PAN + RADIUS * math.sin(angle))
                return {
                    sf.pan_tilt(mover, pan, CENTRE_TILT + RADIUS * math.cos(angle)),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

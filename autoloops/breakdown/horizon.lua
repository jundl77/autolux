-- 16-beat loop
-- energy: 1
-- family: cold
-- movement: sweep
local palette = sf.palette("techno")
local LOOP_BEATS = 16
local GLOW_LEVEL = 0.3
local MOVER_LEVEL = 0.4
local CENTRE_PAN, TILT = 0.5, 0.62
local PAN_SWING = 0.25

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local pan = PAN_SWING * sf.lerp(0.5, 1, energy) * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.black),
            sf.dimmer(sf.group("wash"), 0),
            sf.colour(sf.group("strip"), palette.cold),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local position = beat / LOOP_BEATS * (count + 1) - 1
                local glow = math.max(0, 1 - math.abs(position - (index - 1)))
                return sf.dimmer(pixel, GLOW_LEVEL * glow * level)
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.each(sf.group("movers"), function(mover, index)
                return {
                    sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * pan, TILT),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

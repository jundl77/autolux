-- 16-beat loop
-- energy: 1
-- family: cold
-- movement: sweep
local palette = sf.palette("techno")
local LOOP_BEATS = 16
local PIXEL_LOW, PIXEL_HIGH = 0.03, 0.3
local CENTRE_PIXELS = { [4] = true, [5] = true }
local MOVER_LEVEL = 0.4
local CENTRE_PAN, PAN_SWING = 0.5, 0.2
local TILT_LOW, TILT_HIGH = 0.6, 0.82

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local breath = sf.osc.sine(beat, LOOP_BEATS / 2, PIXEL_LOW, PIXEL_HIGH)
        local cross = PAN_SWING * sf.lerp(0.5, 1, energy) * sf.osc.sine(beat, LOOP_BEATS, 1, -1)
        local tilt = sf.osc.sine(beat, LOOP_BEATS, TILT_LOW, TILT_HIGH)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.black),
            sf.dimmer(sf.group("wash"), 0),
            sf.colour(sf.group("strip"), palette.cold),
            sf.each(sf.group("strip"), function(pixel, index)
                return sf.dimmer(pixel, CENTRE_PIXELS[index] and breath * level or 0)
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.each(sf.group("movers"), function(mover, index)
                return {
                    sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * cross, tilt),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

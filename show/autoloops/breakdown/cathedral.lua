-- 16-beat loop
-- energy: 1
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local LOOP_BEATS = 16
local PIXEL_LOW, PIXEL_HIGH = 0.03, 0.3
local MOVER_LEVEL = 0.4
local CROSS_DEGREES, RISE_DEGREES = 20, 12
-- One mover turns a slow circle and back at the top of every 16 bars.
local SPIN = { measure = 16, every = 64 }

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = sf.energy(ctx)
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local breath = sf.osc.sine(beat, LOOP_BEATS / 2, PIXEL_LOW, PIXEL_HIGH)
        local cross = CROSS_DEGREES * sf.lerp(0.5, 1, energy) * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        local rise = RISE_DEGREES * sf.osc.sine(beat, LOOP_BEATS)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.black),
            sf.dimmer(sf.group("wash"), 0),
            sf.colour(sf.group("strip"), palette.cold),
            sf.each(sf.group("strip"), function(pixel, index, count)
                return sf.dimmer(pixel, sf.is_middle(index, count) and breath * level or 0)
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * level),
            sf.phaser(sf.group("movers"), ctx, { shape = "cross", size = cross, centre_tilt = rise, spin = SPIN })
        }
    end
}

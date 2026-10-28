-- 16-beat loop
-- energy: 1
-- movement: sweep
-- texture: mixed
local palette = sf.palette()
local LOOP_BEATS = 16
local WASH_LOW, WASH_HIGH = 0.06, 0.22
local STRIP_LOW, STRIP_HIGH = 0.02, 0.18
local MOVER_LEVEL = 0.3
local CENTRE_PAN, TILT = 0.5, 0.6
local PAN_SWING = 0.08

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = sf.energy(ctx)
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local swing = PAN_SWING * sf.lerp(0.5, 1, energy) * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        return sf.scene {
            sf.colour(sf.all(), palette.cold),
            sf.dimmer(sf.group("wash"), sf.osc.sine(beat, LOOP_BEATS, WASH_LOW, WASH_HIGH) * level),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local wave = sf.osc.sine(beat, LOOP_BEATS, STRIP_LOW, STRIP_HIGH, -(index - 1) / count)
                return sf.dimmer(pixel, wave * level)
            end),
            sf.each(sf.group("movers"), function(mover, index)
                local side = sf.side(index)
                return {
                    sf.pan_tilt(mover, CENTRE_PAN + side * swing, TILT),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

-- 16-beat loop
-- energy: 1
-- movement: still
-- texture: pixel
local palette = sf.palette()
local LOOP_BEATS = 16
local WASH_LOW, WASH_HIGH = 0.1, 0.18
local GLOW_LEVEL = 0.22
local MOVER_LEVEL = 0.3
local PARK_DEGREES, BREATHE_DEGREES = 22, 3

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local blend = sf.osc.sine(beat, LOOP_BEATS)
        local tilt = BREATHE_DEGREES * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        return sf.scene {
            sf.colour(sf.group("wash"), sf.mix(palette.secondary, palette.primary, blend)),
            sf.dimmer(sf.group("wash"), sf.osc.sine(beat, LOOP_BEATS, WASH_LOW, WASH_HIGH) * level),
            sf.colour(sf.group("strip"), palette.secondary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local position = sf.osc.triangle(beat, LOOP_BEATS, 0, count - 1)
                local glow = math.max(0, 1 - math.abs(position - (index - 1)))
                return sf.dimmer(pixel, GLOW_LEVEL * glow * level)
            end),
            sf.colour(sf.group("movers"), palette.primary),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * level),
            sf.phaser(sf.group("movers"), ctx, { shape = "fan", size = PARK_DEGREES, centre_tilt = tilt })
        }
    end
}

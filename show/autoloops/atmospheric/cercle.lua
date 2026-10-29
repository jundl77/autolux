-- 16-beat loop
-- energy: 1
-- movement: still
-- texture: pixel
local palette = sf.palette()
local LOOP_BEATS = 16
local WASH_LEVEL = 0.16
local HORIZON_LEVEL = 0.22
local MOVER_LEVEL = 0.25
local PARK_DEGREES = 25

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local dusk = sf.osc.sine(beat, LOOP_BEATS)
        return sf.scene {
            sf.colour(sf.group("wash"), sf.mix(palette.secondary, palette.accent, dusk)),
            sf.dimmer(sf.group("wash"), WASH_LEVEL * level),
            sf.colour(sf.group("strip"), palette.secondary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local from_centre = sf.from_centre(index, count)
                local band = math.max(0, 1 - 3 * math.abs(from_centre - dusk))
                return sf.dimmer(pixel, HORIZON_LEVEL * band * level)
            end),
            sf.colour(sf.group("movers"), palette.secondary),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * level),
            sf.phaser(sf.group("movers"), ctx, { shape = "fan", size = PARK_DEGREES })
        }
    end
}

-- 16-beat loop
-- energy: 2
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local FAN_DEGREES, FAN_MEASURE = 25, 16
local KICK_WIDTH_BEATS, WAVE_SHARE = 0.4, 0.19
local FLOOR_QUIET, FLOOR_LOUD = 0.05, 0.25
local WASH_PEAK, STRIP_PEAK, MOVER_FLOOR, MOVER_PEAK = 0.85, 0.9, 0.6, 0.85

return {
    render = function(ctx)
        local energy = sf.energy(ctx)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local kick = sf.on_beat(ctx, KICK_WIDTH_BEATS)
        local wave = ctx.beat_position % 1
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, WASH_PEAK, kick) * gain),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local distance = math.abs(sf.from_centre(index, count) - wave) * (count - 1) / 2
                local front = math.max(0, 1 - distance / sf.pixels(WAVE_SHARE, count)) * (1 - wave)
                return {
                    sf.colour(pixel, palette.secondary),
                    sf.dimmer(pixel, STRIP_PEAK * front * gain)
                }
            end),
            sf.colour(sf.group("movers"), palette.secondary),
            sf.dimmer(sf.group("movers"), sf.lerp(MOVER_FLOOR, MOVER_PEAK, kick) * gain),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "fan", size = FAN_DEGREES * sf.lerp(0.6, 1, energy), measure = FAN_MEASURE
            })
        }
    end
}

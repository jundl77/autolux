-- 8-beat loop
-- energy: 3
-- movement: snap
-- texture: wash
local palette = sf.palette()
local HALF_BAR_BEATS = 2
local KICK_WIDTH_BEATS = 0.4
local FLOOR_QUIET, FLOOR_LOUD = 0.05, 0.3
local WASH_PEAK, STRIP_LEVEL, MOVER_FLOOR, MOVER_PEAK = 0.85, 0.8, 0.5, 0.85
local SHAPES = { { shape = "fan", size = 15 }, { shape = "cross", size = 8, centre_tilt = 8 } }

return {
    render = function(ctx)
        local energy = sf.energy(ctx)
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local kick = sf.on_beat(ctx, KICK_WIDTH_BEATS)
        local peak = WASH_PEAK * sf.lerp(0.75, 1, sf.onset(ctx))
        local half_bar = math.floor(sf.section_beats(ctx) / HALF_BAR_BEATS) % 2
        local look = SHAPES[half_bar + 1]
        local reach = sf.lerp(0.5, 1, energy)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, peak, kick) * gain),
            sf.colour(sf.group("strip"), half_bar == 0 and palette.secondary or palette.primary),
            sf.dimmer(sf.group("strip"), STRIP_LEVEL * gain),
            sf.colour(sf.group("movers"), palette.primary),
            sf.dimmer(sf.group("movers"), sf.lerp(MOVER_FLOOR, MOVER_PEAK, kick) * gain),
            sf.phaser(sf.group("movers"), ctx, {
                shape = look.shape, size = look.size * reach, centre_tilt = (look.centre_tilt or 0) * reach
            })
        }
    end
}

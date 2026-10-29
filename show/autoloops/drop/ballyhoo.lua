-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local BALLYHOO_DEGREES, BALLYHOO_TILT_DEGREES, BALLYHOO_MEASURE = 30, 20, 8
local KICK_WIDTH_BEATS, SPARK_WIDTH_BEATS = 0.4, 0.3
local SPARK_SHARE, LOOP_EIGHTHS = 0.3, 16
local FLOOR_QUIET, FLOOR_LOUD = 0.08, 0.3
local WASH_PEAK, STRIP_PEAK, MOVER_FLOOR, MOVER_PEAK = 0.9, 0.9, 0.6, 0.85

return {
    render = function(ctx)
        local energy = sf.energy(ctx)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local kick = sf.on_beat(ctx, KICK_WIDTH_BEATS)
        local eighth = math.floor(ctx.beat_position * 2) % LOOP_EIGHTHS
        local spark = sf.on_beat(ctx, SPARK_WIDTH_BEATS, 0.5)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, WASH_PEAK, kick) * gain),
            sf.colour(sf.group("strip"), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index)
                local lit = sf.random(ctx, index, eighth) < SPARK_SHARE and spark or 0
                return sf.dimmer(pixel, STRIP_PEAK * lit * gain)
            end),
            sf.colour(sf.group("movers"), palette.secondary),
            sf.dimmer(sf.group("movers"), sf.lerp(MOVER_FLOOR, MOVER_PEAK, spark) * gain),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "ballyhoo", size = BALLYHOO_DEGREES, tilt_size = BALLYHOO_TILT_DEGREES,
                measure = BALLYHOO_MEASURE
            })
        }
    end
}

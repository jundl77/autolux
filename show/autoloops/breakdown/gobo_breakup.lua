-- 16-beat loop
-- energy: 1
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local BREAKUP_SLOT = 1
local CIRCLE_DEGREES, CIRCLE_MEASURE = 6, 16
local WASH_LEVEL, MOVER_LOW, MOVER_HIGH = 0.2, 0.45, 0.7

return {
    render = function(ctx)
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local swell = sf.osc.sine(sf.section_beats(ctx), CIRCLE_MEASURE)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_LEVEL * level),
            sf.colour(sf.group("strip"), palette.black),
            sf.dimmer(sf.group("strip"), 0),
            sf.colour(sf.group("movers"), palette.primary),
            sf.gobo(sf.group("movers"), BREAKUP_SLOT),
            sf.dimmer(sf.group("movers"), sf.lerp(MOVER_LOW, MOVER_HIGH, swell) * level),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "circle", size = CIRCLE_DEGREES, measure = CIRCLE_MEASURE
            })
        }
    end
}

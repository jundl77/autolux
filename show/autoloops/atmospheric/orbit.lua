-- 16-beat loop
-- energy: 1
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local ORBIT_BEATS = 16
local CIRCLE_DEGREES, CIRCLE_TILT_DEGREES = 12, 8
local WASH_LOW, WASH_HIGH = 0.06, 0.2
local GLOW_LEVEL, MOVER_LEVEL = 0.22, 0.35

-- One orbit drives the room: the beams circle their focus, the light swings round the washes
-- with it, and the strip's mirrored glow rides the beams' cross-stage swing.
return {
    render = function(ctx)
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local turn = 2 * math.pi * (sf.section_beats(ctx) / ORBIT_BEATS % 1)
        local swing = 0.5 + 0.5 * math.cos(turn)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.secondary),
            sf.each(sf.group("wash"), function(wash, index, count)
                local across = count > 1 and 2 * (index - 1) / (count - 1) - 1 or 0
                local near = 0.5 + 0.5 * math.sin(turn) * across
                return sf.dimmer(wash, sf.lerp(WASH_LOW, WASH_HIGH, near) * level)
            end),
            sf.colour(sf.group("strip"), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local glow = math.max(0, 1 - math.abs(sf.from_centre(index, count) - swing) * (count - 1) / 2)
                return sf.dimmer(pixel, GLOW_LEVEL * glow * level)
            end),
            sf.colour(sf.group("movers"), palette.primary),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * level),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "circle", size = CIRCLE_DEGREES, tilt_size = CIRCLE_TILT_DEGREES,
                measure = ORBIT_BEATS
            })
        }
    end
}

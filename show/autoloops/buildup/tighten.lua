-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local FAN_DEGREES, FAN_MEASURE = 30, 8
local WASH_HOLD, WASH_RISE = 0.3, 0.4
local STRIP_PEAK, MOVER_PEAK = 0.6, 0.9
local DOT_WIDTH = 1.5

-- One amount closes the fan onto the movers' shared point and walks the strip's two dots in:
-- the build reaches half of it, a cued drop's finale the rest. The washes hold back so the
-- closed beam point is the brightest thing in the room.
return {
    render = function(ctx)
        local build = sf.build(ctx)
        local converge = sf.lerp(0.5 * build.tension, 1, build.finale)
        local opening = (1 - converge) * sf.osc.sine(sf.section_beats(ctx), FAN_MEASURE, 1, 0)
        local movers = sf.group("movers")
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_HOLD + WASH_RISE * build.finale),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local distance = math.abs(sf.from_centre(index, count) - opening) * (count - 1) / 2
                local dot = math.max(0, 1 - distance / DOT_WIDTH)
                return sf.dimmer(pixel, STRIP_PEAK * build.drive * dot)
            end),
            sf.dimmer(movers, MOVER_PEAK * sf.lerp(0.5, 1, build.drive)),
            sf.phaser(movers, ctx, {
                shape = "fan", size = FAN_DEGREES * opening, at = sf.shared_focus(movers)
            })
        }
    end
}

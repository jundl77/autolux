-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local FAN_DEGREES, FAN_MEASURE, RISE_DEGREES = 30, 8, 15
local WASH_HOLD, WASH_RISE = 0.3, 0.4
local STRIP_PEAK, STRIP_GLOW, MOVER_PEAK = 0.6, 0.25, 0.9
local DOT_SHARE = 0.19

-- One amount closes the fan onto the movers' shared point and walks the strip's two dots in:
-- the build reaches half of it, a cued drop's finale the rest. The washes hold back so the
-- closed beam point is the brightest thing in the room.
return {
    render = function(ctx)
        local build = sf.build(ctx)
        local converge = sf.lerp(0.5 * build.tension, 1, build.finale)
        local opening = (1 - converge) * sf.osc.sine(sf.section_beats(ctx), FAN_MEASURE, 1, 0)
        local movers = sf.group("movers")
        local held_back = (WASH_HOLD + WASH_RISE * build.finale) * sf.lerp(0.4, 1, build.tension)
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), held_back),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local distance = math.abs(sf.from_centre(index, count) - opening) * (count - 1) / 2
                local dot = math.max(STRIP_GLOW, 1 - distance / sf.pixels(DOT_SHARE, count))
                return sf.dimmer(pixel, STRIP_PEAK * build.drive * dot)
            end),
            sf.dimmer(movers, MOVER_PEAK * build.drive),
            sf.phaser(movers, ctx, {
                shape = "fan", size = FAN_DEGREES * opening, at = sf.shared_focus(movers),
                centre_tilt = RISE_DEGREES * build.drive
            })
        }
    end
}

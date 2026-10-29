-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: mixed
local palette = sf.palette()
local PULSE_WIDTH_BEATS = 0.4
local FAN_DEGREES, LIFT_DEGREES = 12, 35
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85

local function pulsing(ctx, build)
    if sf.drop_within(build, ctx.beats_per_bar) then return 1 end
    return sf.lerp(0.35, 1, sf.on_beat(ctx, PULSE_WIDTH_BEATS))
end

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local pulse = pulsing(ctx, build)
        local level = build.drive * pulse
        local washes_in = build.stage >= 1
        local movers_in = build.stage >= 2
        local beat = math.floor(ctx.beat_position)
        local landing = 1 - sf.approach(build, ctx.beats_per_bar)
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index)
                local lit = washes_in or (index + beat) % 2 == 0
                return sf.dimmer(pixel, lit and STRIP_PEAK * sf.lerp(0.5, 1, build.drive) * pulse or 0)
            end),
            sf.dimmer(sf.group("wash"), washes_in and WASH_PEAK * level or 0),
            sf.dimmer(sf.group("movers"), movers_in and MOVER_PEAK * level or 0),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "fan", size = FAN_DEGREES * landing, measure = build.measure,
                centre_tilt = LIFT_DEGREES * build.tension * landing
            })
        }
    end
}

-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: mixed
local palette = sf.palette()
local WASH_ENTRY, MOVER_ENTRY = 0.33, 0.66
local PULSE_WIDTH_BEATS = 0.4
local FAN_DEGREES, LIFT_DEGREES = 12, 35
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85

-- Each group enters on the downbeat after the build's tension passes its line, so the look
-- stacks strip, then washes, then movers, and the beams lift with the tension. Onset scales
-- how hard a beat pulses, never where a head points; the last bar locks every pulse solid.
local function pulsing(ctx, build)
    if build.left ~= nil and build.left <= ctx.beats_per_bar then return 1 end
    return sf.lerp(0.35, 1, sf.on_beat(ctx, PULSE_WIDTH_BEATS) * sf.onset(ctx))
end

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local pulse = pulsing(ctx, build)
        local level = build.drive * pulse
        local washes_in = build.step >= WASH_ENTRY
        local movers_in = build.step >= MOVER_ENTRY
        local beat = math.floor(ctx.beat_position)
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index)
                local lit = washes_in or (index + beat) % 2 == 0
                return sf.dimmer(pixel, lit and STRIP_PEAK * sf.lerp(0.5, 1, build.drive) * pulse or 0)
            end),
            sf.dimmer(sf.group("wash"), washes_in and WASH_PEAK * level or 0),
            sf.dimmer(sf.group("movers"), movers_in and MOVER_PEAK * level or 0),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "fan", size = FAN_DEGREES, centre_tilt = LIFT_DEGREES * build.tension
            })
        }
    end
}

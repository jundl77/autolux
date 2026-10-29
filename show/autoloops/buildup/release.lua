-- 8-beat loop
-- energy: 2
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local HOLD_UNTIL = 0.5
local SWEEP_DEGREES, TIGHT_DEGREES, RISE_DEGREES = 30, 6, 15
local WASH_HELD, MOVER_HELD = 0.4, 0.6
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85
local LAP_BEATS, TAIL_SHARE = 2, 0.3

local function letting_go(build)
    local open = math.max(0, (build.tension - HOLD_UNTIL) / (1 - HOLD_UNTIL))
    return open + (1 - open) * build.finale
end

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local open = letting_go(build)
        local bar = ctx.beats_per_bar
        local tightening = sf.approach(build, bar)
        local sweep = sf.lerp(SWEEP_DEGREES * open, TIGHT_DEGREES, tightening)
        local head = ctx.beat_position / LAP_BEATS % 1
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_PEAK * build.drive * sf.lerp(WASH_HELD, 1, open)),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local dot = math.abs(index - (count + 1) / 2) < 1 and 1 or 0
                local behind = (head * count - (index - 1)) % count
                local chase = math.max(0, 1 - behind / sf.pixels(TAIL_SHARE, count))
                return sf.dimmer(pixel, STRIP_PEAK * build.drive * sf.lerp(dot, chase, open))
            end),
            sf.dimmer(sf.group("movers"), MOVER_PEAK * build.drive * sf.lerp(MOVER_HELD, 1, open)),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "fan", size = sweep, measure = build.measure, centre_tilt = RISE_DEGREES * build.drive
            })
        }
    end
}

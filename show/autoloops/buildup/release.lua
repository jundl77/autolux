-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local HOLD_UNTIL = 0.5
local SWEEP_DEGREES, TIGHT_DEGREES, RISE_DEGREES = 30, 6, 15
local WASH_HELD, DOT_HELD, MOVER_HELD = 0.12, 0.3, 0.3
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85
local LAP_BEATS, TAIL_SHARE = 2, 0.3

-- Held back, then let go: until half-way the room is dim and motionless, one dot and beams
-- parked on their focus; then, and over a cued drop's finale, the beams open into a sweep and
-- the dot into a chase. The last bar eases the sweep tight but keeps the chase.
local function letting_go(build)
    local open = math.max(0, (build.tension - HOLD_UNTIL) / (1 - HOLD_UNTIL))
    return open + (1 - open) * build.finale
end

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local open = letting_go(build)
        local bar = ctx.beats_per_bar
        local tightening = build.left and math.max(0, math.min(1, (bar - build.left) / bar)) or 0
        local sweep = sf.lerp(SWEEP_DEGREES * open, TIGHT_DEGREES, tightening)
        local head = ctx.beat_position / LAP_BEATS % 1
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_PEAK * sf.lerp(WASH_HELD, build.drive, open)),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local dot = math.abs(index - (count + 1) / 2) < 1 and DOT_HELD or 0
                local behind = (head * count - (index - 1)) % count
                local chase = math.max(0, 1 - behind / sf.pixels(TAIL_SHARE, count)) * build.drive
                return sf.dimmer(pixel, STRIP_PEAK * sf.lerp(dot, chase, open))
            end),
            sf.dimmer(sf.group("movers"), MOVER_PEAK * sf.lerp(MOVER_HELD, build.drive, open)),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "fan", size = sweep, measure = build.measure, centre_tilt = RISE_DEGREES * build.drive
            })
        }
    end
}

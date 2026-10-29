-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local HOLD_UNTIL = 0.5
local SWEEP_DEGREES, SWEEP_MEASURE, TIGHT_DEGREES = 30, 2, 6
local WASH_HELD, DOT_HELD, MOVER_HELD = 0.12, 0.3, 0.3
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85
local BOUNCE_BEATS, TAIL_PIXELS = 4, 2.5

-- Held back, then let go: until half-way the room is dim and motionless, one dot and beams
-- parked on their focus; then, and over a cued drop's finale, the beams open into a sweep and
-- the dot into a bouncing chase. The last bar snaps the sweep tight but keeps the chase.
local function letting_go(build)
    local open = math.max(0, (build.tension - HOLD_UNTIL) / (1 - HOLD_UNTIL))
    return open + (1 - open) * build.finale
end

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local open = letting_go(build)
        local last_bar = build.left ~= nil and build.left <= ctx.beats_per_bar
        local sweep = last_bar and TIGHT_DEGREES or SWEEP_DEGREES * open
        local head = sf.osc.triangle(ctx.beat_position, BOUNCE_BEATS)
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_PEAK * sf.lerp(WASH_HELD, build.drive, open)),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local dot = math.abs(index - (count + 1) / 2) < 1 and DOT_HELD or 0
                local behind = math.abs(head * (count - 1) - (index - 1))
                local chase = math.max(0, 1 - behind / TAIL_PIXELS) * build.drive
                return sf.dimmer(pixel, STRIP_PEAK * sf.lerp(dot, chase, open))
            end),
            sf.dimmer(sf.group("movers"), MOVER_PEAK * sf.lerp(MOVER_HELD, build.drive, open)),
            sf.phaser(sf.group("movers"), ctx, { shape = "fan", size = sweep, measure = SWEEP_MEASURE })
        }
    end
}

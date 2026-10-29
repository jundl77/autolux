-- 8-beat loop
-- energy: 2
-- movement: still
-- texture: wash
local palette = sf.palette()
local BREATH_BEATS, HOLD_BEATS = 8, 16
local RISE_DEGREES, FAN_DEGREES = 15, 10
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85

-- Two-bar breaths that deepen with the build; once a drop is cued the room holds its breath:
-- every layer freezes at the top of the inhale and is completely still for the last bar.
local function held_breath(ctx, build)
    local breath = sf.osc.sine(sf.section_beats(ctx), BREATH_BEATS)
    if build.left == nil then return breath end
    local hold = (HOLD_BEATS - build.left) / (HOLD_BEATS - ctx.beats_per_bar)
    return sf.lerp(breath, 1, math.max(0, math.min(1, hold)))
end

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local breath = held_breath(ctx, build)
        local level = build.drive * sf.lerp(sf.lerp(0.5, 0.15, build.tension), 1, breath)
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_PEAK * level),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local reach = (breath - sf.from_centre(index, count)) * count / 2 + 1
                local filled = math.max(0, math.min(1, reach))
                return sf.dimmer(pixel, STRIP_PEAK * sf.lerp(0.3, 1, build.drive) * filled)
            end),
            sf.dimmer(sf.group("movers"), MOVER_PEAK * level),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "fan", size = FAN_DEGREES, centre_tilt = RISE_DEGREES * breath
            })
        }
    end
}

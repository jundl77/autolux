-- 8-beat loop
-- energy: 3
-- movement: snap
-- texture: wash
local palette = sf.palette()
local ROLL_BEATS, PULSE_SHARE = 8, 0.45
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85
local WIDE_DEGREES, NARROW_DEGREES, RISE_DEGREES = 20, 6, 15

local function pulse_period(ctx, build)
    if sf.drop_within(build, ROLL_BEATS) then return 0.5 end
    return ({ ctx.beats_per_bar, ctx.beats_per_bar / 2, 1 })[build.stage + 1]
end

return {
    render = function(ctx)
        local build = sf.build(ctx)
        local period = pulse_period(ctx, build)
        local pulse = sf.on_beat(ctx, PULSE_SHARE * period, period)
        local count = math.floor(ctx.beat_position / period)
        local filled = sf.approach(build, ROLL_BEATS)
        local level = sf.lerp(sf.lerp(0.08, 1, filled), 1, pulse)
        local look, dark = sf.snap(ctx, ctx.beats_per_bar)
        local rolling = period < 1
        local spread = (rolling or look == 1) and NARROW_DEGREES or WIDE_DEGREES
        local movers_lit = (dark and not rolling) and 0 or 1
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_PEAK * build.drive * level),
            sf.each(sf.group("strip"), function(pixel, index)
                local lit = (index + count) % 2 == 0 and level or level * filled
                return sf.dimmer(pixel, STRIP_PEAK * build.drive * lit)
            end),
            sf.dimmer(sf.group("movers"), MOVER_PEAK * build.drive * level * movers_lit),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "fan", size = spread, centre_tilt = RISE_DEGREES * build.drive
            })
        }
    end
}

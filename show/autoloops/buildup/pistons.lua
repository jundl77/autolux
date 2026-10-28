-- 8-beat loop
-- energy: 3
-- movement: snap
-- texture: mixed
local palette = sf.palette()
local PLATEAU_BEATS, FINALE_BEATS, EASE_BEATS = 64, 16, 8
local DUCK_SHARE = 0.6
local STROKE_DEGREES = 10
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85

-- Regime U builds on elapsed beats; a cued drop hands over continuously through the finale.
local function drive_and_finale(ctx)
    local drive = 0.15 + 0.55 * sf.tension(ctx, PLATEAU_BEATS)
    if ctx.next_intent == "drop" then
        local finale = sf.finale(ctx, FINALE_BEATS)
        return drive + (1 - drive) * finale, finale
    end
    local ease = ctx.next_intent and sf.finale(ctx, EASE_BEATS) or 0
    return drive * (1 - 0.5 * ease), 0
end

-- A sidechain pump: each stroke ducks right after its beat and recovers before the next. The pump
-- deepens with the build, doubles over the drop's last two bars and locks solid in the last.
local function stroke(ctx, finale)
    local left = finale > 0 and ctx.beats_until_next or math.huge
    local period = left <= 8 and 0.5 or 1
    local depth = (left <= ctx.beats_per_bar and 0 or 1) * sf.lerp(0.3, 0.8, sf.tension(ctx, PLATEAU_BEATS))
    return period, depth * (ctx.onset or 1)
end

return {
    render = function(ctx)
        local drive, finale = drive_and_finale(ctx)
        local period, depth = stroke(ctx, finale)
        local duck = math.sin(math.pi * math.min(1, (ctx.beat_position % period) / (DUCK_SHARE * period)))
        local pump = 1 - depth * duck
        local count = math.floor(ctx.beat_position / period)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_PEAK * drive * pump * gain),
            sf.each(sf.group("strip"), function(pixel, index)
                local stroke_on = depth == 0 or (index + count) % 2 == 0
                return sf.dimmer(pixel, STRIP_PEAK * sf.lerp(0.4, 1, drive) * (stroke_on and pump or 0) * gain)
            end),
            sf.dimmer(sf.group("movers"), MOVER_PEAK * sf.lerp(0.4, 1, drive) * pump * gain),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "tilt_wave", size = 0, tilt_size = STROKE_DEGREES * depth, measure = 2 * period, phase = 1
            })
        }
    end
}

-- 8-beat loop
-- energy: 2
-- movement: still
-- texture: wash
local palette = sf.palette()
local PLATEAU_BEATS, FINALE_BEATS, EASE_BEATS = 64, 16, 8
local BREATH_BEATS = 8
local RISE_DEGREES, FAN_DEGREES = 15, 10
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

-- Two-bar breaths that deepen with the build; once the drop is cued the room holds its breath:
-- each layer freezes at the top of the inhale and is completely still for the last bar.
local function held_breath(ctx, finale)
    local breath = sf.osc.sine(ctx.beat_position, BREATH_BEATS)
    local hold = math.min(1, finale * FINALE_BEATS / (FINALE_BEATS - ctx.beats_per_bar))
    return sf.lerp(breath, 1, hold)
end

return {
    render = function(ctx)
        local drive, finale = drive_and_finale(ctx)
        local breath = held_breath(ctx, finale)
        local floor = sf.lerp(0.5, 0.15, sf.tension(ctx, PLATEAU_BEATS))
        local level = drive * sf.lerp(floor, 1, breath)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_PEAK * level * gain),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local from_centre = count > 1 and math.abs(2 * (index - 1) / (count - 1) - 1) or 0
                local filled = math.max(0, math.min(1, (breath - from_centre) * count / 2 + 1))
                return sf.dimmer(pixel, STRIP_PEAK * sf.lerp(0.3, 1, drive) * filled * gain)
            end),
            sf.dimmer(sf.group("movers"), MOVER_PEAK * level * gain),
            sf.phaser(sf.group("movers"), ctx, { shape = "fan", size = FAN_DEGREES, centre_tilt = RISE_DEGREES * breath })
        }
    end
}

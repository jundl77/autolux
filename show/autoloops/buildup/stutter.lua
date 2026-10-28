-- 8-beat loop
-- energy: 3
-- movement: snap
-- texture: wash
local palette = sf.palette()
local PLATEAU_BEATS, FINALE_BEATS, EASE_BEATS = 64, 16, 8
local PULSE_SHARE = 0.45
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85
local WIDE_DEGREES, NARROW_DEGREES = 20, 6

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

-- The roll halves on downbeats: a bar, two beats, a beat over the build, then half and a
-- quarter beat over the drop's last two bars.
local function pulse_period(ctx, finale)
    local downbeat = math.max(0, (ctx.section_elapsed_beats or ctx.autoloop_elapsed_beats) - ctx.bar_phase)
    local period = downbeat < PLATEAU_BEATS / 2 and 4 or downbeat < 3 * PLATEAU_BEATS / 4 and 2 or 1
    if finale == 0 then return period end
    local left = ctx.beats_until_next
    return math.min(period, left > 8 and 1 or left > 4 and 0.5 or 0.25)
end

return {
    render = function(ctx)
        local drive, finale = drive_and_finale(ctx)
        local period = pulse_period(ctx, finale)
        local pulse = sf.on_beat(ctx, PULSE_SHARE * period, period)
        local count = math.floor(ctx.beat_position / period)
        local floor = sf.lerp(0.08, 0.45, finale)
        local level = sf.lerp(floor, 1, pulse)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local spread = period >= 1 and count % 2 == 0 and WIDE_DEGREES or NARROW_DEGREES
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_PEAK * drive * level * gain),
            sf.each(sf.group("strip"), function(pixel, index)
                local lit = (index + count) % 2 == 0 and pulse or floor * pulse
                return sf.dimmer(pixel, STRIP_PEAK * sf.lerp(0.4, 1, drive) * lit * gain)
            end),
            sf.dimmer(sf.group("movers"), MOVER_PEAK * sf.lerp(0.4, 1, drive) * level * gain),
            sf.phaser(sf.group("movers"), ctx, { shape = "fan", size = spread })
        }
    end
}

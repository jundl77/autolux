-- 8-beat loop
-- energy: 2
-- movement: sweep
-- texture: pixel
local palette = sf.palette()
local PLATEAU_BEATS, FINALE_BEATS, EASE_BEATS = 64, 16, 8
local TAIL_PIXELS = 2.5
local CIRCLE_DEGREES = 12
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85
local LAP_PULSE_SHARE = 0.3

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

-- Speed doubles on downbeats where every older lap also ends, so neither the chase head nor
-- the circles jump: one lap a bar, then two, then four, and eight over the drop's last two bars.
local function lap_beats(ctx, finale)
    local downbeat = math.max(0, (ctx.section_elapsed_beats or ctx.autoloop_elapsed_beats) - ctx.bar_phase)
    local beats = downbeat < PLATEAU_BEATS / 2 and 4 or downbeat < 3 * PLATEAU_BEATS / 4 and 2 or 1
    if finale > 0 and ctx.beats_until_next <= 8 then return 0.5 end
    return beats
end

return {
    render = function(ctx)
        local drive, finale = drive_and_finale(ctx)
        local lap = lap_beats(ctx, finale)
        local head = ctx.beat_position / lap % 1
        local closing = finale > 0
            and math.min(1, (FINALE_BEATS - ctx.beats_until_next) / (FINALE_BEATS - ctx.beats_per_bar)) or 0
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local wash = sf.lerp(0.35, 1, sf.on_beat(ctx, LAP_PULSE_SHARE * lap, lap))
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_PEAK * drive * wash * gain),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local behind = (head * count - (index - 1)) % count
                local lit = math.max(0, 1 - behind / TAIL_PIXELS)
                local level = STRIP_PEAK * sf.lerp(0.35, 1, drive) * gain
                return sf.dimmer(pixel, level * sf.lerp(lit, 1, closing))
            end),
            sf.dimmer(sf.group("movers"), MOVER_PEAK * sf.lerp(0.4, 1, drive) * gain),
            sf.phaser(sf.group("movers"), ctx, {
                shape = "circle", size = CIRCLE_DEGREES * (1 - closing), measure = math.max(1, 2 * lap)
            })
        }
    end
}

-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: pixel
local palette = sf.palette()
local PLATEAU_BEATS, FINALE_BEATS, EASE_BEATS = 64, 16, 8
local COUNT_BEATS = 8
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85
local TICK_WIDTH_BEATS = 0.45
local RAISED_DEGREES = 20

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

-- The bar fills from the centre one step per downbeat, tops up while the drop approaches,
-- then counts back one step per beat over the last eight, so the count lands on the drop.
local function lit_share(ctx, finale)
    local downbeat = math.max(0, (ctx.section_elapsed_beats or ctx.autoloop_elapsed_beats) - ctx.bar_phase)
    local fill = math.min(1, downbeat / PLATEAU_BEATS)
    if finale == 0 then return fill end
    local left = math.ceil(ctx.beats_until_next)
    if left <= COUNT_BEATS then return left / COUNT_BEATS end
    return fill + (1 - fill) * (FINALE_BEATS - left) / (FINALE_BEATS - COUNT_BEATS)
end

return {
    render = function(ctx)
        local drive, finale = drive_and_finale(ctx)
        local share = lit_share(ctx, finale)
        local counting = finale > 0 and ctx.beats_until_next <= COUNT_BEATS
        local tick = sf.on_beat(ctx, TICK_WIDTH_BEATS, counting and 1 or ctx.beats_per_bar)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_PEAK * drive * sf.lerp(0.3, 1, tick) * gain),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local ranks = math.ceil(count / 2)
                local from_centre = math.floor(math.abs(index - (count + 1) / 2))
                local lit = math.max(0, math.min(1, share * ranks - from_centre))
                return sf.dimmer(pixel, STRIP_PEAK * lit * sf.lerp(0.5, 1, drive) * gain)
            end),
            sf.dimmer(sf.group("movers"), MOVER_PEAK * drive * sf.lerp(0.5, 1, tick) * gain),
            sf.phaser(sf.group("movers"), ctx, { shape = "fan", size = 0, centre_tilt = RAISED_DEGREES * (1 - drive) })
        }
    end
}

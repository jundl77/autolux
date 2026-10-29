-- 16-beat loop
-- energy: 3
-- movement: snap
-- texture: wash
local palette = sf.palette()
local LOOP_BEATS = 8
local KICK_WIDTH_BEATS, GHOST_WIDTH_BEATS = 0.4, 0.3
local GHOST_AT_IN_BAR, GHOST_OFFSET_BEATS = 3.5, 0.5
local GHOST_LEVEL = 0.5
local FLOOR_QUIET, FLOOR_LOUD = 0.04, 0.2
local WASH_PEAK, STRIP_PEAK, MOVER_FLOOR, MOVER_PEAK = 0.9, 0.85, 0.6, 0.85
local LOOKS = { { shape = "fan", size = 20, centre_tilt = 0 }, { shape = "fan", size = 8, centre_tilt = 10 } }

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local gain, floor, energy = sf.drop_levels(ctx, FLOOR_QUIET, FLOOR_LOUD)
        local hit = sf.on_beat(beat, KICK_WIDTH_BEATS)
        local in_bar = sf.beat_in_bar(beat)
        if in_bar >= GHOST_AT_IN_BAR then
            hit = math.max(hit, GHOST_LEVEL * sf.on_beat(in_bar - GHOST_OFFSET_BEATS, GHOST_WIDTH_BEATS))
        end
        local look, dark = sf.snap_look(ctx, ctx.beats_per_bar, LOOKS, sf.lerp(0.5, 1, energy))
        local swapped = sf.colours_swapped(ctx)
        return sf.scene {
            sf.colour(sf.group("wash"), swapped and palette.secondary or palette.primary),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, WASH_PEAK, hit) * gain),
            sf.colour(sf.group("strip"), palette.primary),
            sf.dimmer(sf.group("strip"), STRIP_PEAK * hit * gain),
            sf.colour(sf.group("movers"), swapped and palette.primary or palette.secondary),
            sf.dimmer(sf.group("movers"), dark and 0 or sf.lerp(MOVER_FLOOR, MOVER_PEAK, hit) * gain),
            sf.phaser(sf.group("movers"), ctx, look)
        }
    end
}

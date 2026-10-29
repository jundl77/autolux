-- 16-beat loop
-- energy: 3
-- movement: snap
-- texture: beam
local palette = sf.palette()
local LOOP_BEATS = 8
local KICK_WIDTH_BEATS = 0.4
local OFFBEAT_WIDTH_BEATS, OFFBEAT_AT = 0.3, 0.5
local FLOOR_QUIET, FLOOR_LOUD = 0.04, 0.2
local WASH_PEAK, STRIP_LEVEL, MOVER_FLOOR, MOVER_PEAK = 0.9, 0.35, 0.6, 0.85
local LOOKS = {
    { shape = "fan", size = 22, centre_tilt = 0 },
    { shape = "cross", size = 10, centre_tilt = 12 }
}

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local gain, floor, energy = sf.drop_levels(ctx, FLOOR_QUIET, FLOOR_LOUD)
        local kick = sf.on_beat(beat, KICK_WIDTH_BEATS)
        local offbeat = STRIP_LEVEL * sf.on_beat(beat - OFFBEAT_AT, OFFBEAT_WIDTH_BEATS)
        local look, dark = sf.snap_look(ctx, ctx.beats_per_bar, LOOKS, sf.lerp(0.5, 1, energy))
        local swapped = sf.colours_swapped(ctx)
        return sf.scene {
            sf.colour(sf.group("wash"), swapped and palette.secondary or palette.primary),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, WASH_PEAK, kick) * gain),
            sf.colour(sf.group("strip"), palette.accent),
            sf.dimmer(sf.group("strip"), offbeat * gain),
            sf.colour(sf.group("movers"), swapped and palette.primary or palette.secondary),
            sf.dimmer(sf.group("movers"), dark and 0 or sf.lerp(MOVER_FLOOR, MOVER_PEAK, kick) * gain),
            sf.phaser(sf.group("movers"), ctx, look)
        }
    end
}

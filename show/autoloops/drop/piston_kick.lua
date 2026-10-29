-- 8-beat loop
-- energy: 2
-- movement: snap
-- texture: wash
local palette = sf.palette()
local LOOP_BEATS = 8
local KICK_WIDTH_BEATS, STRIP_WIDTH_BEATS = 0.4, 0.7
local FLOOR_QUIET, FLOOR_LOUD = 0.04, 0.2
local WASH_PEAK, STRIP_PEAK, STRIP_BASE = 0.9, 0.9, 0.1
local MOVER_FLOOR, MOVER_PEAK = 0.6, 0.85
local LOOKS = { { shape = "fan", size = 18, centre_tilt = 0 }, { shape = "fan", size = 7, centre_tilt = 9 } }

local OUTER_FROM = 0.5

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = sf.energy(ctx)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local kick = sf.on_beat(beat, KICK_WIDTH_BEATS)
        local outer_turn = math.floor(beat) % 2 == 0
        local in_bar = math.floor(sf.beat_in_bar(beat))
        local strip_hit = (in_bar == 0 or in_bar == 2) and sf.on_beat(beat, STRIP_WIDTH_BEATS) or 0
        local snap, dark = sf.snap(ctx, ctx.beats_per_bar)
        local look = LOOKS[snap + 1]
        local reach = sf.lerp(0.5, 1, energy)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            sf.each(sf.group("wash"), function(wash, index, count)
                local hit = (sf.from_centre(index, count) > OUTER_FROM) == outer_turn and kick or 0
                return sf.dimmer(wash, sf.lerp(floor, WASH_PEAK, hit) * gain)
            end),
            sf.colour(sf.group("strip"), in_bar == 2 and palette.accent or palette.primary),
            sf.dimmer(sf.group("strip"), sf.lerp(STRIP_BASE, STRIP_PEAK, strip_hit) * gain),
            sf.colour(sf.group("movers"), palette.secondary),
            sf.dimmer(sf.group("movers"), dark and 0 or sf.lerp(MOVER_FLOOR, MOVER_PEAK, kick) * gain),
            sf.phaser(sf.group("movers"), ctx, {
                shape = look.shape, size = look.size * reach, centre_tilt = look.centre_tilt * reach
            })
        }
    end
}

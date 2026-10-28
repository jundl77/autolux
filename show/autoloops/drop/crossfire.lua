-- 8-beat loop
-- energy: 3
-- movement: snap
-- texture: beam
local palette = sf.palette()
local LOOP_BEATS = 8
local KICK_WIDTH_BEATS = 0.4
local OFFBEAT_DUTY, OFFBEAT_PHASE = 0.3, 0.5
local FLOOR_QUIET, FLOOR_LOUD = 0.04, 0.2
local WASH_PEAK, STRIP_LEVEL, MOVER_LEVEL = 0.9, 0.35, 0.85
local LOOKS = { { shape = "fan", size = 22, centre_tilt = 0 }, { shape = "cross", size = 10, centre_tilt = 12 } }

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = sf.energy(ctx)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local kick = sf.on_beat(beat, KICK_WIDTH_BEATS)
        local offbeat = sf.osc.pulse(beat, 1, 0, STRIP_LEVEL, OFFBEAT_DUTY, OFFBEAT_PHASE)
        local look = LOOKS[math.floor(beat) % 2 + 1]
        local reach = sf.lerp(0.5, 1, energy)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.cold),
            sf.dimmer(sf.group("wash"), sf.lerp(floor, WASH_PEAK, kick) * gain),
            sf.colour(sf.group("strip"), palette.accent),
            sf.dimmer(sf.group("strip"), offbeat * gain),
            sf.colour(sf.group("movers"), palette.primary),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * gain),
            sf.phaser(sf.group("movers"), ctx, {
                shape = look.shape, size = look.size * reach, centre_tilt = look.centre_tilt * reach
            })
        }
    end
}

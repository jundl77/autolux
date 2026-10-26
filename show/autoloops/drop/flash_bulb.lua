-- 8-beat loop
-- energy: 2
-- movement: snap
-- texture: wash
local palette = sf.palette()
local LOOP_BEATS = 8
local HALF_BAR_BEATS = 2
local FLASH_WIDTH_BEATS, KICK_WIDTH_BEATS = 1.5, 0.4
local FILL_QUIET, FILL_LOUD = 0.05, 0.3
local WASH_PEAK, STRIP_PEAK, MOVER_LEVEL = 0.9, 0.85, 0.85
local CENTRE_PAN = 0.5
local POSITIONS = { { pan_offset = 0.12, tilt = 0.6 }, { pan_offset = 0.2, tilt = 0.5 } }

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local fill = sf.lerp(FILL_QUIET, FILL_LOUD, energy)
        local flash = sf.on_beat(beat, FLASH_WIDTH_BEATS, HALF_BAR_BEATS)
        local kick = sf.on_beat(beat, KICK_WIDTH_BEATS)
        local position = POSITIONS[sf.bar(beat) % 2 + 1]
        local reach = sf.lerp(0.5, 1, energy)
        return sf.scene {
            sf.colour(sf.group("wash"), sf.mix(palette.accent, palette.cold, flash)),
            sf.dimmer(sf.group("wash"), sf.lerp(fill, WASH_PEAK, flash) * gain),
            sf.colour(sf.group("strip"), palette.primary),
            sf.dimmer(sf.group("strip"), STRIP_PEAK * kick * gain),
            sf.colour(sf.group("movers"), palette.cold),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * gain),
            sf.each(sf.group("movers"), function(mover, index)
                local pan = CENTRE_PAN + sf.side(index) * position.pan_offset * reach
                return sf.pan_tilt(mover, pan, position.tilt)
            end)
        }
    end
}

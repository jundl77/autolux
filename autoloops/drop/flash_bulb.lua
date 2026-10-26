-- 8-beat loop, no strobe (Autolux adds a short one on the drop itself).
-- wash: cold flash on beats 1 and 3 with a long decay that melts into an accent fill
-- (up to 0.3 with RMS); beats 2 and 4 stay in the fill.
-- strip: primary kick hit on every beat.
-- movers: cold, snap between two mirrored positions every bar.
-- Palette slots: cold, accent, primary.
local palette = sf.palette("techno")
local LOOP_BEATS = 8
local HALF_BAR_BEATS = 2
local FLASH_WIDTH_BEATS, KICK_WIDTH_BEATS = 1.5, 0.4
local FILL_QUIET, FILL_LOUD = 0.05, 0.3
local WASH_PEAK, STRIP_PEAK, MOVER_LEVEL = 0.9, 0.85, 0.85
local CENTRE_PAN = 0.5
-- Pan is the offset from centre; movers mirror it left/right.
local POSITIONS = { { pan = 0.12, tilt = 0.6 }, { pan = 0.2, tilt = 0.5 } }

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
                local pan = CENTRE_PAN + sf.side(index) * position.pan * reach
                return sf.pan_tilt(mover, pan, position.tilt)
            end)
        }
    end
}

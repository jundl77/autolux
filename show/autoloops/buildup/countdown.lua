-- 16-beat loop
-- energy: 2
-- movement: sweep
-- texture: pixel
local palette = sf.palette()
local LOOP_BEATS = 16
local RAMP_BEATS = 128
local RAMP_SHARE = 0.85
local BEATS_PER_COUNT = 2
local FLASH_WIDTH_BEATS = 0.6
local WASH_PEAK = 0.9
local CENTRE_PAN, TILT = 0.5, 0.6
local START_OFFSET, END_OFFSET = 0.2, 0.02

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local ramp = math.min(ctx.autoloop_elapsed_beats / RAMP_BEATS, 1)
        local energy = ctx.normalized_rms_level or 0.6
        local drive = ramp * RAMP_SHARE + energy * (1 - RAMP_SHARE)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local on_the_one = sf.beat_in_bar(beat) < 1
        local flash = on_the_one and sf.on_beat(beat, FLASH_WIDTH_BEATS) or 0
        local base = sf.lerp(0.15, 0.4, drive)
        local wash = (base + (WASH_PEAK - base) * sf.lerp(0.1, 1, ramp) * flash) * gain
        local remaining = 8 - math.floor(beat / BEATS_PER_COUNT)
        local offset = sf.lerp(START_OFFSET, END_OFFSET, ramp) * sf.lerp(0.5, 1, energy)
        return sf.scene({
            sf.colour(sf.group("wash"), sf.mix(palette.secondary, palette.accent, flash)),
            sf.dimmer(sf.group("wash"), wash),
            sf.colour(sf.group("strip"), palette.cold),
            sf.each(sf.group("strip"), function(pixel, index)
                local lit = index <= remaining and 1 or 0
                return sf.dimmer(pixel, sf.lerp(0.3, 0.9, drive) * gain * lit)
            end),
            sf.colour(sf.group("movers"), palette.primary),
            sf.dimmer(sf.group("movers"), sf.lerp(0.3, 0.8, drive) * gain),
            sf.each(sf.group("movers"), function(mover, index)
                return sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * offset, TILT)
            end)
        })
    end
}

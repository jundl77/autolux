-- 8-beat loop
-- energy: 2
-- family: warm
-- movement: sweep
local palette = sf.palette()
local LOOP_BEATS = 8
local RAMP_BEATS = 128
local RAMP_SHARE = 0.85
local BEATS_PER_PIXEL = 16
local FLASH_WIDTH_BEATS = 0.6
local WASH_PEAK = 0.9
local CENTRE_PAN, SPREAD = 0.5, 0.15
local LOW_TILT, HIGH_TILT = 0.75, 0.45

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local elapsed = ctx.autoloop_elapsed_beats
        local ramp = math.min(elapsed / RAMP_BEATS, 1)
        local energy = ctx.normalized_rms_level or 0.6
        local drive = ramp * RAMP_SHARE + energy * (1 - RAMP_SHARE)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local on_the_one = sf.beat_in_bar(beat) < 1
        local flash = on_the_one and sf.on_beat(beat, FLASH_WIDTH_BEATS) or 0
        local base = sf.lerp(0.15, 0.4, drive)
        local wash = (base + (WASH_PEAK - base) * sf.lerp(0.1, 1, ramp) * flash) * gain
        local filled = math.floor(elapsed / BEATS_PER_PIXEL) + 1
        return sf.scene({
            sf.colour(sf.group("wash"), sf.mix(palette.secondary, palette.accent, flash)),
            sf.dimmer(sf.group("wash"), wash),
            sf.colour(sf.group("strip"), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index)
                local lit = index <= filled and 1 or 0
                return sf.dimmer(pixel, sf.lerp(0.3, 0.9, drive) * gain * lit)
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.dimmer(sf.group("movers"), sf.lerp(0.25, 0.8, drive) * gain),
            sf.each(sf.group("movers"), function(mover, index)
                local pan = CENTRE_PAN + sf.side(index) * SPREAD * sf.lerp(0.5, 1, energy)
                return sf.pan_tilt(mover, pan, sf.lerp(LOW_TILT, HIGH_TILT, ramp))
            end)
        })
    end
}

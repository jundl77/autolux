-- 8-beat loop
-- energy: 2
-- movement: sweep
-- texture: mixed
local palette = sf.palette()
local LOOP_BEATS = 8
local RAMP_BEATS = 128
local RAMP_SHARE = 0.85
local FLASH_WIDTH_BEATS = 0.75
local WASH_PEAK = 0.9
local CENTRE_PAN, TILT = 0.5, 0.6
local START_OFFSET = 0.25

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
        local depth = sf.lerp(0.1, 1, ramp)
        local wash = (base + (WASH_PEAK - base) * depth * flash) * gain
        local offset = sf.lerp(START_OFFSET, 0, ramp)
        return sf.scene({
            sf.colour(sf.group("wash"), on_the_one and palette.accent or palette.secondary),
            sf.dimmer(sf.group("wash"), wash),
            sf.colour(sf.group("strip"), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local fill = sf.lerp(1, count / 2, ramp)
                local from_end = math.min(index - 1, count - index)
                local lit = math.max(0, math.min(1, fill - from_end))
                return sf.dimmer(pixel, sf.lerp(0.3, 0.9, drive) * gain * lit)
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.dimmer(sf.group("movers"), sf.lerp(0.3, 0.8, drive) * gain),
            sf.each(sf.group("movers"), function(mover, index)
                return sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * offset, TILT)
            end)
        })
    end
}

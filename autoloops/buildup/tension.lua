-- 8-beat loop
-- energy: 3
-- family: cold
-- movement: sweep
local palette = sf.palette("techno")
local LOOP_BEATS = 8
local RAMP_BEATS = 128
local RAMP_SHARE = 0.85
local WASH_PEAK = 0.9
local CENTRE_PAN, TILT = 0.5, 0.6
local START_OFFSET, END_OFFSET = 0.2, 0.02
local LIT_PER_PULSE, SCATTER_STRIDE = 3, 3

local function period_for(ramp)
    if ramp < 1 / 3 then return 1 end
    if ramp < 2 / 3 then return 0.5 end
    return 0.25
end

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local ramp = math.min(ctx.autoloop_elapsed_beats / RAMP_BEATS, 1)
        local energy = ctx.normalized_rms_level or 0.6
        local drive = ramp * RAMP_SHARE + energy * (1 - RAMP_SHARE)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local period = period_for(ramp)
        local pulse = sf.on_beat(beat, period * 0.6, period)
        local pulse_index = math.floor(beat / period)
        local offset = sf.lerp(START_OFFSET, END_OFFSET, ramp) * sf.lerp(0.5, 1, energy)
        return sf.scene({
            sf.colour(sf.group("wash"), palette.cold),
            sf.dimmer(sf.group("wash"), sf.lerp(0.1, WASH_PEAK, drive) * gain),
            sf.colour(sf.group("strip"), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local slot = (index - 1 + pulse_index * SCATTER_STRIDE) % count
                local lit = slot < LIT_PER_PULSE and pulse or 0
                return sf.dimmer(pixel, sf.lerp(0.3, 0.9, drive) * gain * lit)
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.dimmer(sf.group("movers"), sf.lerp(0.2, 0.7, drive) * gain),
            sf.each(sf.group("movers"), function(mover, index)
                return sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * offset, TILT)
            end)
        })
    end
}

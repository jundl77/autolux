-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: mixed
local palette = sf.palette()
local LOOP_BEATS = 8
local RAMP_BEATS = 128
local RAMP_SHARE = 0.85
local WASH_FLOOR, WASH_PEAK = 0.06, 0.9
local CENTRE_PAN, TILT = 0.5, 0.6
local START_SWING, END_SWING = 0.2, 0.03

local function pulse_period_and_width(ramp)
    if ramp < 0.33 then return 4, 0.4 end
    if ramp < 0.66 then return 1, 0.4 end
    return 0.5, 0.25
end

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local ramp = math.min(ctx.autoloop_elapsed_beats / RAMP_BEATS, 1)
        local energy = sf.energy(ctx)
        local drive = ramp * RAMP_SHARE + energy * (1 - RAMP_SHARE)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local period, width = pulse_period_and_width(ramp)
        local pulse = sf.on_beat(beat, width, period)
        local pulse_index = math.floor(beat / period)
        local peak = sf.lerp(0.4, WASH_PEAK, drive)
        local swing = sf.lerp(START_SWING, END_SWING, ramp) * sf.lerp(0.5, 1, energy)
        local pan = swing * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        return sf.scene({
            sf.colour(sf.group("wash"), palette.secondary),
            sf.dimmer(sf.group("wash"), sf.lerp(WASH_FLOOR, peak, pulse) * gain),
            sf.colour(sf.group("strip"), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index)
                local lit = (index + pulse_index) % 2 == 0 and pulse or 0
                return sf.dimmer(pixel, sf.lerp(0.3, 0.9, drive) * gain * lit)
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.dimmer(sf.group("movers"), sf.lerp(0.3, 0.8, drive) * gain),
            sf.each(sf.group("movers"), function(mover, index)
                return sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * pan, TILT)
            end)
        })
    end
}

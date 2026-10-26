-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: mixed
local palette = sf.palette()
local LOOP_BEATS = 8
local RAMP_BEATS = 128
local RAMP_SHARE = 0.85
local WASH_PEAK = 0.9
local CENTRE_PAN, TILT, SWING = 0.5, 0.55, 0.18

local function laps_for(ramp)
    if ramp < 1 / 3 then return 1 end
    if ramp < 2 / 3 then return 2 end
    return 4
end

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local ramp = math.min(ctx.autoloop_elapsed_beats / RAMP_BEATS, 1)
        local energy = ctx.normalized_rms_level or 0.6
        local drive = ramp * RAMP_SHARE + energy * (1 - RAMP_SHARE)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local peak = sf.lerp(0.35, WASH_PEAK, drive)
        local depth = sf.lerp(0.1, 0.85, ramp)
        local wash = peak * (1 - depth * (1 - sf.on_beat(beat, 0.5))) * gain
        local period = LOOP_BEATS / laps_for(ramp)
        local pan = SWING * sf.lerp(0.5, 1, energy) * sf.osc.sine(beat, period, -1, 1)
        return sf.scene({
            sf.colour(sf.group("wash"), palette.primary),
            sf.dimmer(sf.group("wash"), wash),
            sf.colour(sf.group("strip"), palette.secondary),
            sf.dimmer(sf.group("strip"), sf.lerp(0.1, 0.5, drive) * gain),
            sf.colour(sf.group("movers"), palette.cold),
            sf.dimmer(sf.group("movers"), sf.lerp(0.3, 0.85, drive) * gain),
            sf.each(sf.group("movers"), function(mover, index)
                return sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * pan, TILT)
            end)
        })
    end
}

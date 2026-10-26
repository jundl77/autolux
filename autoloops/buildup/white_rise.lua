-- 8-beat loop
-- energy: 2
-- family: cold
-- movement: sweep
local palette = sf.palette("techno")
local LOOP_BEATS = 8
local RAMP_BEATS = 128
local RAMP_SHARE = 0.85
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
        local colour = sf.mix(palette.secondary, palette.cold, ramp)
        local depth = sf.lerp(0.1, 0.85, ramp)
        local function pulsed(peak, at)
            return peak * (1 - depth * (1 - sf.on_beat(at, 0.5))) * gain
        end
        local offset = sf.lerp(START_OFFSET, 0, ramp)
        return sf.scene({
            sf.colour(sf.group("wash"), colour),
            sf.dimmer(sf.group("wash"), pulsed(sf.lerp(0.35, WASH_PEAK, drive), beat)),
            sf.colour(sf.group("strip"), colour),
            sf.dimmer(sf.group("strip"), pulsed(sf.lerp(0.3, 0.9, drive), beat + 0.5)),
            sf.colour(sf.group("movers"), colour),
            sf.dimmer(sf.group("movers"), sf.lerp(0.3, 0.8, drive) * gain),
            sf.each(sf.group("movers"), function(mover, index)
                return sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * offset, TILT)
            end)
        })
    end
}

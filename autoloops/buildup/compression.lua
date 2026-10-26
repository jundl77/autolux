-- 8-beat loop; time since selection ramps to a plateau over 32 bars, RMS adds the top 15%.
-- energy: 3
-- family: warm
-- movement: sweep
-- wash: primary, a pulse on every beat whose decay tightens from a full beat to 0.2 beats.
-- strip: secondary, held low, the steady floor under the squeeze.
-- movers: cold, mirrored, slow sweep narrowing toward centre.
-- Palette slots: primary, secondary, cold.
local palette = sf.palette("techno")
local LOOP_BEATS = 8
local RAMP_BEATS = 128
local RAMP_SHARE = 0.85
local WASH_FLOOR, WASH_PEAK = 0.05, 0.9
local START_WIDTH, END_WIDTH = 1.0, 0.2
local STRIP_LEVEL = 0.2
local CENTRE_PAN, TILT = 0.5, 0.6
local START_SWING, END_SWING = 0.18, 0.04

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local ramp = math.min(ctx.autoloop_elapsed_beats / RAMP_BEATS, 1)
        local energy = ctx.normalized_rms_level or 0.6
        local drive = ramp * RAMP_SHARE + energy * (1 - RAMP_SHARE)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local pulse = sf.on_beat(beat, sf.lerp(START_WIDTH, END_WIDTH, ramp))
        local peak = sf.lerp(0.4, WASH_PEAK, drive)
        local swing = sf.lerp(START_SWING, END_SWING, ramp) * sf.lerp(0.5, 1, energy)
        local pan = swing * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        return sf.scene({
            sf.colour(sf.group("wash"), palette.primary),
            sf.dimmer(sf.group("wash"), sf.lerp(WASH_FLOOR, peak, pulse) * gain),
            sf.colour(sf.group("strip"), palette.secondary),
            sf.dimmer(sf.group("strip"), STRIP_LEVEL * gain),
            sf.colour(sf.group("movers"), palette.cold),
            sf.dimmer(sf.group("movers"), sf.lerp(0.25, 0.75, drive) * gain),
            sf.each(sf.group("movers"), function(mover, index)
                return sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * pan, TILT)
            end)
        })
    end
}

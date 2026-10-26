-- 8-beat loop; time since selection lifts intensity to an 85% plateau over 32 bars,
-- energy: 2
-- family: warm
-- movement: sweep
-- measured RMS supplies the last 15%. No strobe: that is reserved for the drop.
-- wash: secondary, beat pulses that deepen with the ramp.
-- strip: primary rising chase, doubling speed every 8 bars.
-- movers: cold, mirrored sweep tightening toward centre.
-- Palette slots: primary, secondary, cold.
local palette = sf.palette("techno")
local LOOP_BEATS = 8
local RAMP_BEATS = 128
local RAMP_SHARE = 0.85
local CHASE_DOUBLING_BEATS = 32
local CHASE_TAIL_PIXELS = 2
local CENTRE_PAN, TILT = 0.5, 0.6
local START_SWING, END_SWING = 0.2, 0.02

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local elapsed = ctx.autoloop_elapsed_beats
        local ramp = math.min(elapsed / RAMP_BEATS, 1)
        local energy = ctx.normalized_rms_level or 0.6
        -- Time alone stops short of full; only a loud track reaches the top 15%.
        local drive = ramp * RAMP_SHARE + energy * (1 - RAMP_SHARE)
        -- Applied to the final levels, not just drive, so the lerp floors also fade in silence.
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local depth = sf.lerp(0.2, 0.9, ramp)
        local wash = sf.lerp(0.35, 1, drive) * gain * (1 - depth * (1 - sf.on_beat(beat, 0.5)))
        -- Whole laps per loop keep the chase continuous across the loop wrap.
        local laps = 2 ^ math.min(3, math.floor(elapsed / CHASE_DOUBLING_BEATS))
        local head = beat / LOOP_BEATS * laps % 1
        local swing = sf.lerp(START_SWING, END_SWING, ramp) * sf.lerp(0.5, 1, energy)
        local pan = swing * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        return sf.scene({
            sf.colour(sf.group("wash"), palette.secondary),
            sf.dimmer(sf.group("wash"), wash),
            sf.colour(sf.group("strip"), palette.primary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local behind = (head * count - (index - 1)) % count
                local lit = math.max(0, 1 - behind / CHASE_TAIL_PIXELS)
                return sf.dimmer(pixel, sf.lerp(0.3, 1, drive) * gain * lit)
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.dimmer(sf.group("movers"), sf.lerp(0.3, 0.8, drive) * gain),
            sf.each(sf.group("movers"), function(mover, index)
                local side = index % 2 == 1 and -1 or 1
                return sf.pan_tilt(mover, CENTRE_PAN + side * pan, TILT)
            end)
        })
    end
}

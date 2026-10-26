-- 16-beat loop, no beat pulses or strobe.
-- energy: 1
-- family: warm
-- movement: still
-- wash: secondary, one slow breath per loop up to 0.2.
-- strip: off.
-- movers: primary, still, both on centre. Palette slots: primary, secondary, black.
local palette = sf.palette("techno")
local LOOP_BEATS = 16
local WASH_LOW, WASH_HIGH = 0.08, 0.2
local MOVER_LEVEL = 0.35
local CENTRE_PAN, TILT = 0.5, 0.6

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.secondary),
            sf.dimmer(sf.group("wash"), sf.osc.sine(beat, LOOP_BEATS, WASH_LOW, WASH_HIGH) * level),
            sf.colour(sf.group("strip"), palette.black),
            sf.dimmer(sf.group("strip"), 0),
            sf.colour(sf.group("movers"), palette.primary),
            sf.pan_tilt(sf.group("movers"), CENTRE_PAN, TILT),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * level)
        }
    end
}

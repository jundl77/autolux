-- 16-beat loop
-- energy: 1
-- family: accent
-- movement: still
local palette = sf.palette()
local LOOP_BEATS = 16
local DROP_BEATS = 2
local SCATTER = { 3, 6, 1, 8, 4, 7, 2, 5 }
local DROP_PEAK = 0.4
local WASH_LEVEL = 0.05
local MOVER_LEVEL = 0.15
local CENTRE_PAN, TILT = 0.5, 0.6

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local lit = SCATTER[math.floor(beat / DROP_BEATS) % #SCATTER + 1]
        local decay = 1 - (beat % DROP_BEATS) / DROP_BEATS
        return sf.scene {
            sf.colour(sf.all(), palette.accent),
            sf.dimmer(sf.group("wash"), WASH_LEVEL * level),
            sf.each(sf.group("strip"), function(pixel, index)
                return sf.dimmer(pixel, index == lit and DROP_PEAK * decay * decay * level or 0)
            end),
            sf.pan_tilt(sf.group("movers"), CENTRE_PAN, TILT),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * level)
        }
    end
}

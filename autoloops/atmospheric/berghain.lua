-- 16-beat loop
-- energy: 1
-- family: cold
-- movement: sweep
local palette = sf.palette()
local LOOP_BEATS = 16
local WASH_LEVEL = 0.12
local EDGE_LEVEL = 0.08
local MOVER_LEVEL = 0.3
local CENTRE_PAN, PARK_PAN = 0.5, 0.12
local CENTRE_TILT, TILT_SWING = 0.6, 0.08

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local tilt = CENTRE_TILT + TILT_SWING * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        return sf.scene {
            sf.colour(sf.all(), palette.cold),
            sf.dimmer(sf.group("wash"), WASH_LEVEL * level),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local edge = (index == 1 or index == count) and EDGE_LEVEL or 0
                return sf.dimmer(pixel, edge * level)
            end),
            sf.each(sf.group("movers"), function(mover, index)
                return {
                    sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * PARK_PAN, tilt),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

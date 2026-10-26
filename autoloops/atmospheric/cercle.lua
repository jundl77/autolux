-- 16-beat loop
-- energy: 1
-- family: accent
-- movement: still
local palette = sf.palette()
local LOOP_BEATS = 16
local WASH_LEVEL = 0.16
local HORIZON_LEVEL = 0.22
local MOVER_LEVEL = 0.25
local CENTRE_PAN, PARK_PAN, TILT = 0.5, 0.3, 0.58

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local dusk = sf.osc.sine(beat, LOOP_BEATS)
        return sf.scene {
            sf.colour(sf.group("wash"), sf.mix(palette.secondary, palette.accent, dusk)),
            sf.dimmer(sf.group("wash"), WASH_LEVEL * level),
            sf.colour(sf.group("strip"), palette.secondary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local from_centre = math.abs(2 * (index - 1) / (count - 1) - 1)
                local band = math.max(0, 1 - 3 * math.abs(from_centre - dusk))
                return sf.dimmer(pixel, HORIZON_LEVEL * band * level)
            end),
            sf.colour(sf.group("movers"), palette.secondary),
            sf.each(sf.group("movers"), function(mover, index)
                return {
                    sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * PARK_PAN, TILT),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

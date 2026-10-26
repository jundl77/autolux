-- 16-beat loop
-- energy: 1
-- family: cold
-- movement: sweep
local palette = sf.palette()
local LOOP_BEATS = 16
local WASH_LOW, WASH_HIGH = 0.06, 0.2
local GLOW_LEVEL = 0.25
local MOVER_LEVEL = 0.25
local CENTRE_PAN, PARK_PAN = 0.5, 0.2
local TILT_LOW, TILT_HIGH = 0.55, 0.62

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local sun = sf.osc.triangle(sf.phrase_phase(beat, LOOP_BEATS / 4), 1)
        return sf.scene {
            sf.colour(sf.group("wash"), sf.mix(palette.secondary, palette.cold, sun)),
            sf.dimmer(sf.group("wash"), sf.lerp(WASH_LOW, WASH_HIGH, sun) * level),
            sf.colour(sf.group("strip"), palette.secondary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local depth = 1 - math.abs(2 * (index - 1) / (count - 1) - 1)
                local glow = math.max(0, math.min(1, 1.5 * sun + 0.5 - 1.5 * depth))
                return sf.dimmer(pixel, GLOW_LEVEL * glow * level)
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.each(sf.group("movers"), function(mover, index)
                local tilt = sf.lerp(TILT_LOW, TILT_HIGH, sun)
                return {
                    sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * PARK_PAN, tilt),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

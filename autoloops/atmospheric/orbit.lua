-- 16-beat loop
-- energy: 1
-- family: cold
-- movement: sweep
local palette = sf.palette("techno")
local LOOP_BEATS = 16
local WASH_LEVEL = 0.15
local MOVER_LEVEL = 0.32
local CENTRE_PAN, PARK_PAN = 0.5, 0.15
local CENTRE_TILT, RADIUS = 0.6, 0.05

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local angle = 2 * math.pi * beat / LOOP_BEATS
        return sf.scene {
            sf.colour(sf.group("wash"), palette.cold),
            sf.dimmer(sf.group("wash"), WASH_LEVEL * level),
            sf.colour(sf.group("strip"), palette.black),
            sf.dimmer(sf.group("strip"), 0),
            sf.colour(sf.group("movers"), palette.cold),
            sf.each(sf.group("movers"), function(mover, index)
                local pan = CENTRE_PAN + sf.side(index) * (PARK_PAN + RADIUS * math.sin(angle))
                return {
                    sf.pan_tilt(mover, pan, CENTRE_TILT + RADIUS * math.cos(angle)),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

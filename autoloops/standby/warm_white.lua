-- 16-beat loop
local palette = sf.palette("techno")
local WASH_LEVEL = 0.12
local STRIP_LEVEL = 0.04
local PARK_PAN, PARK_TILT = 0.5, 0.6

return {
    render = function()
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.dimmer(sf.group("wash"), WASH_LEVEL),
            sf.dimmer(sf.group("strip"), STRIP_LEVEL),
            sf.pan_tilt(sf.group("movers"), PARK_PAN, PARK_TILT),
            sf.dimmer(sf.group("movers"), 0)
        }
    end
}

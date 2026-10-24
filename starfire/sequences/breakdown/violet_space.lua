local WASH_LEVEL = 0.10
local MOVER_LEVEL = 0.12
local VIOLET_HUE = 275
local SATURATION = 0.85
local REST_PAN = 0.50
local REST_TILT = 0.42

return {
    render = function(ctx)
        return sf.scene {
            sf.colour(sf.all(), sf.hsv(VIOLET_HUE, SATURATION, 1)),
            sf.dimmer(sf.group("wash"), WASH_LEVEL),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL),
            sf.pan_tilt(sf.group("movers"), REST_PAN, REST_TILT)
        }
    end
}

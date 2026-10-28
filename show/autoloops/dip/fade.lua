-- Breakdown entry: the room settles to a low glow instead of cutting to black.
-- strobe: no
-- intensity: ltp
local GLOW = 0.15

return {
    render = function()
        return sf.scene {
            sf.dimmer(sf.group("wash"), GLOW),
            sf.dimmer(sf.group("strip"), GLOW),
            sf.dimmer(sf.group("movers"), GLOW)
        }
    end
}

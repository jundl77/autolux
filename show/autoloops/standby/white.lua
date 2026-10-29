-- 16-beat loop
local WASH_LEVEL = 0.12
local STRIP_LEVEL = 0.04

return {
    render = function(ctx)
        return sf.scene {
            sf.colour(sf.all(), sf.white()),
            sf.dimmer(sf.group("wash"), WASH_LEVEL),
            sf.dimmer(sf.group("strip"), STRIP_LEVEL),
            sf.phaser(sf.group("movers"), ctx, { shape = "fan", size = 0 }),
            sf.dimmer(sf.group("movers"), 0)
        }
    end
}

-- The blackout before the drop: every loop group dark, so the one lands out of nothing.
-- strobe: no
-- intensity: ltp
return {
    render = function()
        return sf.scene {
            sf.dimmer(sf.group("wash"), 0),
            sf.dimmer(sf.group("strip"), 0),
            sf.dimmer(sf.group("movers"), 0)
        }
    end
}

-- The Wild drop-entry strobe: every loop group flashes white at a beat division of the tempo.
-- strobe: yes
-- intensity: htp
-- movement: still
-- texture: mixed
local FLASHES_PER_BEAT = 4

return {
    render = function(ctx)
        local hz = sf.beat_strobe_hz(ctx, FLASHES_PER_BEAT)
        local effects = {}
        for _, name in ipairs({ "wash", "strip", "movers" }) do
            local group = sf.group(name)
            effects[#effects + 1] = {
                sf.colour(group, sf.white()), sf.dimmer(group, 1), sf.strobe(group, hz)
            }
        end
        return sf.scene(effects)
    end
}

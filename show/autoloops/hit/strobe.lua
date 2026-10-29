-- The Wild drop-entry strobe: every loop group flashes white at a beat division of the tempo.
-- strobe: yes
-- intensity: htp
-- movement: still
-- texture: mixed
local MAX_STROBE_HZ = 10
local DIVISIONS = { 4, 2, 1 }

-- The fastest beat division under the cap keeps the flashes on the grid at any tempo.
local function strobe_hz(bpm)
    for _, division in ipairs(DIVISIONS) do
        local hz = bpm / 60 * division
        if hz <= MAX_STROBE_HZ then return hz end
    end
    return MAX_STROBE_HZ
end

return {
    render = function(ctx)
        local hz = strobe_hz(ctx.bpm)
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

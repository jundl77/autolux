-- The Wild riser: over its last eight beats white rises to full by the dip, strobing faster.
-- strobe: yes
-- intensity: htp
-- movement: still
-- texture: mixed
local STROBE_BEATS = 8

-- Held back to the last eight beats so the build's own finale shows first; there the rate
-- steps on beat lines through half, one, two and four flashes a beat, as fast as the engine
-- lets through at this intensity.
local function strobe_hz(ctx, remaining)
    if remaining > STROBE_BEATS then return 0 end
    return sf.beat_strobe_hz(ctx, 2 ^ (math.floor((STROBE_BEATS - remaining) / 2) - 1))
end

return {
    render = function(ctx)
        local _, left = sf.overlay_progress(ctx)
        local hz = strobe_hz(ctx, math.ceil(left))
        local rising = math.max(0, math.min(1, 1 - left / STROBE_BEATS))
        local level = 1 - (1 - rising) ^ 2
        local effects = {}
        for _, name in ipairs({ "wash", "strip", "movers" }) do
            local group = sf.group(name)
            effects[#effects + 1] = {
                sf.colour(group, sf.white()), sf.dimmer(group, level), sf.strobe(group, hz)
            }
        end
        return sf.scene(effects)
    end
}

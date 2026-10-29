-- The Wild riser: white rising to full by the dip, strobing faster each pair of beats.
-- strobe: yes
-- intensity: htp
-- movement: still
-- texture: mixed
local MAX_STROBE_HZ = 10
local STROBE_BEATS = 8

-- Over the last eight beats the rate steps on beat lines through half, one, two and four
-- flashes a beat, never past the cap. The level eases out so it clears a loud build early.
local function strobe_hz(ctx, remaining)
    if remaining > STROBE_BEATS then return 0 end
    local division = 2 ^ (math.floor((STROBE_BEATS - remaining) / 2) - 1)
    while division > 0.5 and ctx.bpm / 60 * division > MAX_STROBE_HZ do division = division / 2 end
    return ctx.bpm / 60 * division
end

return {
    render = function(ctx)
        local progress, left = sf.overlay_progress(ctx)
        local hz = strobe_hz(ctx, math.ceil(left))
        local level = 1 - (1 - progress) ^ 2
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

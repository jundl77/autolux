-- The Wild riser: a white wash rising over the pre-roll, strobing faster each pair of beats.
-- strobe: yes
-- intensity: htp
local MAX_STROBE_HZ = 10
local STROBE_BEATS = 8
local PEAK = 0.6

-- Over the last eight beats the rate steps on beat lines through half, one, two and four
-- flashes a beat, never past the cap, so the ramp stays on the grid at any tempo.
local function strobe_hz(ctx, remaining)
    if remaining > STROBE_BEATS then return 0 end
    local division = 2 ^ (math.floor((STROBE_BEATS - remaining) / 2) - 1)
    while division > 0.5 and ctx.bpm / 60 * division > MAX_STROBE_HZ do division = division / 2 end
    return ctx.bpm / 60 * division
end

return {
    render = function(ctx)
        local length = ctx.overlay_length_beats or 1
        local progress = math.min(1, ctx.autoloop_elapsed_beats / length)
        local remaining = math.ceil(length - ctx.autoloop_elapsed_beats)
        local hz = strobe_hz(ctx, remaining)
        local effects = {}
        for _, name in ipairs({ "wash", "strip", "movers" }) do
            local group = sf.group(name)
            effects[#effects + 1] = { sf.colour(group, sf.white()), sf.dimmer(group, PEAK * progress), sf.strobe(group, hz) }
        end
        return sf.scene(effects)
    end
}

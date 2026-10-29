-- The Wild riser: over its last eight beats strip and beams rise in white to full by the dip, strobing faster.
-- strobe: yes
-- intensity: htp
-- movement: still
-- texture: mixed
local STROBE_BEATS = 8
local BEAM_AND_STRIP = { "strip", "movers" }

-- Flashes per beat double every two beats: 0.5, 1, 2, 4, then 8 at the end.
local function strobe_hz(ctx, remaining)
    if remaining > STROBE_BEATS then return 0 end
    return sf.beat_strobe_hz(ctx, 2 ^ (math.floor((STROBE_BEATS - remaining) / 2) - 1))
end

return {
    render = function(ctx)
        local _, left = sf.overlay_progress(ctx)
        local rising = math.max(0, math.min(1, 1 - left / STROBE_BEATS))
        return sf.white_strobe(1 - (1 - rising) ^ 2, strobe_hz(ctx, math.ceil(left)), BEAM_AND_STRIP)
    end
}

-- The Wild drop-entry strobe: every loop group flashes white at a beat division of the tempo.
-- strobe: yes
-- intensity: htp
-- movement: still
-- texture: mixed
local FLASHES_PER_BEAT = 4

return {
    render = function(ctx)
        return sf.white_strobe(1, sf.beat_strobe_hz(ctx, FLASHES_PER_BEAT))
    end
}

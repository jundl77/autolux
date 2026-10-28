-- Every beam snaps onto one point with the open gobo in white, decaying over the hit.
-- strobe: no
-- intensity: htp
local OPEN_SLOT = 0

-- The movers' shared point, so the punch is one spot whatever each head's own focus.
local function shared_focus(group)
    local members, x, y, z = sf.members(group), 0, 0, 0
    for _, cell in ipairs(members) do
        local fx, fy, fz = sf.focus(cell)
        x, y, z = x + fx, y + fy, z + fz
    end
    local count = math.max(1, #members)
    return x / count, y / count, z / count
end

return {
    render = function(ctx)
        local progress = math.min(1, ctx.autoloop_elapsed_beats / (ctx.overlay_length_beats or 1))
        local movers = sf.group("movers")
        local x, y, z = shared_focus(movers)
        return sf.scene {
            sf.colour(movers, sf.white()),
            sf.gobo(movers, OPEN_SLOT),
            sf.dimmer(movers, (1 - progress) ^ 2),
            sf.each(movers, function(mover)
                local pan, tilt = sf.aim(mover, x, y, z)
                if pan ~= nil then return sf.pan_tilt(mover, pan, tilt) end
            end)
        }
    end
}

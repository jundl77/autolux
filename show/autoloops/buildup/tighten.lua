-- 8-beat loop
-- energy: 3
-- movement: sweep
-- texture: beam
local palette = sf.palette()
local PLATEAU_BEATS, FINALE_BEATS, EASE_BEATS = 64, 16, 8
local FAN_DEGREES, FAN_MEASURE = 30, 8
local WASH_PEAK, STRIP_PEAK, MOVER_PEAK = 0.9, 0.9, 0.85
local DOT_WIDTH = 1.5

-- One amount, converge, closes the fan, walks the strip's two dots to the centre and pulls
-- the washes inwards; regime U reaches half of it, a cued drop's finale the rest.
local function drive_and_converge(ctx)
    local tension = sf.tension(ctx, PLATEAU_BEATS)
    local drive, converge = 0.15 + 0.55 * tension, 0.5 * tension
    if ctx.next_intent == "drop" then
        local finale = sf.finale(ctx, FINALE_BEATS)
        return drive + (1 - drive) * finale, converge + (1 - converge) * finale
    end
    local ease = ctx.next_intent and sf.finale(ctx, EASE_BEATS) or 0
    return drive * (1 - 0.5 * ease), converge
end

-- The movers' shared point, so a closed fan is one spot whatever each head's own focus.
local function shared_focus(group)
    local members, x, y, z = sf.members(group), 0, 0, 0
    if #members == 0 then return nil end
    for _, cell in ipairs(members) do
        local fx, fy, fz = sf.focus(cell)
        x, y, z = x + fx, y + fy, z + fz
    end
    return { x / #members, y / #members, z / #members }
end

local function from_centre(index, count)
    return count > 1 and math.abs(2 * (index - 1) / (count - 1) - 1) or 0
end

return {
    render = function(ctx)
        local drive, converge = drive_and_converge(ctx)
        local elapsed = ctx.section_elapsed_beats or ctx.autoloop_elapsed_beats
        local opening = (1 - converge) * sf.osc.sine(elapsed, FAN_MEASURE, 1, 0)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local movers = sf.group("movers")
        return sf.scene {
            sf.colour(sf.all(), palette.primary),
            sf.each(sf.group("wash"), function(wash, index, count)
                local inward = 1 - converge * from_centre(index, count)
                return sf.dimmer(wash, WASH_PEAK * drive * inward * gain)
            end),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local distance = math.abs(from_centre(index, count) - opening) * (count - 1) / 2
                local dot = math.max(0, 1 - distance / DOT_WIDTH)
                return sf.dimmer(pixel, STRIP_PEAK * dot * sf.lerp(0.4, 1, drive) * gain)
            end),
            sf.dimmer(movers, MOVER_PEAK * sf.lerp(0.5, 1, drive) * gain),
            sf.phaser(movers, ctx, { shape = "fan", size = FAN_DEGREES * opening, at = shared_focus(movers) })
        }
    end
}

-- 16-beat loop
-- energy: 1
-- movement: sweep
-- texture: wash
-- A soft peak crosses each group house left to house right (beats 0-8), then closes in from
-- both sides at once (beats 8-16). sf.position gives each member's X, normalised to the
-- group's own extent so the same travel fits 1.8 m of washes or a 40 m truss; sf.mirror pairs
-- members by position for the closing half; sf.side splits the movers' pan. Cells sharing one
-- position (one fixture's pixels) travel in geometry order; a lone fixture holds still.
local palette = sf.palette()
local LOOP_BEATS, HALF_BEATS = 16, 8
local WIDTH = 0.35
local CENTRE_PAN, TILT, PAN_SWING = 0.5, 0.6, 0.06

local function placed(group)
    local members = sf.members(group)
    local low, high = math.huge, -math.huge
    for _, cell in ipairs(members) do
        local x = sf.position(cell)
        low, high = math.min(low, x), math.max(high, x)
    end
    local along, reach = {}, {}
    for index, cell in ipairs(members) do
        if high > low then
            along[cell] = (sf.position(cell) - low) / (high - low)
        elseif #members > 1 then
            along[cell] = (index - 1) / (#members - 1)
        end
    end
    for _, pair in ipairs(sf.mirror(group)) do
        local left, right = along[pair[1].fixture], along[pair[2].fixture]
        if left and right then
            local outer = math.max(math.abs(2 * left - 1), math.abs(2 * right - 1))
            reach[pair[1].fixture], reach[pair[2].fixture] = outer, outer
        end
    end
    return { target = group, members = members, along = along, reach = reach }
end

local wash, strip, movers = placed(sf.group("wash")), placed(sf.group("strip")), placed(sf.group("movers"))

local function bump(offset)
    if math.abs(offset) >= WIDTH then return 0 end
    return (1 + math.cos(math.pi * offset / WIDTH)) / 2
end

local function travelling(group, colour, floor, peak, beat, level)
    local closing = beat >= HALF_BEATS
    local front = sf.lerp(-WIDTH, 1 + WIDTH, (beat % HALF_BEATS) / HALF_BEATS)
    local effects = { sf.colour(group.target, colour) }
    for _, cell in ipairs(group.members) do
        local spot = closing and group.reach[cell] and 1 - group.reach[cell] or group.along[cell]
        local glow = spot and bump(spot - front) or 0
        effects[#effects + 1] = sf.dimmer(sf.fixture(cell), (floor + (peak - floor) * glow) * level)
    end
    return effects
end

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local swing = PAN_SWING * sf.lerp(0.5, 1, energy) * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        return sf.scene {
            travelling(wash, palette.secondary, 0.06, 0.5, beat, level),
            travelling(strip, palette.cold, 0.02, 0.25, beat, level),
            travelling(movers, palette.cold, 0.04, 0.3, beat, level),
            sf.each(sf.group("movers"), function(mover, index, count)
                return sf.pan_tilt(mover, CENTRE_PAN + sf.side(index, count) * swing, TILT)
            end)
        }
    end
}

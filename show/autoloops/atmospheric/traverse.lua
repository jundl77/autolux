-- 16-beat loop
-- energy: 1
-- movement: sweep
-- texture: wash
-- X is normalised to each group's own extent, so one travel fits 1.8 m of washes or a 40 m truss.
-- Members sharing one X travel in geometry order; a lone member sits mid-group; an empty group is skipped.
-- Only glowing cells get their own dimmer (under 70 % of a group), so 512 cells stay inside the effect cap.
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
        else
            along[cell] = 0.5
        end
    end
    for _, pair in ipairs(sf.mirror(group)) do
        local left, right = along[pair[1].fixture], along[pair[2].fixture]
        local outer = math.max(math.abs(2 * left - 1), math.abs(2 * right - 1))
        reach[pair[1].fixture], reach[pair[2].fixture] = outer, outer
    end
    return { target = group, members = members, along = along, reach = reach }
end

local wash, strip, movers = placed(sf.group("wash")), placed(sf.group("strip")), placed(sf.group("movers"))

local function bump(offset)
    if math.abs(offset) >= WIDTH then return 0 end
    return (1 + math.cos(math.pi * offset / WIDTH)) / 2
end

local function travelling(group, colour, floor, peak, beat, loudness)
    local closing = beat >= HALF_BEATS
    local front = sf.lerp(-WIDTH, 1 + WIDTH, (beat % HALF_BEATS) / HALF_BEATS)
    local effects = { sf.colour(group.target, colour), sf.dimmer(group.target, floor * loudness) }
    for _, cell in ipairs(group.members) do
        local spot = closing and 1 - group.reach[cell] or group.along[cell]
        local glow = bump(spot - front)
        if glow > 0 then
            effects[#effects + 1] = sf.dimmer(sf.fixture(cell), (floor + (peak - floor) * glow) * loudness)
        end
    end
    return effects
end

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local loudness = sf.loudness_gain(ctx.normalized_rms_level)
        local swing = PAN_SWING * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        local pans = {}
        for _, mover in ipairs(movers.members) do
            local along = movers.along[mover]
            local side = along < 0.5 and -1 or along > 0.5 and 1 or 0
            pans[#pans + 1] = sf.pan_tilt(sf.fixture(mover), CENTRE_PAN + side * swing, TILT)
        end
        return sf.scene {
            travelling(wash, palette.secondary, 0.06, 0.4, beat, loudness),
            travelling(strip, palette.cold, 0.02, 0.25, beat, loudness),
            travelling(movers, palette.cold, 0.04, 0.3, beat, loudness),
            pans
        }
    end
}

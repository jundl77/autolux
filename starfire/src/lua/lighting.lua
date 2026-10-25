local sf = { osc = {} }
local FULL_TURN = 2 * math.pi
local HUE_SECTOR_DEGREES = 60
local HUE_CIRCLE_DEGREES = 360

local function finite(value)
    return type(value) == "number" and value == value and math.abs(value) < math.huge
end

local function phase(beats, period_beats, offset)
    assert(finite(beats), "oscillator beat position must be finite")
    assert(finite(period_beats) and period_beats > 0, "oscillator period must be positive and finite")
    offset = offset or 0
    assert(finite(offset), "oscillator phase offset must be finite")
    return (beats / period_beats + offset) % 1
end

function sf.lerp(first, last, amount)
    return first + (last - first) * amount
end

function sf.osc.sine(beats, period_beats, low, high, offset)
    local amount = (1 - math.cos(FULL_TURN * phase(beats, period_beats, offset))) / 2
    return sf.lerp(low or 0, high or 1, amount)
end

function sf.osc.triangle(beats, period_beats, low, high, offset)
    local amount = 1 - math.abs(2 * phase(beats, period_beats, offset) - 1)
    return sf.lerp(low or 0, high or 1, amount)
end

function sf.osc.pulse(beats, period_beats, low, high, duty, offset)
    duty = duty or 0.5
    assert(finite(duty) and duty >= 0 and duty <= 1, "pulse duty must be within 0..1")
    if phase(beats, period_beats, offset) < duty then return high or 1 end
    return low or 0
end

function sf.rgb(red, green, blue)
    return { red = red, green = green, blue = blue }
end

function sf.hsv(hue_degrees, saturation, value)
    assert(finite(hue_degrees), "hue must be finite")
    assert(finite(saturation) and saturation >= 0 and saturation <= 1, "saturation must be within 0..1")
    assert(finite(value) and value >= 0 and value <= 1, "value must be within 0..1")
    local hue = (hue_degrees % HUE_CIRCLE_DEGREES) / HUE_SECTOR_DEGREES
    local chroma = value * saturation
    local intermediate = chroma * (1 - math.abs(hue % 2 - 1))
    local minimum = value - chroma
    local red, green, blue
    if hue < 1 then red, green, blue = chroma, intermediate, 0
    elseif hue < 2 then red, green, blue = intermediate, chroma, 0
    elseif hue < 3 then red, green, blue = 0, chroma, intermediate
    elseif hue < 4 then red, green, blue = 0, intermediate, chroma
    elseif hue < 5 then red, green, blue = intermediate, 0, chroma
    else red, green, blue = chroma, 0, intermediate end
    return sf.rgb(red + minimum, green + minimum, blue + minimum)
end

function sf.group(name) return { group = name } end
function sf.fixture(name) return { fixture = name } end
function sf.all() return "all" end

function sf.dimmer(target, level)
    return { target = target, values = { intensity = level } }
end

function sf.colour(target, colour)
    return { target = target, values = { colour = colour } }
end

function sf.pan_tilt(target, pan, tilt)
    return { target = target, values = { pan = pan, tilt = tilt } }
end

function sf.strobe(target, rate_hz)
    return { target = target, values = { strobe_hz = rate_hz } }
end

function sf.physical_attribute(target, attribute, physical_value)
    assert(type(attribute) == "string", "GDTF attribute name must be a string")
    assert(finite(physical_value), "GDTF physical value must be finite")
    return { target = target, values = { attributes = { [attribute] = physical_value } } }
end

function sf.channel_preset(target, attribute, preset)
    assert(type(attribute) == "string" and type(preset) == "string", "GDTF attribute and preset names must be strings")
    return { target = target, values = { attributes = { [attribute] = preset } } }
end

function sf.scene(effects)
    local result = {}
    local function append(item)
        if item.target ~= nil then
            result[#result + 1] = item
        else
            for _, effect in ipairs(item) do append(effect) end
        end
    end
    for _, effect in ipairs(effects) do append(effect) end
    return result
end

function sf.each(target, make_effect)
    local result = {}
    local members = sf.members(target)
    for index, name in ipairs(members) do
        result[#result + 1] = make_effect(sf.fixture(name), index, #members)
    end
    return sf.scene(result)
end

return sf

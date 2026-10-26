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

-- Blend between two palette slots, so crossfades stay inside the palette.
function sf.mix(first, last, amount)
    return sf.rgb(sf.lerp(first.red, last.red, amount), sf.lerp(first.green, last.green, amount),
        sf.lerp(first.blue, last.blue, amount))
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

local BEATS_PER_BAR = 4

-- Grid helpers take `ctx` (preferred: Starfire's bar fields from the tracker's phase)
-- or a beat count, which they read as 4/4 with beat 0 on "the one". Starfire anchors
-- ctx.beat_position so both agree, but a loop-local count (beat % 16) has no bar size.
function sf.beat_in_bar(beats)
    if type(beats) == "table" then return beats.bar_phase end
    return beats % BEATS_PER_BAR
end

function sf.bar(beats)
    if type(beats) == "table" then return beats.bar_index end
    return math.floor(beats / BEATS_PER_BAR)
end

function sf.phrase_phase(beats, bars)
    assert(finite(bars) and bars > 0, "phrase length must be positive and finite")
    if type(beats) == "table" then
        return ((beats.bar_index % bars) + beats.bar_phase / beats.beats_per_bar) / bars % 1
    end
    return (beats / (bars * BEATS_PER_BAR)) % 1
end

-- Kick envelope: 1 on each grid edge (every `period` beats, default 1), squared decay
-- to 0 after `width` beats. A period of 0.5 gives eighth notes.
function sf.on_beat(beats, width, period)
    assert(finite(width) and width > 0, "beat envelope width must be positive and finite")
    period = period or 1
    assert(finite(period) and period > 0, "beat envelope period must be positive and finite")
    if type(beats) == "table" then beats = beats.beat_position end
    local remaining = math.max(0, 1 - (beats % period) / width)
    return remaining * remaining
end

-- Mirror sign for paired fixtures: odd members (left) -1, even members (right) +1.
function sf.side(index) return index % 2 == 1 and -1 or 1 end

-- RMS is normalised over -48..-12 dBFS, so a quiet room or a stopped track reads ~0.
-- The 0.08 floor keeps the rig barely alive instead of black; full level from rms 0.8.
-- nil means no measurement yet, treated as a typical 0.6.
local LOUDNESS_FLOOR, LOUDNESS_FULL_RMS, UNMEASURED_RMS = 0.08, 0.8, 0.6

function sf.loudness_gain(rms)
    rms = rms or UNMEASURED_RMS
    return sf.lerp(LOUDNESS_FLOOR, 1, math.min(1, math.max(0, rms / LOUDNESS_FULL_RMS)))
end

-- Loops use palette slots only, so a genre swap is a one-word change per loop.
local PALETTES = {
    techno = {
        primary = sf.rgb(1, 0.82, 0.6), -- warm white
        secondary = sf.rgb(1, 0.55, 0.1), -- amber
        accent = sf.rgb(0.75, 0, 0.02), -- deep red
        cold = sf.rgb(0.75, 0.85, 1), -- cool white
        black = sf.rgb(0, 0, 0)
    },
    house = {
        primary = sf.rgb(1, 0.82, 0.6), -- warm white
        secondary = sf.rgb(1, 0, 0.7), -- magenta
        accent = sf.rgb(1, 0.4, 0), -- orange
        cold = sf.rgb(0, 0.85, 1), -- cyan
        black = sf.rgb(0, 0, 0)
    }
}

function sf.palette(name)
    local palette = PALETTES[name]
    assert(palette ~= nil, "unknown palette")
    return palette
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

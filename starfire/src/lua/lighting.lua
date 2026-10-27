local sf = { osc = {} }
local FULL_TURN = 2 * math.pi

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

function sf.mix(first, last, amount)
    return sf.rgb(sf.lerp(first.red, last.red, amount), sf.lerp(first.green, last.green, amount),
        sf.lerp(first.blue, last.blue, amount))
end

local BEATS_PER_BAR = 4

function sf.beat_in_bar(ctx_or_beats)
    if type(ctx_or_beats) == "table" then return ctx_or_beats.bar_phase end
    return ctx_or_beats % BEATS_PER_BAR
end

function sf.bar(ctx_or_beats)
    if type(ctx_or_beats) == "table" then return ctx_or_beats.bar_index end
    return math.floor(ctx_or_beats / BEATS_PER_BAR)
end

function sf.phrase_phase(ctx_or_beats, bars)
    assert(finite(bars) and bars > 0, "phrase length must be positive and finite")
    if type(ctx_or_beats) == "table" then
        return ((ctx_or_beats.bar_index % bars) + ctx_or_beats.bar_phase / ctx_or_beats.beats_per_bar) / bars % 1
    end
    return (ctx_or_beats / (bars * BEATS_PER_BAR)) % 1
end

local render_intensity = 1
-- The depth curve lives in Rust (starfire::intensity) so pulse and movement share one table.
local render_pulse_depth = 1

local function use_render_intensity(intensity, pulse_depth)
    assert(finite(intensity) and intensity >= 0 and intensity <= 1, "intensity must be within 0..1")
    assert(finite(pulse_depth) and pulse_depth >= 0 and pulse_depth <= 1, "pulse depth must be within 0..1")
    render_intensity = intensity
    render_pulse_depth = pulse_depth
end

function sf.on_beat(ctx_or_beats, width, period)
    assert(finite(width) and width > 0, "beat envelope width must be positive and finite")
    period = period or 1
    assert(finite(period) and period > 0, "beat envelope period must be positive and finite")
    if type(ctx_or_beats) == "table" then ctx_or_beats = ctx_or_beats.beat_position end
    local decay_width = width * (2 - render_intensity)
    local remaining = math.max(0, 1 - (ctx_or_beats % period) / decay_width)
    return remaining * remaining * render_pulse_depth
end

local LEFT, RIGHT = -1, 1
function sf.side(index) return index % 2 == 1 and LEFT or RIGHT end

local LOUDNESS_FLOOR, LOUDNESS_FULL_RMS, UNMEASURED_RMS = 0.08, 0.8, 0.6

function sf.loudness_gain(rms)
    rms = rms or UNMEASURED_RMS
    return sf.lerp(LOUDNESS_FLOOR, 1, math.min(1, math.max(0, rms / LOUDNESS_FULL_RMS)))
end

local named_palettes, active_palette = {}, nil

function sf.palette(name)
    if name == nil then return active_palette end
    local palette = named_palettes[name]
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

return sf, named_palettes, function(palette) active_palette = palette end, use_render_intensity

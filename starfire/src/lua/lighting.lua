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
local each_count

local function use_render_intensity(intensity, pulse_depth)
    assert(finite(intensity) and intensity >= 0 and intensity <= 1, "intensity must be within 0..1")
    assert(finite(pulse_depth) and pulse_depth >= 0 and pulse_depth <= 1, "pulse depth must be within 0..1")
    render_intensity = intensity
    render_pulse_depth = pulse_depth
    -- Loops cannot pcall, so an error inside sf.each ends the frame; a new frame starts outside one.
    each_count = nil
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

local LEFT, CENTRE, RIGHT = -1, 0, 1
-- Group members run left to right (Autolux orders them by MVR position), so the side is the
-- member's half of the group, an odd middle at the centre. Inside sf.each the count defaults
-- to that group's size.
function sf.side(index, count)
    count = count or each_count
    assert(math.type(index) == "integer" and math.type(count) == "integer"
        and index >= 1 and index <= count, "side needs a member index within 1..count")
    if 2 * index == count + 1 then return CENTRE end
    return 2 * index <= count and LEFT or RIGHT
end

local LOUDNESS_FLOOR, LOUDNESS_FULL_RMS, UNMEASURED_RMS = 0.08, 0.8, 0.6

-- The measured RMS, or a typical level before any is measured.
function sf.energy(ctx) return ctx.normalized_rms_level or UNMEASURED_RMS end

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

-- sf.mirror(target) is native: partners by reflection about the stage centreline, derived
-- once at rig load (starfire::spatial).

function sf.each(target, make_effect)
    -- A nested sf.each sides by its own group, then hands the outer group's size back.
    local result, outer_count = {}, each_count
    local members = sf.members(target)
    each_count = #members
    for index, name in ipairs(members) do
        result[#result + 1] = make_effect(sf.fixture(name), index, #members)
    end
    each_count = outer_count
    return sf.scene(result)
end

-- ==== W1 context ====
-- Section lookahead and per-beat music. Starfire derives these from the cue Autolux sent ahead
-- of the room; every ctx field here is nil until there is something to say.

-- The active section's intent ("atmospheric", "breakdown", "buildup", "drop") and the beats
-- since it started; nil, nil while no cue is active.
function sf.section(ctx)
    return ctx.section_intent, ctx.section_elapsed_beats
end

local function smoothstep(x)
    return x * x * (3 - 2 * x)
end

local function bar_floor(ctx, beats)
    return math.floor(beats / ctx.beats_per_bar) * ctx.beats_per_bar
end

-- 0..1 over the section's first plateau_beats (the rotation entry's without a cue). The second
-- value holds still within a bar, so thresholds compared against it switch on a downbeat.
function sf.tension(ctx, plateau_beats)
    assert(finite(plateau_beats) and plateau_beats > 0, "tension plateau must be positive and finite")
    local elapsed = ctx.section_elapsed_beats or ctx.autoloop_elapsed_beats
    local function level(beats) return smoothstep(math.min(1, beats / plateau_beats)) end
    return level(elapsed), level(bar_floor(ctx, elapsed))
end

-- 0..1 over the last `beats` before the next cue starts, 0 while none is pending. Given `from`
-- (the level the loop builds to without a known next section), the result hands over from it
-- continuously: from + (1 - from) * fin. The second value holds still within a bar.
function sf.finale(ctx, beats, from)
    assert(finite(beats) and beats > 0, "finale length must be positive and finite")
    assert(from == nil or (finite(from) and from >= 0 and from <= 1), "finale base must be within 0..1")
    local remaining = ctx.beats_until_next
    local function level(left)
        local fin = left and 1 - math.min(1, left / beats) or 0
        return from and from + (1 - from) * fin or fin
    end
    local stepped = remaining and math.ceil(remaining / ctx.beats_per_bar) * ctx.beats_per_bar
    return level(remaining), level(stepped)
end

-- Whole beats left before the next cue starts, one step per beat, within the last `steps`
-- beats; nil otherwise.
function sf.countdown(ctx, steps)
    assert(math.type(steps) == "integer" and steps > 0, "countdown needs a positive whole step count")
    local remaining = ctx.beats_until_next
    if remaining == nil or remaining > steps then return nil end
    return math.ceil(remaining)
end

local function random_index(value)
    if value == nil then return 0 end
    assert(math.type(value) == "integer", "sf.random takes integer indices (use the sf.each index)")
    return value
end

local function splitmix(z)
    z = z + 0x9E3779B97F4A7C15
    z = (z ~ (z >> 30)) * 0xBF58476D1CE4E5B9
    z = (z ~ (z >> 27)) * 0x94D049BB133111EB
    return z ~ (z >> 31)
end

-- 0..1, a pure hash of the cue's seed and two integer indices: the same arguments give the same
-- value on every frame and every run.
function sf.random(ctx, i, j)
    local x = ctx.seed ~ (random_index(i) * 0x9E3779B97F4A7C15) ~ (random_index(j) << 21)
    return (splitmix(splitmix(x)) >> 11) / 9007199254740992.0
end

-- A neutral mid level before the analyser has measured a beat.
local UNMEASURED_FEATURE = 0.5

-- The newest beat's onset strength, 0..1.
function sf.onset(ctx)
    return ctx.onset or UNMEASURED_FEATURE
end

-- The newest beat's level in "bass", "mid" or "high", 0..1.
function sf.band(ctx, name)
    assert(name == "bass" or name == "mid" or name == "high", "band is bass, mid or high")
    return ctx[name] or UNMEASURED_FEATURE
end

-- The newest beat's tempo; 0 before the first beat.
function sf.bpm(ctx)
    return ctx.bpm
end

-- A beat-related rate in Hz: `per_beat` cycles every beat (2 is eighths).
function sf.beat_hz(ctx, per_beat)
    assert(finite(per_beat) and per_beat >= 0, "beats division must be finite and nonnegative")
    return ctx.bpm / 60 * per_beat
end

return sf, named_palettes, function(palette) active_palette = palette end, use_render_intensity

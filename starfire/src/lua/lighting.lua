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

-- ==== W3 aim ====
-- sf.aim(cell, x, y, z) -> pan, tilt (nil, nil without a pan range) and sf.focus(cell) -> x, y, z
-- are native (starfire::fixtures). Points are metres in the MVR scene frame: X cross-stage,
-- Y upstage, Z up.

local X_AXIS, Z_AXIS = { 1, 0, 0 }, { 0, 0, 1 }
-- Parallel means float noise only; the fallbacks are for beams plotted exactly vertical or
-- exactly cross-stage.
local PARALLEL = 1e-9

local function cross(a, b)
    return { a[2] * b[3] - a[3] * b[2], a[3] * b[1] - a[1] * b[3], a[1] * b[2] - a[2] * b[1] }
end

local function norm(v) return math.sqrt(v[1] * v[1] + v[2] * v[2] + v[3] * v[3]) end

-- The unit normal of the plane holding `beam` and `towards`, or `fallback` when they are parallel.
local function rotation_axis(beam, towards, fallback)
    local axis = cross(beam, towards)
    local length = norm(axis)
    if length <= PARALLEL * norm(beam) then return fallback end
    return { axis[1] / length, axis[2] / length, axis[3] / length }
end

-- Rodrigues: `v` turned right-handed by `degrees` about the unit `axis`.
local function rotate(v, axis, degrees)
    local angle = math.rad(degrees)
    local c, s = math.cos(angle), math.sin(angle)
    local k = cross(axis, v)
    local along = (axis[1] * v[1] + axis[2] * v[2] + axis[3] * v[3]) * (1 - c)
    return { v[1] * c + k[1] * s + axis[1] * along, v[2] * c + k[2] * s + axis[2] * along,
        v[3] * c + k[3] * s + axis[3] * along }
end

-- Spread turns the beam (focus - position) in its plane with X, positive towards +X; elevation
-- turns it in its plane with Z, positive up. Both planes come from the rest beam, so a circle of
-- offsets stays a cone round a vertical beam. A beam along X spreads about Z, one along Z
-- elevates about X. nil, nil for a cell without a pan range.
function sf.aim_offset(cell, spread_degrees, elevation_degrees)
    assert(finite(spread_degrees) and finite(elevation_degrees), "aim offsets must be finite degrees")
    local x, y, z = sf.position(cell)
    local focus_x, focus_y, focus_z = sf.focus(cell)
    local beam = { focus_x - x, focus_y - y, focus_z - z }
    local spread_axis = rotation_axis(beam, X_AXIS, Z_AXIS)
    local elevation_axis = rotation_axis(beam, Z_AXIS, X_AXIS)
    beam = rotate(rotate(beam, elevation_axis, elevation_degrees), spread_axis, spread_degrees)
    return sf.aim(cell, x + beam[1], y + beam[2], z + beam[3])
end

-- The beam along (dx, dy, dz) from the head whatever its focus: parallel walls, every beam up.
function sf.aim_direction(cell, dx, dy, dz)
    assert(finite(dx) and finite(dy) and finite(dz) and (dx ~= 0 or dy ~= 0 or dz ~= 0),
        "aim direction must be a finite nonzero vector")
    local x, y, z = sf.position(cell)
    return sf.aim(cell, x + dx, y + dy, z + dz)
end

-- Stands in for sf.random(ctx, a, b) until it lands; the same SplitMix64 recipe over ctx.seed.
local function _w3_hash(ctx, a, b)
    b = b or 0
    assert(math.type(a) == "integer" and math.type(b) == "integer", "hash arguments must be integers")
    local x = (ctx.seed or 0) ~ (a * 0x9E3779B97F4A7C15) ~ (b << 21)
    for _ = 1, 2 do
        x = (x ~ (x >> 30)) * 0xBF58476D1CE4E5B9
        x = (x ~ (x >> 27)) * 0x94D049BB133111EB
        x = x ~ (x >> 31)
    end
    return (x >> 11) / 2.0 ^ 53
end

-- Each shape gives (spread, elevation) per unit size at cycle position t (nil when static), for
-- a member at signed stage position u (wings: outermost +-1, centreline 0; else 1) on side w.
local PHASER_SHAPES = {
    fan = function(t, u) return u * (t and 0.5 + 0.5 * math.cos(FULL_TURN * t) or 1), 0 end,
    cross = function(t, u) return -u * (t and math.cos(FULL_TURN * t) or 1), 0 end,
    circle = function(t, _, w) return w * math.cos(FULL_TURN * t), math.sin(FULL_TURN * t) end,
    figure8 = function(t, _, w)
        return w * math.sin(FULL_TURN * t), 0.5 * math.sin(2 * FULL_TURN * t)
    end,
    tilt_wave = function(t) return 0, math.sin(FULL_TURN * t) end,
    ballyhoo = function(t, _, w, ctx, index)
        return w * (0.7 * math.sin(FULL_TURN * t) + 0.3 * math.sin(3 * FULL_TURN * t + FULL_TURN * _w3_hash(ctx, index))),
            0.7 * math.cos(FULL_TURN * t) + 0.3 * math.sin(2 * FULL_TURN * t + FULL_TURN * _w3_hash(ctx, index, 1))
    end,
}
local STATIC_SHAPES = { fan = true, cross = true }
local LONGEST_MEASURE_EXPONENT = 5

-- 1, 2, 4, 8, 16 or 32 beats, nearest in ratio: cycles divide phrases, so counted from the
-- section start every member is back at its start on each phrase line.
local function snap_measure(measure)
    local exponent = math.floor(math.log(measure, 2) + 0.5)
    return 2.0 ^ math.max(0, math.min(LONGEST_MEASURE_EXPONENT, exponent))
end

-- spec = { shape = "fan"|"cross"|"circle"|"figure8"|"tilt_wave"|"ballyhoo",
--   size = spread degrees, tilt_size = elevation degrees (default size),
--   measure = beats per cycle, snapped as above (nil holds a fan or cross still),
--   phase = 0..1 spread of the cycle across the selection (default 0),
--   wings = mirror partners share a rank and move outward (default true; false for tilt_wave),
--   centre_pan = spread degrees, centre_tilt = elevation degrees added to every member }
-- Member i cycles at t = frac(E / measure + phase * rank_i / ranks), E the beats since the
-- section start, else since the loop started. Change measure only where E is a multiple of both
-- measures (any bar line for measures up to 4), or t jumps. Returns sf.pan_tilt effects, none for
-- cells without a pan range.
function sf.phaser(target, ctx, spec)
    local shape = PHASER_SHAPES[spec.shape]
    assert(shape ~= nil, "unknown phaser shape")
    local size = spec.size
    local tilt_size = spec.tilt_size or size
    local phase_spread = spec.phase or 0
    local centre_pan, centre_tilt = spec.centre_pan or 0, spec.centre_tilt or 0
    assert(finite(size) and finite(tilt_size) and finite(centre_pan) and finite(centre_tilt),
        "phaser sizes and centres must be finite degrees")
    assert(finite(phase_spread) and phase_spread >= 0 and phase_spread <= 1, "phaser phase must be within 0..1")
    local measure = spec.measure
    if measure == nil then
        assert(STATIC_SHAPES[spec.shape], "only a fan or cross holds still without a measure")
    else
        assert(finite(measure) and measure > 0, "phaser measure must be positive beats")
        measure = snap_measure(measure)
    end
    local wings = spec.wings
    if wings == nil then wings = spec.shape ~= "tilt_wave" end

    local members = sf.members(target)
    local rank, across, ranks = {}, {}, 0
    if wings then
        local xs, low, high = {}, math.huge, -math.huge
        for _, cell in ipairs(members) do
            local x = sf.position(cell)
            xs[cell], low, high = x, math.min(low, x), math.max(high, x)
        end
        local half_width = (high - low) / 2
        -- Partners take opposite halves of the gap between them, so they mirror exactly even
        -- where the plot is uneven; a member that is its own partner sits at 0.
        for _, pair in ipairs(sf.mirror(target)) do
            local first, second = pair[1].fixture, pair[2].fixture
            local reach = half_width > 0 and (xs[second] - xs[first]) / (2 * half_width) or 0
            rank[first], rank[second] = ranks, ranks
            across[first], across[second] = -reach, reach
            ranks = ranks + 1
        end
    end
    for _, cell in ipairs(members) do
        if rank[cell] == nil then
            rank[cell], across[cell], ranks = ranks, wings and 0 or 1, ranks + 1
        end
    end

    local elapsed = ctx.section_elapsed_beats or ctx.autoloop_elapsed_beats or ctx.beat_position
    local effects = {}
    for index, cell in ipairs(members) do
        local t = measure and (elapsed / measure + phase_spread * rank[cell] / ranks) % 1
        local u = across[cell]
        local w = u > 0 and 1 or u < 0 and -1 or 0
        local spread, elevation = shape(t, u, w, ctx, index)
        local pan, tilt = sf.aim_offset(cell, size * spread + centre_pan, tilt_size * elevation + centre_tilt)
        if pan ~= nil then effects[#effects + 1] = sf.pan_tilt(sf.fixture(cell), pan, tilt) end
    end
    return effects
end

return sf, named_palettes, function(palette) active_palette = palette end, use_render_intensity

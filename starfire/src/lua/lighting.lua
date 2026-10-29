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

-- Given ctx, bars since the section start (the bar line at or before a selection without a
-- cue); the absolute bar index before any.
function sf.bar(ctx_or_beats)
    if type(ctx_or_beats) == "table" then
        local elapsed = ctx_or_beats.section_elapsed_beats
        if elapsed == nil then return ctx_or_beats.bar_index end
        return math.floor(elapsed / ctx_or_beats.beats_per_bar)
    end
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
-- since it started (since the bar line at or before a selection without a cue); nil without
-- either.
function sf.section(ctx)
    return ctx.section_intent, ctx.section_elapsed_beats
end

local TENSION_BEATS = 128

local function phrase_beats(ctx)
    return ctx.section_elapsed_beats or ctx.autoloop_elapsed_beats
end

-- 0..1, an ease-in (cubic) over `beats` (128) of section time until the next change becomes
-- known (at ctx.next_known_at); from there it bends linearly to full at that change, so the
-- curve fills the real build without a jump. The second value is the tension at the last
-- downbeat, for loops that step against it.
function sf.tension(ctx, beats)
    beats = beats or TENSION_BEATS
    assert(finite(beats) and beats > 0, "tension length must be positive and finite")
    local elapsed, left, known = phrase_beats(ctx), ctx.beats_until_next, ctx.next_known_at
    local function ease(played) return math.min(1, played / beats) ^ 3 end
    local function at(played)
        if left == nil or known == nil or played < known then return ease(played) end
        local rest = elapsed - known + left
        if rest <= 0 then return 1 end
        local base = ease(known)
        return base + (1 - base) * (played - known) / rest
    end
    local bar = ctx.beats_per_bar
    return at(elapsed), at(math.floor(elapsed / bar) * bar)
end

-- 0..1 over the last `beats` before the next cue starts, 0 while none is pending. Given `from`
-- (the level the loop builds to without a known next section), the result hands over from it
-- continuously: from + (1 - from) * fin. The second value holds each bar at the level its end
-- reaches, so the last bar is full.
function sf.finale(ctx, beats, from)
    assert(finite(beats) and beats > 0, "finale length must be positive and finite")
    assert(from == nil or (finite(from) and from >= 0 and from <= 1), "finale base must be within 0..1")
    local remaining = ctx.beats_until_next
    local function level(left)
        local fin = left and 1 - math.min(1, left / beats) or 0
        return from and from + (1 - from) * fin or fin
    end
    local bar = ctx.beats_per_bar
    local stepped = remaining and math.max(0, (math.ceil(remaining / bar) - 1) * bar)
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

-- Bars left before the next cue starts (fractional), nil while none is pending.
function sf.bars_until_next(ctx)
    return ctx.beats_until_next and ctx.beats_until_next / ctx.beats_per_bar
end

-- Which `bars`-long phrase of the section this is (0 first) and the position in it, 0..1.
function sf.phrase(ctx, bars)
    assert(finite(bars) and bars > 0, "phrase length must be positive and finite")
    local length = bars * ctx.beats_per_bar
    local elapsed = ctx.section_elapsed_beats or ctx.beat_position
    return math.floor(elapsed / length), (elapsed % length) / length
end

local function random_index(value)
    if value == nil then return 0 end
    assert(math.type(value) == "integer", "sf.random takes integer indices (use the sf.each index)")
    return value
end

-- 0..1, a pure hash of the cue's seed and up to three integer indices (say phrase, cell, step):
-- the same arguments give the same value on every frame and every run. SplitMix64's
-- finaliser, two rounds.
function sf.random(ctx, i, j, k)
    local x = ctx.seed ~ (random_index(i) * 0x9E3779B97F4A7C15) ~ (random_index(j) << 21)
        ~ (random_index(k) << 42)
    for _ = 1, 2 do
        x = (x ~ (x >> 30)) * 0xBF58476D1CE4E5B9
        x = (x ~ (x >> 27)) * 0x94D049BB133111EB
        x = x ~ (x >> 31)
    end
    return (x >> 11) / 2.0 ^ 53
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

-- ==== W3 aim ====
-- sf.aim(cell, x, y, z) -> pan, tilt (nil, nil for a cell without a pan range) and
-- sf.focus(cell) -> x, y, z are native (starfire::fixtures). Points are metres in the MVR scene
-- frame: X cross-stage, Y upstage, Z up.

local X_AXIS, Z_AXIS = { 1, 0, 0 }, { 0, 0, 1 }
-- Parallel means float noise only; the fallback is for beams plotted exactly plumb.
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

-- Elevation turns the rest beam in its vertical plane, positive up; a plumb beam turns about X.
-- Spread turns it about the perpendicular in that plane, so the two offsets are at right angles
-- for every beam and a circle of offsets is a cone. Positive spread moves any throw with a Y
-- component towards +X, and a pure cross-stage throw clockwise seen from above. Elevation applies
-- first, so a horizontal beam's offsets are exactly heading and elevation. `beam` = {dx, dy, dz}
-- in metres is the rest beam, by default focus - position. nil, nil for a cell without a pan
-- range.
function sf.aim_offset(cell, spread_degrees, elevation_degrees, beam)
    assert(finite(spread_degrees) and finite(elevation_degrees), "aim offsets must be finite degrees")
    local x, y, z = sf.position(cell)
    if beam == nil then
        local focus_x, focus_y, focus_z = sf.focus(cell)
        beam = { focus_x - x, focus_y - y, focus_z - z }
        -- A focus at the head itself gives nothing to turn; rest along the pan axis as sf.aim does.
        if norm(beam) == 0 then return sf.aim(cell, x, y, z) end
    else
        assert(type(beam) == "table" and finite(beam[1]) and finite(beam[2]) and finite(beam[3])
            and norm(beam) > 0, "a rest beam must be a finite nonzero {dx, dy, dz}")
    end
    local elevation_axis = rotation_axis(beam, Z_AXIS, X_AXIS)
    local spread_axis = cross(elevation_axis, beam)
    local scale = (beam[2] < 0 and 1 or -1) / norm(spread_axis)
    spread_axis = { spread_axis[1] * scale, spread_axis[2] * scale, spread_axis[3] * scale }
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
    -- Each member's own phase on the fundamental keeps partners from reading as one mirrored circle.
    ballyhoo = function(t, _, w, ctx, index)
        local fundamental = FULL_TURN * (t + sf.random(ctx, index, 2))
        return w * (0.7 * math.sin(fundamental) + 0.3 * math.sin(3 * FULL_TURN * t + FULL_TURN * sf.random(ctx, index))),
            0.7 * math.cos(fundamental) + 0.3 * math.sin(2 * FULL_TURN * t + FULL_TURN * sf.random(ctx, index, 1))
    end,
}
local STATIC_SHAPES = { fan = true, cross = true }
-- Cycles that divide a phrase, so counted from the section start every member is back at its
-- start on each phrase line.
local MEASURES = { [1] = true, [2] = true, [4] = true, [8] = true, [16] = true, [32] = true }

-- spec = { shape = "fan"|"cross"|"circle"|"figure8"|"tilt_wave"|"ballyhoo",
--   size = spread degrees (tilt_wave needs none), tilt_size = elevation degrees (default size),
--   measure = beats per cycle: 1, 2, 4, 8, 16 or 32 (nil holds a fan or cross still),
--   phase = -1..1 cycles spread across the selection (default 0); positive runs the wave in
--     selection order, left to right, negative the other way,
--   wings = mirror partners share a rank and move outward (default true; false for tilt_wave),
--   centre_pan = spread degrees, centre_tilt = elevation degrees added to every member,
--   at = {x, y, z} metres every beam rests on, or direction = {dx, dy, dz} every beam rests
--     along (default each member's focus) }
-- Member i cycles at t = frac(E / measure - phase * rank_i / ranks), E the beats since the
-- section start, else since the loop started. t restarts with every section, so it runs on
-- unbroken only when the section's length is a multiple of the measure; likewise change measure
-- only where E is a multiple of both measures. Returns sf.pan_tilt effects for the members with
-- a pan range.
function sf.phaser(target, ctx, spec)
    local shape = PHASER_SHAPES[spec.shape]
    assert(shape ~= nil, "unknown phaser shape")
    local size = spec.size
    local tilt_size = spec.tilt_size or size
    if spec.shape == "tilt_wave" then size = size or 0 end
    local phase_spread = spec.phase or 0
    local centre_pan, centre_tilt = spec.centre_pan or 0, spec.centre_tilt or 0
    assert(finite(size) and finite(tilt_size) and finite(centre_pan) and finite(centre_tilt),
        "phaser sizes and centres must be finite degrees")
    assert(finite(phase_spread) and phase_spread >= -1 and phase_spread <= 1, "phaser phase must be within -1..1")
    local measure = spec.measure
    if measure == nil then
        assert(STATIC_SHAPES[spec.shape], "only a fan or cross holds still without a measure")
    else
        assert(MEASURES[measure], "phaser measure must be 1, 2, 4, 8, 16 or 32 beats")
    end
    local at, direction = spec.at, spec.direction
    assert(at == nil or direction == nil, "a phaser rests on a point or along a direction, not both")
    assert(at == nil or type(at) == "table" and finite(at[1]) and finite(at[2]) and finite(at[3]),
        "phaser at must be finite {x, y, z} metres")
    local wings = spec.wings
    if wings == nil then wings = spec.shape ~= "tilt_wave" end

    -- Only heads that can aim take part, so a wash in the group neither widens the row nor
    -- takes a rank.
    local members, aimable = {}, {}
    for _, cell in ipairs(sf.members(target)) do
        if sf.aim(cell, sf.focus(cell)) ~= nil then
            members[#members + 1] = cell
            aimable[cell] = true
        end
    end
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
            if aimable[first] and aimable[second] then
                local reach = half_width > 0 and (xs[second] - xs[first]) / (2 * half_width) or 0
                rank[first], rank[second] = ranks, ranks
                across[first], across[second] = -reach, reach
                ranks = ranks + 1
            end
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
        local t = measure and (elapsed / measure - phase_spread * rank[cell] / ranks) % 1
        local u = across[cell]
        -- A centre member circles the way the +X side does rather than nodding in a line.
        local w = u < 0 and -1 or 1
        local spread, elevation = shape(t, u, w, ctx, index)
        local beam = direction
        if at ~= nil then
            local x, y, z = sf.position(cell)
            beam = { at[1] - x, at[2] - y, at[3] - z }
        end
        local pan, tilt = sf.aim_offset(cell, size * spread + centre_pan,
            tilt_size * elevation + centre_tilt, beam)
        effects[#effects + 1] = sf.pan_tilt(sf.fixture(cell), pan, tilt)
    end
    return effects
end

return sf, named_palettes, function(palette) active_palette = palette end, use_render_intensity

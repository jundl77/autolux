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
local render_pulse_depth = 1
local each_count

local function use_render_intensity(intensity, pulse_depth)
    assert(finite(intensity) and intensity >= 0 and intensity <= 1, "intensity must be within 0..1")
    assert(finite(pulse_depth) and pulse_depth >= 0 and pulse_depth <= 1, "pulse depth must be within 0..1")
    render_intensity = intensity
    render_pulse_depth = pulse_depth
    -- Loops cannot pcall, so an error inside sf.each ends the frame with each_count still set.
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
-- Autolux orders group members left to right by MVR position.
function sf.side(index, count)
    count = count or each_count
    assert(math.type(index) == "integer" and math.type(count) == "integer"
        and index >= 1 and index <= count, "side needs a member index within 1..count")
    if 2 * index == count + 1 then return CENTRE end
    return 2 * index <= count and LEFT or RIGHT
end

local LOUDNESS_FLOOR, LOUDNESS_FULL_RMS, UNMEASURED_RMS = 0.08, 0.8, 0.6

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

function sf.white() return active_palette.white end

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
    local result, outer_count = {}, each_count
    local members = sf.members(target)
    each_count = #members
    for index, name in ipairs(members) do
        result[#result + 1] = make_effect(sf.fixture(name), index, #members)
    end
    each_count = outer_count
    return sf.scene(result)
end

-- Section and music

function sf.section_beats(ctx)
    return ctx.section_elapsed_beats or ctx.autoloop_elapsed_beats
end

local TENSION_HALF_BEATS = 48

function sf.tension(ctx, half_beats)
    half_beats = half_beats or TENSION_HALF_BEATS
    assert(finite(half_beats) and half_beats > 0, "tension half point must be positive and finite")
    local elapsed, left, known = sf.section_beats(ctx), ctx.beats_until_next, ctx.next_known_at
    local function ease(played) return played / (played + half_beats) end
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

function sf.beat_strobe_hz(ctx, per_beat)
    assert(finite(per_beat) and per_beat > 0, "flashes per beat must be positive and finite")
    local division = per_beat
    while division >= 0.5 do
        local hz = ctx.bpm / 60 * division
        if hz <= ctx.strobe_max_hz then return hz end
        division = division / 2
    end
    return 0
end

local function random_index(value)
    if value == nil then return 0 end
    assert(math.type(value) == "integer", "sf.random takes integer indices (use the sf.each index)")
    return value
end

local function splitmix64_finalise(x)
    x = (x ~ (x >> 30)) * 0xBF58476D1CE4E5B9
    x = (x ~ (x >> 27)) * 0x94D049BB133111EB
    return x ~ (x >> 31)
end

function sf.random(ctx, i, j, k)
    local x = ctx.seed ~ (random_index(i) * 0x9E3779B97F4A7C15) ~ (random_index(j) << 21)
        ~ (random_index(k) << 42)
    return (splitmix64_finalise(splitmix64_finalise(x)) >> 11) / 2.0 ^ 53
end

local UNMEASURED_FEATURE = 0.5

function sf.onset(ctx)
    return ctx.onset or UNMEASURED_FEATURE
end

function sf.band(ctx, name)
    assert(name == "bass" or name == "mid" or name == "high", "band is bass, mid or high")
    return ctx[name] or UNMEASURED_FEATURE
end

-- Aim: points in metres in the MVR scene frame (X cross-stage, Y upstage, Z up), angles in degrees

local X_AXIS, Z_AXIS = { 1, 0, 0 }, { 0, 0, 1 }
local PARALLEL_TOLERANCE = 1e-9

local function cross(a, b)
    return { a[2] * b[3] - a[3] * b[2], a[3] * b[1] - a[1] * b[3], a[1] * b[2] - a[2] * b[1] }
end

local function norm(v) return math.sqrt(v[1] * v[1] + v[2] * v[2] + v[3] * v[3]) end

local function rotation_axis(beam, towards, fallback)
    local axis = cross(beam, towards)
    local length = norm(axis)
    if length <= PARALLEL_TOLERANCE * norm(beam) then return fallback end
    return { axis[1] / length, axis[2] / length, axis[3] / length }
end

local function rotate(v, axis, degrees)
    local angle = math.rad(degrees)
    local c, s = math.cos(angle), math.sin(angle)
    local k = cross(axis, v)
    local along = (axis[1] * v[1] + axis[2] * v[2] + axis[3] * v[3]) * (1 - c)
    return { v[1] * c + k[1] * s + axis[1] * along, v[2] * c + k[2] * s + axis[2] * along,
        v[3] * c + k[3] * s + axis[3] * along }
end

-- The beam sf.aim_offset aims along, or nil for a cell focused on itself.
local function offset_beam(cell, spread_degrees, elevation_degrees, beam)
    assert(finite(spread_degrees) and finite(elevation_degrees), "aim offsets must be finite degrees")
    if beam == nil then
        local x, y, z = sf.position(cell)
        local focus_x, focus_y, focus_z = sf.focus(cell)
        beam = { focus_x - x, focus_y - y, focus_z - z }
        if norm(beam) == 0 then return nil end
    else
        assert(type(beam) == "table" and finite(beam[1]) and finite(beam[2]) and finite(beam[3])
            and norm(beam) > 0, "a rest beam must be a finite nonzero {dx, dy, dz}")
    end
    local elevation_axis = rotation_axis(beam, Z_AXIS, X_AXIS)
    local spread_axis = cross(elevation_axis, beam)
    local scale = (beam[2] < 0 and 1 or -1) / norm(spread_axis)
    spread_axis = { spread_axis[1] * scale, spread_axis[2] * scale, spread_axis[3] * scale }
    return rotate(rotate(beam, elevation_axis, elevation_degrees), spread_axis, spread_degrees)
end

local function aim_along(cell, beam)
    local x, y, z = sf.position(cell)
    if beam == nil then return sf.aim(cell, x, y, z) end
    return sf.aim(cell, x + beam[1], y + beam[2], z + beam[3])
end

function sf.aim_offset(cell, spread_degrees, elevation_degrees, beam)
    return aim_along(cell, offset_beam(cell, spread_degrees, elevation_degrees, beam))
end

function sf.aim_direction(cell, dx, dy, dz)
    assert(finite(dx) and finite(dy) and finite(dz) and (dx ~= 0 or dy ~= 0 or dz ~= 0),
        "aim direction must be a finite nonzero vector")
    local x, y, z = sf.position(cell)
    return sf.aim(cell, x + dx, y + dy, z + dz)
end

local PHASER_SHAPES = {
    fan = function(cycle, across) return across * (cycle and 0.5 + 0.5 * math.cos(FULL_TURN * cycle) or 1), 0 end,
    cross = function(cycle, across) return -across * (cycle and math.cos(FULL_TURN * cycle) or 1), 0 end,
    circle = function(cycle, _, side) return side * math.cos(FULL_TURN * cycle), math.sin(FULL_TURN * cycle) end,
    figure8 = function(cycle, _, side)
        return side * math.sin(FULL_TURN * cycle), 0.5 * math.sin(2 * FULL_TURN * cycle)
    end,
    tilt_wave = function(cycle) return 0, math.sin(FULL_TURN * cycle) end,
    ballyhoo = function(cycle, _, side, ctx, index)
        local fundamental = FULL_TURN * (cycle + sf.random(ctx, index, 2))
        return side * (0.7 * math.sin(fundamental) + 0.3 * math.sin(3 * FULL_TURN * cycle + FULL_TURN * sf.random(ctx, index))),
            0.7 * math.cos(fundamental) + 0.3 * math.sin(2 * FULL_TURN * cycle + FULL_TURN * sf.random(ctx, index, 1))
    end,
}
local STATIC_SHAPES = { fan = true, cross = true }
-- Powers of two divide a phrase, so every member is back at its start on each phrase line.
local MEASURES = { [1] = true, [2] = true, [4] = true, [8] = true, [16] = true, [32] = true }

local SPIN_MEASURES = { [4] = true, [8] = true, [16] = true }
local FULL_CIRCLE_DEGREES = 360
-- A spinning member's look fades out over the first quarter turn and back in over the last, so
-- the circle runs round its focus aim, which sf.pan_reach measures from.
local SPIN_LOOK_FADE_TURNS = 0.25

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
    local spun, spin_degrees
    if spec.spin ~= nil then spun, spin_degrees = sf.spin(target, ctx, spec.spin) end

    local aimable_members, aimable = {}, {}
    for _, cell in ipairs(sf.members(target)) do
        if sf.aim(cell, sf.focus(cell)) ~= nil then
            aimable_members[#aimable_members + 1] = cell
            aimable[cell] = true
        end
    end
    local rank, across, ranks = {}, {}, 0
    if wings then
        local xs, low, high = {}, math.huge, -math.huge
        for _, cell in ipairs(aimable_members) do
            local x = sf.position(cell)
            xs[cell], low, high = x, math.min(low, x), math.max(high, x)
        end
        local half_width = (high - low) / 2
        -- Partners take opposite halves of their gap, so they mirror exactly on an uneven plot.
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
    for _, cell in ipairs(aimable_members) do
        if rank[cell] == nil then
            rank[cell], across[cell], ranks = ranks, wings and 0 or 1, ranks + 1
        end
    end

    local elapsed = sf.section_beats(ctx)
    local effects = {}
    for index, cell in ipairs(aimable_members) do
        local cycle = measure and (elapsed / measure - phase_spread * rank[cell] / ranks) % 1
        -- A centre member takes +1, so it circles like the +X side instead of nodding in a line.
        local side = across[cell] < 0 and -1 or 1
        local spread, elevation = shape(cycle, across[cell], side, ctx, index)
        local beam = direction
        if at ~= nil then
            local x, y, z = sf.position(cell)
            beam = { at[1] - x, at[2] - y, at[3] - z }
        end
        local look = 1
        if cell == spun then
            look = 1 - math.min(1, math.abs(spin_degrees) / (SPIN_LOOK_FADE_TURNS * FULL_CIRCLE_DEGREES))
        end
        beam = offset_beam(cell, look * (size * spread + centre_pan),
            look * (tilt_size * elevation + centre_tilt), beam)
        if cell == spun and beam ~= nil then beam = rotate(beam, Z_AXIS, -spin_degrees) end
        local pan, tilt = aim_along(cell, beam)
        effects[#effects + 1] = sf.pan_tilt(sf.fixture(cell), pan, tilt)
    end
    return effects
end

-- Spin: one member's beam turns a full circle about the vertical and unwinds, eased, over
-- `measure` beats from `at` beats into every `every`-beat period of section time. Returns that
-- member and its heading offset in degrees (clockwise seen from above), or nil between spins;
-- a phaser with `spin = spec` turns that member's look by it. Only members whose pan reaches a
-- full circle one way from the focus aim take part, so no head meets an end stop mid-turn;
-- sf.random picks the member and, where both ways reach, the way, per period.
function sf.spin(target, ctx, spec)
    local measure, every, at = spec.measure, spec.every or 64, spec.at or 0
    assert(SPIN_MEASURES[measure], "spin measure must be 4, 8 or 16 beats")
    assert(finite(every) and finite(at) and at >= 0 and at + measure <= every,
        "a spin must fit inside its period")
    local elapsed = sf.section_beats(ctx)
    local period = math.floor(elapsed / every)
    local progress = (elapsed - period * every - at) / measure
    if progress < 0 or progress >= 1 then return nil end
    local able = {}
    for _, cell in ipairs(sf.members(target)) do
        local clockwise, anticlockwise = sf.pan_reach(cell)
        if clockwise ~= nil and math.max(clockwise, anticlockwise) >= FULL_CIRCLE_DEGREES then
            able[#able + 1] = { cell = cell, clockwise = clockwise >= FULL_CIRCLE_DEGREES,
                anticlockwise = anticlockwise >= FULL_CIRCLE_DEGREES }
        end
    end
    if #able == 0 then return nil end
    local chosen = able[1 + math.floor(sf.random(ctx, period) * #able)]
    local sense = chosen.clockwise and 1 or -1
    if chosen.clockwise and chosen.anticlockwise and sf.random(ctx, period, 1) < 0.5 then sense = -1 end
    return chosen.cell, sense * FULL_CIRCLE_DEGREES * (1 - math.cos(FULL_TURN * progress)) / 2
end

-- Show helpers

function sf.from_centre(index, count)
    return count > 1 and math.abs(2 * (index - 1) / (count - 1) - 1) or 0
end

function sf.pixels(share, count)
    return math.max(1, share * count)
end

function sf.is_middle(index, count)
    return math.abs(index - (count + 1) / 2) < 1
end

local function fade_over(cells_away, share, count)
    return math.max(0, 1 - cells_away / sf.pixels(share, count))
end

function sf.mirrored_spot(index, count, at, share)
    return fade_over(math.abs(sf.from_centre(index, count) - at) * (count - 1) / 2, share, count)
end

function sf.chase(index, count, head, tail_share)
    local behind = (head * count - (index - 1)) % count
    return fade_over(behind, tail_share, count), behind
end

function sf.shared_focus(target)
    local members, x, y, z = sf.members(target), 0, 0, 0
    if #members == 0 then return nil end
    for _, cell in ipairs(members) do
        local fx, fy, fz = sf.focus(cell)
        x, y, z = x + fx, y + fy, z + fz
    end
    return { x / #members, y / #members, z / #members }
end

local SNAP_LEAD_BEATS, SNAP_DARK_BEATS = 0.5, 0.25

function sf.snap(ctx, period)
    local beats = sf.section_beats(ctx) + SNAP_LEAD_BEATS
    return math.floor(beats / period) % 2, beats % period < SNAP_DARK_BEATS
end

local UNSNAPPED_PHASER_KEYS = { "tilt_size", "measure", "phase", "wings", "centre_pan", "at", "direction" }

function sf.snap_look(ctx, period, looks, reach)
    local index, dark = sf.snap(ctx, period)
    local look = looks[index + 1]
    for _, key in ipairs(UNSNAPPED_PHASER_KEYS) do
        assert(look[key] == nil, "a snap look holds only shape, size and centre_tilt")
    end
    return { shape = look.shape, size = look.size * reach, centre_tilt = (look.centre_tilt or 0) * reach },
        dark, index
end

local COLOUR_SWAP_BEATS = 8

function sf.colours_swapped(ctx)
    return math.floor(sf.section_beats(ctx) / COLOUR_SWAP_BEATS) % 2 == 1
end

function sf.drop_levels(ctx, floor_quiet, floor_loud)
    local energy = sf.energy(ctx)
    return sf.loudness_gain(energy), sf.lerp(floor_quiet, floor_loud, energy), energy
end

local FINALE_BEATS, EASE_BEATS, LADDER_BEATS = 16, 8, 8
local DRIVE_FLOOR, DRIVE_GAIN = 0.25, 0.65
local STAGE_TENSIONS = { 1 / 3, 1 / 2 }

local function stage_of(tension)
    return tension < STAGE_TENSIONS[1] and 0 or tension < STAGE_TENSIONS[2] and 1 or 2
end

local function ramp_to_dip(left, over)
    return over > 0 and math.max(0, math.min(1, 1 - left / over)) or 1
end

local function back_at_line(ctx, beats)
    local elapsed = sf.section_beats(ctx)
    local back = elapsed % beats
    return {
        section_elapsed_beats = elapsed - back, autoloop_elapsed_beats = elapsed - back,
        beats_until_next = ctx.beats_until_next and ctx.beats_until_next + back,
        next_known_at = ctx.next_known_at, beats_per_bar = ctx.beats_per_bar,
    }
end

function sf.build(ctx)
    local tension, step = sf.tension(ctx)
    local drive, finale = DRIVE_FLOOR + DRIVE_GAIN * tension, 0
    local left = ctx.next_intent == "drop" and (ctx.beats_until_dip or ctx.beats_until_next) or nil
    local span = FINALE_BEATS
    if left ~= nil then
        local known = ctx.next_known_at
        span = known and math.min(FINALE_BEATS, left + sf.section_beats(ctx) - known) or FINALE_BEATS
        finale = ramp_to_dip(left, span)
        drive = drive + (1 - drive) * finale
    elseif ctx.next_intent ~= nil then
        drive = drive * (1 - 0.5 * sf.finale(ctx, EASE_BEATS))
    end
    local measure = ({ 8, 4, 2 })[stage_of(sf.tension(back_at_line(ctx, LADDER_BEATS))) + 1]
    return {
        drive = drive, finale = finale, tension = tension, step = step, stage = stage_of(step),
        left = left, span = span, measure = measure,
    }
end

function sf.approach(build, beats)
    if build.left == nil then return 0 end
    return ramp_to_dip(build.left, math.min(beats, build.span))
end

function sf.drop_within(build, beats)
    return build.left ~= nil and build.left <= beats
end

function sf.overlay_progress(ctx)
    local length = ctx.overlay_duration_beats or 1
    return math.min(1, ctx.autoloop_elapsed_beats / length), length - ctx.autoloop_elapsed_beats
end

local LOOP_GROUPS = { "wash", "strip", "movers" }

function sf.white_strobe(level, hz)
    local effects = {}
    for _, name in ipairs(LOOP_GROUPS) do
        local group = sf.group(name)
        effects[#effects + 1] = { sf.colour(group, sf.white()), sf.dimmer(group, level), sf.strobe(group, hz) }
    end
    return sf.scene(effects)
end

return sf, named_palettes, function(palette) active_palette = palette end, use_render_intensity

-- 8-beat loop, no strobe (Autolux adds a short one on the drop itself).
-- energy: 2
-- family: warm
-- movement: snap
-- wash: primary kick, outer pair on beats 1 and 3, inner pair on beats 2 and 4,
-- each over an RMS-scaled floor, so the hit pumps outward and inward like a piston.
-- strip: full primary hit on the one, accent hit on beat 3, dim primary otherwise.
-- movers: primary, snap between two mirrored positions every bar.
-- Palette slots: primary, accent.
local palette = sf.palette("techno")
local LOOP_BEATS = 8
local KICK_WIDTH_BEATS, STRIP_WIDTH_BEATS = 0.4, 0.7
local FLOOR_QUIET, FLOOR_LOUD = 0.04, 0.2
local WASH_PEAK, STRIP_PEAK, STRIP_BASE, MOVER_LEVEL = 0.9, 0.9, 0.1, 0.85
local CENTRE_PAN = 0.5
-- Pan is the offset from centre; movers mirror it left/right.
local POSITIONS = { { pan = 0.14, tilt = 0.55 }, { pan = 0.06, tilt = 0.7 } }

local function is_outer(index, count) return index == 1 or index == count end

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local floor = sf.lerp(FLOOR_QUIET, FLOOR_LOUD, energy)
        local kick = sf.on_beat(beat, KICK_WIDTH_BEATS)
        local outer_turn = math.floor(beat) % 2 == 0
        local in_bar = math.floor(sf.beat_in_bar(beat))
        local strip_hit = (in_bar == 0 or in_bar == 2) and sf.on_beat(beat, STRIP_WIDTH_BEATS) or 0
        local position = POSITIONS[sf.bar(beat) % 2 + 1]
        local reach = sf.lerp(0.5, 1, energy)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.primary),
            sf.each(sf.group("wash"), function(wash, index, count)
                local hit = is_outer(index, count) == outer_turn and kick or 0
                return sf.dimmer(wash, sf.lerp(floor, WASH_PEAK, hit) * gain)
            end),
            sf.colour(sf.group("strip"), in_bar == 2 and palette.accent or palette.primary),
            sf.dimmer(sf.group("strip"), sf.lerp(STRIP_BASE, STRIP_PEAK, strip_hit) * gain),
            sf.colour(sf.group("movers"), palette.primary),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * gain),
            sf.each(sf.group("movers"), function(mover, index)
                local pan = CENTRE_PAN + sf.side(index) * position.pan * reach
                return sf.pan_tilt(mover, pan, position.tilt)
            end)
        }
    end
}

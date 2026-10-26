-- 16-beat loop, no beat pulses or strobe.
-- energy: 2
-- family: accent
-- movement: still
-- wash: accent, one soft lub-dub per bar: a swell on the one, a hold, a higher swell
-- to beat 2, then a slow decay. Holding (not dipping) between them keeps one peak per bar.
-- strip: off.
-- movers: off. Palette slots: accent, black.
local palette = sf.palette("techno")
local LOOP_BEATS = 16
local BAR_BEATS = 4
local WASH_FLOOR, WASH_PEAK = 0.03, 0.35
local LUB_SHARE = 0.6
local LUB_END, DUB_START, DUB_END = 0.5, 1, 1.5

local function smooth(t) return t * t * (3 - 2 * t) end

local function heartbeat(b)
    if b < LUB_END then return LUB_SHARE * smooth(b / LUB_END) end
    if b < DUB_START then return LUB_SHARE end
    if b < DUB_END then
        return sf.lerp(LUB_SHARE, 1, smooth((b - DUB_START) / (DUB_END - DUB_START)))
    end
    return 1 - smooth((b - DUB_END) / (BAR_BEATS - DUB_END))
end

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local wash = sf.lerp(WASH_FLOOR, WASH_PEAK, heartbeat(sf.beat_in_bar(beat))) * level
        return sf.scene {
            sf.colour(sf.group("wash"), palette.accent),
            sf.dimmer(sf.group("wash"), wash),
            sf.colour(sf.group("strip"), palette.black),
            sf.dimmer(sf.group("strip"), 0),
            sf.colour(sf.group("movers"), palette.black),
            sf.dimmer(sf.group("movers"), 0)
        }
    end
}

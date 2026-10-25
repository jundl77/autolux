-- 16-beat loop, no beat pulses or strobe.
-- wash: near-black, accent fades in over 12 beats and back out over the last 4.
-- strip: one dim accent pixel crossfading along the strip, one step per 2 beats.
-- movers: wide, very slow mirrored sweep. Palette slots: accent, cold.
local palette = sf.palette("techno")
local LOOP_BEATS = 16
local FADE_IN_SHARE = 0.75
local WASH_FLOOR, WASH_PEAK = 0.02, 0.35
local PIXEL_LEVEL = 0.25
local MOVER_LEVEL = 0.35
local CENTRE_PAN, CENTRE_TILT = 0.5, 0.6
local PAN_SWING, TILT_SWING = 0.25, 0.08

local function fade(phase)
    if phase < FADE_IN_SHARE then return phase / FADE_IN_SHARE end
    return (1 - phase) / (1 - FADE_IN_SHARE)
end

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local level = sf.lerp(0.6, 1, energy)
        local reach = sf.lerp(0.5, 1, energy)
        local pan = PAN_SWING * reach * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        local tilt = CENTRE_TILT + TILT_SWING * reach * sf.osc.sine(beat, LOOP_BEATS, -1, 1, 0.25)
        local wash = sf.lerp(WASH_FLOOR, WASH_PEAK, fade(beat / LOOP_BEATS)) * level
        return sf.scene {
            sf.colour(sf.group("wash"), palette.accent),
            sf.dimmer(sf.group("wash"), wash),
            sf.colour(sf.group("strip"), palette.accent),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local distance = math.abs(beat / LOOP_BEATS * count - (index - 1))
                distance = math.min(distance, count - distance)
                return sf.dimmer(pixel, PIXEL_LEVEL * level * math.max(0, 1 - distance))
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.each(sf.group("movers"), function(mover, index)
                local side = index % 2 == 1 and -1 or 1
                return {
                    sf.pan_tilt(mover, CENTRE_PAN + side * pan, tilt),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

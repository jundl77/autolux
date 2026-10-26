-- 16-beat loop, no beat pulses or strobe.
-- wash: cold, near-black at 0.1.
-- strip: cold drop trickling down from pixel 8 to pixel 1, one step per beat, two passes
-- per loop; the head and the pixel it just left are lit.
-- movers: cold, small slow mirrored figure-eight. Palette slots: cold.
local palette = sf.palette("techno")
local LOOP_BEATS = 16
local WASH_LEVEL = 0.1
local HEAD_LEVEL, TAIL_LEVEL = 0.35, 0.12
local MOVER_LEVEL = 0.3
local CENTRE_PAN, CENTRE_TILT = 0.5, 0.6
local PAN_SWING, TILT_SWING = 0.06, 0.04

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local reach = sf.lerp(0.5, 1, energy)
        -- Offset 0.25 turns the sines into sin(t) and sin(2t): a figure-eight.
        local pan = PAN_SWING * reach * sf.osc.sine(beat, LOOP_BEATS, -1, 1, 0.25)
        local tilt = CENTRE_TILT + TILT_SWING * reach * sf.osc.sine(beat, LOOP_BEATS / 2, -1, 1, 0.25)
        return sf.scene {
            sf.colour(sf.all(), palette.cold),
            sf.dimmer(sf.group("wash"), WASH_LEVEL * level),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local head = count - math.floor(beat) % count
                -- Wraps, so the last drop's tail at pixel 1 overlaps the new head at pixel 8.
                local tail = head % count + 1
                local lit = index == head and HEAD_LEVEL or index == tail and TAIL_LEVEL or 0
                return sf.dimmer(pixel, lit * level)
            end),
            sf.each(sf.group("movers"), function(mover, index)
                return {
                    sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * pan, tilt),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

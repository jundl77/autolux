-- 16-beat loop, no beat pulses or strobe.
-- energy: 1
-- family: warm
-- movement: still
-- wash: low level, colour crossfades secondary -> primary -> secondary across the phrase.
-- strip: one secondary glow drifting end to end and back, crossfading between pixels.
-- movers: primary, parked wide and mirrored, tiny slow tilt breathe.
-- Palette slots: primary, secondary.
local palette = sf.palette("techno")
local LOOP_BEATS = 16
local WASH_LOW, WASH_HIGH = 0.1, 0.18
local GLOW_LEVEL = 0.22
local MOVER_LEVEL = 0.3
local CENTRE_PAN, PARK_PAN = 0.5, 0.22
local TILT, TILT_BREATHE = 0.6, 0.02

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local blend = sf.osc.sine(beat, LOOP_BEATS)
        local tilt = TILT + TILT_BREATHE * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        return sf.scene {
            sf.colour(sf.group("wash"), sf.mix(palette.secondary, palette.primary, blend)),
            sf.dimmer(sf.group("wash"), sf.osc.sine(beat, LOOP_BEATS, WASH_LOW, WASH_HIGH) * level),
            sf.colour(sf.group("strip"), palette.secondary),
            sf.each(sf.group("strip"), function(pixel, index, count)
                -- Triangle travel turns around at the ends instead of jumping back.
                local position = sf.osc.triangle(beat, LOOP_BEATS, 0, count - 1)
                local glow = math.max(0, 1 - math.abs(position - (index - 1)))
                return sf.dimmer(pixel, GLOW_LEVEL * glow * level)
            end),
            sf.colour(sf.group("movers"), palette.primary),
            sf.each(sf.group("movers"), function(mover, index)
                return {
                    sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * PARK_PAN, tilt),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

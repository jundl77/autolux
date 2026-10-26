-- 16-beat loop
-- energy: 1
-- movement: sweep
-- texture: wash
local palette = sf.palette()
local LOOP_BEATS = 16
local WASH_LOW, WASH_HIGH = 0.05, 0.25
local HAZE_SCALE = 0.4
local MOVER_LEVEL = 0.22
local CENTRE_PAN, PARK_PAN = 0.5, 0.15
local CENTRE_TILT, TILT_DRIFT = 0.6, 0.05
local STAGGER_BEATS = 4
local WASH_COUNT = 4

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local function breathe(slot)
            return sf.osc.sine(beat - slot * STAGGER_BEATS, LOOP_BEATS, WASH_LOW, WASH_HIGH)
        end
        local drift = TILT_DRIFT * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        return sf.scene {
            sf.colour(sf.all(), palette.cold),
            sf.each(sf.group("wash"), function(wash, index)
                return sf.dimmer(wash, breathe(index - 1) * level)
            end),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local slot = math.floor((index - 1) * WASH_COUNT / count)
                return sf.dimmer(pixel, HAZE_SCALE * breathe(slot) * level)
            end),
            sf.each(sf.group("movers"), function(mover, index)
                local tilt = CENTRE_TILT + sf.side(index) * drift
                return {
                    sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * PARK_PAN, tilt),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

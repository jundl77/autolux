-- 16-beat loop
-- energy: 1
-- movement: sweep
-- texture: wash
local palette = sf.palette()
local LOOP_BEATS = 16
local WASH_LOW, WASH_HIGH = 0.05, 0.25
local HAZE_SCALE = 0.4
local MOVER_LEVEL = 0.22
local PARK_DEGREES, DRIFT_DEGREES = 12, 4
local STAGGER_BEATS = 4

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local function breathe(slot)
            return sf.osc.sine(beat - slot * STAGGER_BEATS, LOOP_BEATS, WASH_LOW, WASH_HIGH)
        end
        local drift = DRIFT_DEGREES * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        local wash_count = #sf.members(sf.group("wash"))
        return sf.scene {
            sf.colour(sf.all(), palette.cold),
            sf.each(sf.group("wash"), function(wash, index)
                return sf.dimmer(wash, breathe(index - 1) * level)
            end),
            sf.each(sf.group("strip"), function(pixel, index, count)
                local slot = math.floor((index - 1) * wash_count / count)
                return sf.dimmer(pixel, HAZE_SCALE * breathe(slot) * level)
            end),
            sf.dimmer(sf.group("movers"), MOVER_LEVEL * level),
            sf.phaser(sf.group("movers"), ctx, { shape = "fan", size = PARK_DEGREES, centre_tilt = drift })
        }
    end
}

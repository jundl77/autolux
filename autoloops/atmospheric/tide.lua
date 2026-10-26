-- 16-beat loop, no beat pulses or strobe.
-- energy: 1
-- family: cold
-- movement: sweep
-- wash: faint secondary, steady.
-- strip: cold wave travelling from pixel 1 to pixel 8 once per loop, with a soft trailing tail.
-- movers: cold, slow mirrored pan sway. Palette slots: secondary, cold.
local palette = sf.palette("techno")
local LOOP_BEATS = 16
local WASH_LEVEL = 0.08
local WAVE_LEVEL, WAVE_WIDTH = 0.3, 2.5
local MOVER_LEVEL = 0.25
local CENTRE_PAN, TILT = 0.5, 0.6
local PAN_SWING = 0.07

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local energy = ctx.normalized_rms_level or 0.6
        local level = sf.loudness_gain(ctx.normalized_rms_level)
        local swing = PAN_SWING * sf.lerp(0.5, 1, energy) * sf.osc.sine(beat, LOOP_BEATS, -1, 1)
        return sf.scene {
            sf.colour(sf.group("wash"), palette.secondary),
            sf.dimmer(sf.group("wash"), WASH_LEVEL * level),
            sf.colour(sf.group("strip"), palette.cold),
            sf.each(sf.group("strip"), function(pixel, index, count)
                -- The crest starts and ends beyond the strip, so it enters and leaves dark instead of wrapping.
                local crest = sf.lerp(-WAVE_WIDTH, count - 1 + WAVE_WIDTH, beat / LOOP_BEATS)
                local behind = crest - (index - 1)
                local wave = behind >= 0 and math.max(0, 1 - behind / WAVE_WIDTH)
                    or math.max(0, 1 + 2 * behind / WAVE_WIDTH)
                return sf.dimmer(pixel, WAVE_LEVEL * wave * level)
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.each(sf.group("movers"), function(mover, index)
                return {
                    sf.pan_tilt(mover, CENTRE_PAN + sf.side(index) * swing, TILT),
                    sf.dimmer(mover, MOVER_LEVEL * level)
                }
            end)
        }
    end
}

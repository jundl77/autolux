-- 8-beat loop; time since selection ramps to a plateau over 32 bars, RMS adds the top 15%.
-- wash: primary, a kick envelope on every beat whose depth grows with the ramp.
-- strip: secondary, odd and even pixels trading beats in step with the movers.
-- movers: cold, mirrored, tilting up on beats 1/3 and down on 2/4, travel growing with the ramp.
-- Palette slots: primary, secondary, cold.
local palette = sf.palette("techno")
local LOOP_BEATS = 8
local RAMP_BEATS = 128
local RAMP_SHARE = 0.85
local WASH_PEAK = 0.9
local KICK_WIDTH_BEATS = 0.4
local STRIP_REST = 0.15
local CENTRE_PAN, CENTRE_TILT, SPREAD = 0.5, 0.6, 0.12
local START_TRAVEL, END_TRAVEL = 0.03, 0.15

return {
    render = function(ctx)
        local beat = ctx.beat_position % LOOP_BEATS
        local ramp = math.min(ctx.autoloop_elapsed_beats / RAMP_BEATS, 1)
        local energy = ctx.normalized_rms_level or 0.6
        local drive = ramp * RAMP_SHARE + energy * (1 - RAMP_SHARE)
        local gain = sf.loudness_gain(ctx.normalized_rms_level)
        local depth = sf.lerp(0.15, 0.9, ramp)
        local kick = sf.on_beat(beat, KICK_WIDTH_BEATS)
        local wash = sf.lerp(0.35, WASH_PEAK, drive) * (1 - depth * (1 - kick)) * gain
        -- +1 on beats 1 and 3, -1 on 2 and 4; a triangle glides between so the heads never snap.
        local stroke = sf.osc.triangle(beat, 2, 1, -1)
        local travel = sf.lerp(START_TRAVEL, END_TRAVEL, ramp) * sf.lerp(0.5, 1, energy)
        local up_beat = math.floor(beat) % 2 == 0
        return sf.scene({
            sf.colour(sf.group("wash"), palette.primary),
            sf.dimmer(sf.group("wash"), wash),
            sf.colour(sf.group("strip"), palette.secondary),
            sf.each(sf.group("strip"), function(pixel, index)
                local lit = (index % 2 == 1) == up_beat and 1 or STRIP_REST
                return sf.dimmer(pixel, sf.lerp(0.2, 0.8, drive) * gain * lit)
            end),
            sf.colour(sf.group("movers"), palette.cold),
            sf.dimmer(sf.group("movers"), sf.lerp(0.3, 0.85, drive) * gain),
            sf.each(sf.group("movers"), function(mover, index)
                local pan = CENTRE_PAN + sf.side(index) * SPREAD * sf.lerp(0.5, 1, energy)
                return sf.pan_tilt(mover, pan, CENTRE_TILT - travel * stroke)
            end)
        })
    end
}

-- The other bar-8 fill: the beams cross through the centre and back, flashing on each beat.
-- strobe: no
-- intensity: ltp
local palette = sf.palette()
local CROSS_DEGREES, FLASH_WIDTH_BEATS = 20, 0.4
local FLOOR, PEAK = 0.4, 0.9

return {
    render = function(ctx)
        local progress = math.min(1, ctx.autoloop_elapsed_beats / (ctx.overlay_length_beats or 1))
        local movers = sf.group("movers")
        return sf.scene {
            sf.colour(movers, palette.secondary),
            sf.dimmer(movers, sf.lerp(FLOOR, PEAK, sf.on_beat(ctx, FLASH_WIDTH_BEATS))),
            sf.phaser(movers, ctx, { shape = "cross", size = CROSS_DEGREES * math.sin(math.pi * progress) })
        }
    end
}

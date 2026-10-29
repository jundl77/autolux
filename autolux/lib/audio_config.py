SAMPLE_RATE = 44100
BUFFER_SIZE = 256
# How far the room's audio trails the analysis. The one knob: production sends it to
# Radius TX at login, the engine schedules against it and simulation replays with it.
# It must exceed the chain latency (~13.7 s) by the lead cues need to book a riser.
PLAYBACK_DELAY_SEC = 24.0

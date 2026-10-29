mod support;

use std::collections::BTreeMap;
use std::time::Instant;

use serde_json::json;
use starfire::engine::{Engine, RenderedFrame, perceptual_to_linear_master};
use starfire::fixtures::FixtureRig;
use starfire::intensity::STROBE_GATE_INTENSITY;
use starfire::model::{
    AttributeValue, Command, FixtureState, LightingOverride, OverlayCue, OverrideLifetime,
    PropertyValues, Rgb, RotationEntry, SectionCue, SectionIntent, Target, Transition,
};
use starfire::protocol::wire;
use starfire::script::AutoloopCatalog;

const SECONDS_PER_BEAT: f64 = 0.5;
const FRAME_RATE_HZ: f64 = 40.0;
const RED: Rgb = Rgb {
    red: 1.0,
    green: 0.0,
    blue: 0.0,
};
const BLUE: Rgb = Rgb {
    red: 0.0,
    green: 0.0,
    blue: 1.0,
};
const WHITE: Rgb = Rgb {
    red: 1.0,
    green: 1.0,
    blue: 1.0,
};

const BASE: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), 0.5), sf.colour(sf.all(), sf.rgb(1, 0, 0)),
    sf.pan_tilt(sf.group("movers"), 0.2, 0.4)} end}"#;
const BRIGHT: &str = "return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), 0.8)} end}";
const DARK: &str = "return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), 0)} end}";
const UNEVEN: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), 0.8), sf.dimmer(sf.fixture("wash_left_outer"), 0.2)} end}"#;
const CHASE: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.group("movers"), sf.osc.pulse(ctx.beat_position, 2, 0.1, 0.9)),
    sf.pan_tilt(sf.group("movers"), 0.2, 0.4),
    sf.physical_attribute(sf.group("movers"), "Gobo1", 0)} end}"#;
const TINT: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.group("wash"), 0.25), sf.colour(sf.all(), sf.rgb(0, 0, 1)),
    {target = sf.group("movers"), values = {pan = 1}}, sf.strobe(sf.all(), 4)} end}"#;
const HIT: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), 1 - ctx.autoloop_elapsed_beats / ctx.overlay_length_beats),
    sf.colour(sf.all(), sf.rgb(1, 1, 1)), sf.pan_tilt(sf.group("movers"), 1, 0.4)} end}"#;
const BEAM: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.group("movers"), 0.5), sf.pan_tilt(sf.group("movers"), 0.8, 0.6),
    sf.physical_attribute(sf.group("movers"), "Gobo1", 200)} end}"#;
const STROBE: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.group("wash"), 0.3), sf.strobe(sf.group("wash"), 5)} end}"#;
const FADE: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.group("wash"), 1 - ctx.autoloop_elapsed_beats / 8)} end}"#;
const QUARTER: &str =
    r#"return {render=function(ctx) return sf.scene {sf.dimmer(sf.group("wash"), 0.25)} end}"#;
const THREE_QUARTERS: &str =
    r#"return {render=function(ctx) return sf.scene {sf.dimmer(sf.group("wash"), 0.75)} end}"#;
const FULL: &str =
    r#"return {render=function(ctx) return sf.scene {sf.dimmer(sf.group("wash"), 1)} end}"#;
const FULL_BLUE: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), 1), sf.colour(sf.all(), sf.rgb(0, 0, 1))} end}"#;
const LEFT_FULL: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.fixture("wash_left_outer"), 1)} end}"#;
const RIGHT_FULL: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.fixture("wash_right_outer"), 1)} end}"#;
const BREAKS_AT_TWO: &str = r#"return {render=function(ctx)
    if ctx.autoloop_elapsed_beats >= 2 then error("overlay broke") end
    return sf.scene {sf.dimmer(sf.fixture("wash_left_outer"), 1)} end}"#;
const BEFORE: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), 1), sf.colour(sf.all(), sf.rgb(1, 0, 0)),
    sf.pan_tilt(sf.group("movers"), 0.2, 0.3),
    sf.physical_attribute(sf.group("movers"), "Gobo1", 0)} end}"#;
const AFTER: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), 1), sf.colour(sf.all(), sf.rgb(0, 0, 1)),
    sf.pan_tilt(sf.group("movers"), 0.9, 0.7),
    sf.physical_attribute(sf.group("movers"), "Gobo1", 128)} end}"#;
const AFTER_RIGHT: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), 1), sf.pan_tilt(sf.fixture("mover_right"), 0.9, 0.7)} end}"#;
const AFTER_BARS: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), 1), sf.pan_tilt(sf.group("movers"), 0.1 + ctx.bar_index / 10,
        0.2 + ctx.bar_phase / 10)} end}"#;
const DIP: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.fixture("mover_left"), 0)} end}"#;
const DARK_LEFT_MOVER: &str = DIP;

const HTP: [&str; 5] = ["hit", "right_full", "beam", "strobe", "heavy"];

const SCRIPTS: [(&str, &str); 25] = [
    ("base", BASE),
    ("bright", BRIGHT),
    ("dark", DARK),
    ("uneven", UNEVEN),
    ("chase", CHASE),
    ("tint", TINT),
    ("hit", HIT),
    ("beam", BEAM),
    ("strobe", STROBE),
    ("fade", FADE),
    ("quarter", QUARTER),
    ("three_quarters", THREE_QUARTERS),
    ("dip/three_quarters_dip", THREE_QUARTERS),
    ("hit/quarter_hit", QUARTER),
    ("full", FULL),
    ("full_blue", FULL_BLUE),
    ("left_full", LEFT_FULL),
    ("right_full", RIGHT_FULL),
    ("breaks_at_two", BREAKS_AT_TWO),
    ("before", BEFORE),
    ("after", AFTER),
    ("after_right", AFTER_RIGHT),
    ("after_bars", AFTER_BARS),
    ("dip/dip", DIP),
    ("fill/dark_left_mover", DARK_LEFT_MOVER),
];

fn identity(id: &str) -> (String, String) {
    let (bank, autoloop) = id.split_once('/').unwrap_or(("test", id));
    (bank.to_owned(), autoloop.to_owned())
}

fn engine_on(rig: FixtureRig, scripts: &[(String, String)]) -> Engine {
    let catalog = AutoloopCatalog::from_definitions(
        &scripts
            .iter()
            .map(|(id, source)| {
                let (bank, autoloop) = identity(id);
                wire::AutoloopDefinition {
                    intensity_htp: HTP.iter().any(|htp| autoloop.starts_with(htp)),
                    autoloop_id: autoloop,
                    bank,
                    lua_source: source.clone(),
                }
            })
            .collect::<Vec<_>>(),
    )
    .unwrap();
    let mut engine = Engine::new(rig, catalog, support::palettes()).unwrap();
    engine
        .apply(0.0, &support::beat(0, Some(0), 120.0))
        .unwrap();
    engine
}

fn select(engine: &mut Engine, id: &str) {
    let (bank, autoloop) = identity(id);
    engine
        .apply(0.0, &support::select(&bank, &autoloop, Transition::Cut))
        .unwrap();
}

fn file_scripts() -> Vec<(String, String)> {
    SCRIPTS
        .iter()
        .map(|(id, source)| ((*id).to_owned(), (*source).to_owned()))
        .collect()
}

fn playing(base: &str) -> Engine {
    playing_on(support::stage(), base)
}

fn playing_on(rig: FixtureRig, base: &str) -> Engine {
    let mut engine = engine_on(rig, &file_scripts());
    select(&mut engine, base);
    engine
}

fn at(beat: f64) -> f64 {
    beat * SECONDS_PER_BEAT
}

fn overlay(id: &str, begin_beats: f64, length_beats: f64) -> OverlayCue {
    let (bank, autoloop) = identity(id);
    OverlayCue {
        bank,
        autoloop,
        priority: 0,
        begin_beats,
        length_beats,
        every_bars: 0,
        attack_beats: 0.0,
        release_beats: 0.0,
    }
}

fn enveloped(id: &str, length_beats: f64, attack_beats: f64, release_beats: f64) -> OverlayCue {
    OverlayCue {
        attack_beats,
        release_beats,
        ..overlay(id, 0.0, length_beats)
    }
}

fn dip(beats: f64, attack_beats: f64) -> OverlayCue {
    OverlayCue {
        attack_beats,
        ..overlay("dip/dip", -beats, beats)
    }
}

fn cue(start: u64, base: &str, transition: Transition, overlays: Vec<OverlayCue>) -> SectionCue {
    let (bank, autoloop) = identity(base);
    SectionCue {
        start_beat_index: start,
        intent: SectionIntent::Drop,
        rotation: vec![RotationEntry {
            bank,
            autoloop,
            bars: 4,
            transition,
        }],
        overlays,
        palette: "techno".into(),
        drop_strength: None,
        seed: 0,
    }
}

fn send_cue(engine: &mut Engine, beat: f64, cue: SectionCue) {
    engine.apply(at(beat), &support::cue(cue)).unwrap();
}

fn send(engine: &mut Engine, beat: f64, start: u64, base: &str, overlays: Vec<OverlayCue>) {
    send_cue(engine, beat, cue(start, base, Transition::Cut, overlays));
}

fn beat_message(engine: &mut Engine, seconds: f64, index: u64, bar_phase: Option<u32>) {
    engine
        .apply(seconds, &support::beat(index, bar_phase, 120.0))
        .unwrap();
}

fn render(engine: &mut Engine, beat: f64) -> RenderedFrame {
    let frame = engine.render_at(at(beat)).unwrap();
    assert_eq!(frame.script_error, None, "beat {beat}");
    frame
}

fn state<'a>(frame: &'a RenderedFrame, label: &str) -> &'a FixtureState {
    &frame.fixture_properties_before_strobe[&support::stage_uuid(label)]
}

fn near(actual: f64, expected: f64, what: &str) {
    assert!(
        (actual - expected).abs() < 1e-9,
        "{what}: {actual} != {expected}"
    );
}

fn near_colour(actual: Rgb, expected: Rgb, what: &str) {
    near(actual.red, expected.red, what);
    near(actual.green, expected.green, what);
    near(actual.blue, expected.blue, what);
}

fn pink(white: f64) -> Rgb {
    Rgb {
        red: 1.0,
        green: white,
        blue: white,
    }
}

fn number(value: f64) -> Option<AttributeValue> {
    Some(AttributeValue::Number(value))
}

fn attribute(state: &FixtureState, key: &str) -> Option<AttributeValue> {
    state.attributes.get(key).cloned()
}

struct Slots {
    universe: u16,
    slots: Vec<usize>,
    low: Vec<u8>,
    high: Vec<u8>,
}

impl Slots {
    fn of(label: &str, set: impl Fn(&mut FixtureState, bool)) -> Self {
        let rig = support::stage();
        let encode = |high| {
            let mut states = rig.empty_states();
            let state = states.get_mut(&support::stage_uuid(label)).unwrap();
            state.intensity = 1.0;
            state.colour = WHITE;
            set(state, high);
            rig.encode(&states).unwrap()
        };
        let (low, high) = (encode(false), encode(true));
        let universe = *low.keys().next().unwrap();
        let slots: Vec<usize> = (0..low[&universe].len())
            .filter(|&slot| low[&universe][slot] != high[&universe][slot])
            .collect();
        assert!(!slots.is_empty(), "{label}");
        Self {
            universe,
            low: slots.iter().map(|&slot| low[&universe][slot]).collect(),
            high: slots.iter().map(|&slot| high[&universe][slot]).collect(),
            slots,
        }
    }

    fn wheel(label: &str) -> Self {
        let slots = Self::of(label, |state, blue| {
            state.colour = if blue { BLUE } else { RED }
        });
        assert_eq!(slots.slots.len(), 1, "{label}: one wheel byte");
        slots
    }

    fn light(label: &str) -> Self {
        Self::of(label, |state, lit| {
            state.intensity = if lit { 1.0 } else { 0.0 }
        })
    }

    fn in_frame(&self, frame: &RenderedFrame) -> Vec<u8> {
        let bytes = &frame.dmx_frames_by_universe[&self.universe];
        self.slots.iter().map(|&slot| bytes[slot]).collect()
    }

    fn is_low(&self, frame: &RenderedFrame) -> bool {
        self.in_frame(frame) == self.low
    }

    fn is_high(&self, frame: &RenderedFrame) -> bool {
        self.in_frame(frame) == self.high
    }
}

#[test]
fn an_ltp_overlay_blends_each_property_it_sets_by_its_level_and_leaves_the_rest() {
    let mut engine = playing("base");
    send(
        &mut engine,
        0.0,
        4,
        "base",
        vec![enveloped("tint", 8.0, 4.0, 0.0)],
    );
    for (beat, level) in [(4.0, 0.0), (5.0, 0.25), (6.0, 0.5), (8.0, 1.0)] {
        let frame = render(&mut engine, beat);
        let what = format!("beat {beat}");
        let wash = state(&frame, "wash_left_outer");
        near(wash.intensity, 0.5 - 0.25 * level, &what);
        near_colour(wash.colour, RED.lerp(BLUE, level), &what);
        near(wash.strobe_hz, if level < 0.5 { 0.0 } else { 4.0 }, &what);
        let mover = state(&frame, "mover_left");
        near(mover.pan, 0.2 + 0.8 * level, &what);
        near(mover.tilt, 0.4, &what);
        near(mover.intensity, 0.5, &what);
        near_colour(mover.colour, if level < 0.5 { RED } else { BLUE }, &what);
    }
}

#[test]
fn an_htp_overlay_mixes_colour_by_its_share_of_the_light_and_owns_the_rest_from_the_start() {
    let mut engine = playing("base");
    send(&mut engine, 0.0, 4, "base", vec![overlay("hit", 0.0, 8.0)]);
    for (elapsed, intensity, share) in [
        (0.0, 1.0, 1.0),
        (2.0, 0.75, 1.0),
        (4.0, 0.5, 1.0),
        (6.0, 0.5, 0.5),
        (7.0, 0.5, 0.25),
    ] {
        let frame = render(&mut engine, 4.0 + elapsed);
        let what = format!("elapsed {elapsed}");
        let wash = state(&frame, "wash_left_outer");
        near(wash.intensity, intensity, &what);
        near_colour(wash.colour, pink(share), &what);
        let mover = state(&frame, "mover_left");
        near(mover.pan, 1.0, &what);
        near_colour(mover.colour, WHITE, &what);
    }
    let mut engine = playing("dark");
    send(&mut engine, 0.0, 4, "dark", vec![overlay("hit", 0.0, 8.0)]);
    render(&mut engine, 4.0);
    let frame = render(&mut engine, 10.0);
    let wash = state(&frame, "wash_left_outer");
    near(wash.intensity, 0.25, "dark base");
    near_colour(wash.colour, WHITE, "dark base");
}

#[test]
fn heads_and_gobos_under_an_htp_overlay_never_flip_with_a_dimmer_chase_below() {
    let mut engine = playing("chase");
    send(
        &mut engine,
        0.0,
        4,
        "chase",
        vec![overlay("beam", 0.0, 8.0)],
    );
    for frame in (at(4.0) * FRAME_RATE_HZ) as usize..(at(12.0) * FRAME_RATE_HZ) as usize {
        let rendered = engine.render_at(frame as f64 / FRAME_RATE_HZ).unwrap();
        let mover = state(&rendered, "mover_left");
        let what = format!("frame {frame}");
        assert!(mover.intensity == 0.5 || mover.intensity == 0.9, "{what}");
        near(mover.pan, 0.8, &what);
        near(mover.tilt, 0.6, &what);
        assert_eq!(attribute(mover, "Gobo1"), number(200.0), "{what}");
    }
}

#[test]
fn an_htp_strobe_starts_on_its_own_first_frame_and_flashes_in_step_over_uneven_bases() {
    let mut engine = playing("uneven");
    send(
        &mut engine,
        0.0,
        4,
        "uneven",
        vec![overlay("strobe", 0.0, 8.0)],
    );
    let (low, high) = (
        Slots::light("wash_left_outer"),
        Slots::light("wash_right_outer"),
    );
    let frames: Vec<RenderedFrame> = ((at(4.0) * FRAME_RATE_HZ) as usize
        ..(at(6.0) * FRAME_RATE_HZ) as usize)
        .map(|frame| engine.render_at(frame as f64 / FRAME_RATE_HZ).unwrap())
        .collect();
    near(
        state(&frames[0], "wash_right_outer").strobe_hz,
        5.0,
        "first",
    );
    near(
        state(&frames[0], "wash_right_outer").intensity,
        0.8,
        "first",
    );
    near(state(&frames[0], "wash_left_outer").intensity, 0.3, "first");
    let gated: Vec<(bool, bool)> = frames
        .iter()
        .map(|frame| (low.is_low(frame), high.is_low(frame)))
        .collect();
    assert!(gated.iter().all(|(left, right)| left == right), "{gated:?}");
    assert!(gated.iter().any(|(dark, _)| *dark), "it strobes");
    assert!(gated.iter().any(|(dark, _)| !dark), "it flashes");
}

#[test]
fn past_the_burst_cap_an_overlay_strobe_is_refused_and_the_base_plays_on() {
    let mut engine = playing("base");
    engine
        .apply(0.0, &Command::SetStrobeMaxBurstSeconds { seconds: 1.0 })
        .unwrap();
    send(
        &mut engine,
        0.0,
        4,
        "base",
        vec![overlay("strobe", 0.0, 8.0)],
    );
    let light = Slots::light("wash_left_outer");
    for frame in (at(4.0) * FRAME_RATE_HZ) as usize..(at(8.0) * FRAME_RATE_HZ) as usize {
        let seconds = frame as f64 / FRAME_RATE_HZ;
        let rendered = engine.render_at(seconds).unwrap();
        let wash = state(&rendered, "wash_left_outer");
        let what = format!("{seconds} s");
        if seconds < at(4.0) + 1.0 {
            near(wash.strobe_hz, 5.0, &what);
        } else {
            near(wash.strobe_hz, 0.0, &what);
            near(wash.intensity, 0.5, &what);
            assert!(!light.is_low(&rendered), "{what}: the base stays lit");
        }
    }
}

#[test]
fn at_the_strobe_gate_an_uncapped_overlay_strobe_is_refused_and_the_base_plays_on() {
    let mut engine = playing("base");
    send(&mut engine, 0.0, 4, "base", vec![overlay("tint", 0.0, 8.0)]);
    let frame = render(&mut engine, 4.0);
    near(
        state(&frame, "wash_left_outer").strobe_hz,
        4.0,
        "above the gate",
    );
    engine
        .apply(
            at(4.5),
            &Command::SetIntensity {
                level: STROBE_GATE_INTENSITY,
            },
        )
        .unwrap();
    let frame = render(&mut engine, 4.5);
    let wash = state(&frame, "wash_left_outer");
    near(wash.strobe_hz, 0.0, "at the gate");
    near(wash.intensity, 0.5, "at the gate");
    near_colour(wash.colour, RED, "at the gate");
}

#[test]
fn blackout_darkens_an_overlay_with_everything_else() {
    let mut engine = playing("base");
    send(
        &mut engine,
        0.0,
        4,
        "base",
        vec![overlay("strobe", 0.0, 8.0)],
    );
    render(&mut engine, 4.0);
    engine
        .apply(at(4.5), &Command::SetBlackout { enabled: true })
        .unwrap();
    let frame = render(&mut engine, 5.0);
    let light = Slots::light("wash_left_outer");
    for state in frame.fixture_properties_before_strobe.values() {
        near(state.intensity, 0.0, "blackout");
        near(state.strobe_hz, 0.0, "blackout");
    }
    assert!(light.is_low(&frame));
}

#[test]
fn higher_priority_composes_later_and_ties_keep_list_order() {
    let row = |id: &str, priority: i32| OverlayCue {
        priority,
        ..overlay(id, 0.0, 8.0)
    };
    for (rows, expected) in [
        (vec![row("quarter", 1), row("three_quarters", 0)], 0.25),
        (vec![row("three_quarters", 1), row("quarter", 0)], 0.75),
        (vec![row("quarter", 0), row("three_quarters", 0)], 0.75),
        (vec![row("three_quarters", 0), row("quarter", 0)], 0.25),
        (
            vec![row("dip/three_quarters_dip", 0), row("hit/quarter_hit", 0)],
            0.25,
        ),
        (
            vec![row("hit/quarter_hit", 1), row("dip/three_quarters_dip", 0)],
            0.25,
        ),
    ] {
        let mut engine = playing("base");
        send(&mut engine, 0.0, 4, "base", rows);
        render(&mut engine, 4.0);
        let frame = render(&mut engine, 5.0);
        near(
            state(&frame, "wash_left_outer").intensity,
            expected,
            "top layer",
        );
    }
}

#[test]
fn envelopes_attack_from_the_first_frame_release_from_the_level_reached_and_then_drop() {
    let cases: [(_, &[(f64, f64)]); 3] = [
        (
            (2.0, 2.0, 4.0),
            &[
                (0.0, 0.0),
                (1.0, 0.5),
                (2.0, 1.0),
                (3.0, 1.0),
                (4.0, 1.0),
                (5.0, 0.5),
                (6.0, 0.0),
            ],
        ),
        (
            (0.0, 0.0, 4.0),
            &[(0.0, 1.0), (3.75, 1.0), (4.0, 0.0), (5.0, 0.0)],
        ),
        (
            (8.0, 2.0, 4.0),
            &[(0.0, 0.0), (2.0, 0.25), (4.0, 0.5), (5.0, 0.25), (6.0, 0.0)],
        ),
    ];
    for ((attack, release, length), samples) in cases {
        let mut engine = playing("dark");
        send(
            &mut engine,
            0.0,
            4,
            "dark",
            vec![enveloped("full", length, attack, release)],
        );
        for &(after, level) in samples {
            let frame = render(&mut engine, 4.0 + after);
            let what = format!("attack {attack} release {release} at {after}");
            near(state(&frame, "wash_left_outer").intensity, level, &what);
            if after >= length + release {
                assert_eq!(engine.active_overlay_count(), 0, "{what}: dropped");
            }
        }
    }
    let mut engine = playing("dark");
    let late = OverlayCue {
        attack_beats: 2.0,
        ..overlay("full", -4.0, 4.0)
    };
    send(&mut engine, 6.0, 8, "dark", vec![late]);
    for (beat, level) in [(6.0, 0.0), (7.0, 0.5), (7.5, 0.75)] {
        let frame = render(&mut engine, beat);
        near(
            state(&frame, "wash_left_outer").intensity,
            level,
            &format!("late at {beat}"),
        );
    }
}

#[test]
fn a_replaced_cue_releases_its_overlays_from_the_level_they_reached() {
    let mut engine = playing("dark");
    send(
        &mut engine,
        0.0,
        4,
        "dark",
        vec![enveloped("full", 32.0, 16.0, 4.0)],
    );
    send(&mut engine, 0.0, 12, "dark", vec![]);
    render(&mut engine, 4.0);
    near(
        state(&render(&mut engine, 8.0), "wash_left_outer").intensity,
        0.25,
        "attacking",
    );
    for (beat, level) in [(12.0, 0.5), (13.0, 0.375), (14.0, 0.25), (16.0, 0.0)] {
        let frame = render(&mut engine, beat);
        near(
            state(&frame, "wash_left_outer").intensity,
            level,
            &format!("released, beat {beat}"),
        );
    }
    assert_eq!(engine.active_overlay_count(), 0);
}

#[test]
fn a_new_overlay_composes_above_one_still_releasing() {
    let mut engine = playing("dark");
    send(
        &mut engine,
        0.0,
        4,
        "dark",
        vec![enveloped("fade", 32.0, 0.0, 4.0)],
    );
    send(
        &mut engine,
        0.0,
        8,
        "dark",
        vec![overlay("quarter", 0.0, 8.0)],
    );
    for beat in [4.0, 8.0] {
        render(&mut engine, beat);
    }
    let frame = render(&mut engine, 8.5);
    assert_eq!(engine.active_overlay_count(), 2);
    near(state(&frame, "wash_left_outer").intensity, 0.25, "on top");
}

fn zoom_head() -> wire::FixtureProfile {
    wire::FixtureProfile {
        profile_id: String::new(),
        dmx_mode: "Zoom".into(),
        mode: Some(wire::DmxMode {
            channels: vec![wire::DmxChannel {
                geometry_path: vec!["Base".into()],
                offsets: vec![1],
                default: 128,
                functions: vec![wire::ChannelFunction {
                    path: "Base_Zoom.Zoom.Zoom".into(),
                    attribute: "Zoom".into(),
                    qualified_attribute: "Base.Zoom".into(),
                    physical_from: 10.0,
                    physical_to: 40.0,
                    ..Default::default()
                }],
            }],
        }),
    }
}

fn stage_with_beams() -> FixtureRig {
    let mut definition = support::stage_definition();
    let added = support::definition(&json!({"fixtures": [
        {"fixture_uuid": "scanner", "profile": "scanner", "universe": 1, "address": 1},
        {"fixture_uuid": "zoom_set", "profile": "zoom", "universe": 1, "address": 7},
        {"fixture_uuid": "zoom_free", "profile": "zoom", "universe": 1, "address": 8}]}));
    definition.fixtures.extend(added.fixtures);
    definition
        .profiles
        .push(support::compiled_profile("beam_scanner#6ch", "scanner"));
    definition.profiles.push(wire::FixtureProfile {
        profile_id: "zoom".into(),
        ..zoom_head()
    });
    FixtureRig::from_wire(&definition).unwrap()
}

#[test]
fn an_overlay_fades_numbers_in_from_below_or_the_gdtf_default_and_steps_the_rest_half_way() {
    let rig = stage_with_beams();
    let rest = rig
        .profile("zoom_free")
        .unwrap()
        .functions()
        .into_iter()
        .find(|function| function.key == "Zoom")
        .unwrap()
        .rest;
    assert!(rest > 10.0, "a default distinct from the range start");
    let preset = |name: &str| Some(AttributeValue::Preset(name.to_owned()));
    let base = r#"return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), 1),
        sf.physical_attribute(sf.group("movers"), "Gobo1", 0),
        sf.physical_attribute(sf.fixture("zoom_set"), "Zoom", 10),
        sf.channel_preset(sf.fixture("scanner"), "Gobo1", "Open")} end}"#;
    let turn = r#"return {render=function(ctx) return sf.scene {
        sf.physical_attribute(sf.group("movers"), "Gobo1", 128),
        sf.physical_attribute(sf.fixture("zoom_set"), "Zoom", 40),
        sf.physical_attribute(sf.fixture("zoom_free"), "Zoom", 40),
        sf.channel_preset(sf.fixture("scanner"), "Gobo1", "Lines")} end}"#;
    let mut scripts = file_scripts();
    scripts.extend([
        ("beam_base".to_owned(), base.to_owned()),
        ("turn".to_owned(), turn.to_owned()),
    ]);
    let mut engine = engine_on(rig, &scripts);
    select(&mut engine, "beam_base");
    send(
        &mut engine,
        0.0,
        4,
        "beam_base",
        vec![enveloped("turn", 8.0, 4.0, 0.0)],
    );
    let mover = support::stage_uuid("mover_left");
    for (beat, level) in [(4.0, 0.0), (5.0, 0.25), (6.0, 0.5), (8.0, 1.0)] {
        let frame = engine.render_at(at(beat)).unwrap();
        let states = &frame.fixture_properties_before_strobe;
        let what = format!("beat {beat}");
        let stepped = level >= 0.5;
        assert_eq!(
            attribute(&states["zoom_set"], "Zoom"),
            number(10.0 + 30.0 * level),
            "{what}"
        );
        let free = attribute(&states["zoom_free"], "Zoom");
        if level > 0.0 {
            assert_eq!(free, number(rest + (40.0 - rest) * level), "{what}");
        } else {
            assert_eq!(free, None, "{what}");
        }
        assert_eq!(
            attribute(&states[&mover], "Gobo1"),
            number(if stepped { 128.0 } else { 0.0 }),
            "{what}"
        );
        assert_eq!(
            attribute(&states["scanner"], "Gobo1"),
            preset(if stepped { "Lines" } else { "Open" }),
            "{what}"
        );
    }
}

#[test]
fn overrides_and_masters_compose_above_every_overlay() {
    let mut engine = playing("base");
    send(
        &mut engine,
        0.0,
        4,
        "base",
        vec![overlay("full_blue", 0.0, 16.0)],
    );
    render(&mut engine, 4.0);
    let frame = render(&mut engine, 5.0);
    near(state(&frame, "wash_left_outer").intensity, 1.0, "overlay");
    near_colour(state(&frame, "wash_left_outer").colour, BLUE, "overlay");

    engine
        .apply(
            at(5.5),
            &Command::SetOverride {
                value: static_look(
                    Target::All,
                    PropertyValues {
                        intensity: Some(0.3),
                        colour: Some(RED),
                        ..Default::default()
                    },
                ),
            },
        )
        .unwrap();
    let frame = render(&mut engine, 6.0);
    near(state(&frame, "wash_left_outer").intensity, 0.3, "static");
    near_colour(state(&frame, "wash_left_outer").colour, RED, "static");

    engine
        .apply(
            at(6.5),
            &Command::ClearOverride {
                id: "static".into(),
                instantaneous_release: true,
            },
        )
        .unwrap();
    engine
        .apply(
            at(6.5),
            &Command::SetGroupMaster {
                group: "wash".into(),
                level: 0.5,
            },
        )
        .unwrap();
    let frame = render(&mut engine, 7.0);
    near(
        state(&frame, "wash_left_outer").intensity,
        perceptual_to_linear_master(0.5),
        "submaster",
    );
    near(
        state(&frame, "mover_left").intensity,
        1.0,
        "outside the submaster",
    );
}

fn static_look(target: Target, values: PropertyValues) -> LightingOverride {
    LightingOverride {
        id: "static".into(),
        target,
        values,
        intensity_multiplier: None,
        lifetime: OverrideLifetime::UntilCleared,
        priority: 1100,
        special_fx: None,
        pan_sweep: false,
        aim_focus: false,
        pose: None,
    }
}

#[test]
fn a_failing_overlay_stops_alone_and_never_darkens_the_show() {
    let mut engine = playing("base");
    send(
        &mut engine,
        0.0,
        4,
        "base",
        vec![
            overlay("breaks_at_two", 0.0, 8.0),
            overlay("right_full", 0.0, 8.0),
        ],
    );
    render(&mut engine, 4.0);
    let frame = render(&mut engine, 5.0);
    near(state(&frame, "wash_left_outer").intensity, 1.0, "both");
    assert_eq!(engine.active_overlay_count(), 2);
    assert_eq!(engine.overlay_error(), None);
    for beat in [6.5, 7.0, 9.0] {
        let frame = render(&mut engine, beat);
        let what = format!("beat {beat}");
        near(state(&frame, "wash_left_outer").intensity, 0.5, &what);
        near(state(&frame, "wash_right_outer").intensity, 1.0, &what);
        assert_eq!(engine.active_overlay_count(), 1, "{what}");
        let error = engine.overlay_error().unwrap();
        assert!(
            error.contains("test/breaks_at_two") && error.contains("overlay broke"),
            "{error}"
        );
    }
}

#[test]
fn a_failing_recurring_overlay_stays_stopped_for_the_rest_of_its_cue() {
    let mut engine = playing("base");
    let every_bar = OverlayCue {
        every_bars: 1,
        ..overlay("breaks_at_two", 0.0, 3.0)
    };
    send(&mut engine, 0.0, 4, "base", vec![every_bar]);
    for beat in [4.0, 6.5] {
        render(&mut engine, beat);
    }
    assert_eq!(engine.active_overlay_count(), 0, "stopped");
    for beat in [8.0, 9.0, 12.0, 16.0] {
        let frame = render(&mut engine, beat);
        assert_eq!(engine.active_overlay_count(), 0, "beat {beat}");
        near(state(&frame, "wash_left_outer").intensity, 0.5, "base");
    }
}

#[test]
fn pause_drops_a_releasing_overlay_and_keeps_a_running_one() {
    let mut engine = playing("base");
    let short = OverlayCue {
        release_beats: 8.0,
        ..overlay("left_full", 0.0, 1.0)
    };
    send(
        &mut engine,
        0.0,
        4,
        "base",
        vec![short, overlay("right_full", 0.0, 16.0)],
    );
    render(&mut engine, 4.0);
    let frame = render(&mut engine, 6.0);
    near(
        state(&frame, "wash_left_outer").intensity,
        0.5 + 0.5 * 0.875,
        "releasing",
    );
    assert_eq!(engine.active_overlay_count(), 2);
    engine
        .apply(at(6.0), &Command::SetPaused { paused: true })
        .unwrap();
    render(&mut engine, 6.5);
    assert_eq!(engine.active_overlay_count(), 1);
    engine
        .apply(at(7.0), &Command::SetPaused { paused: false })
        .unwrap();
    let frame = render(&mut engine, 7.5);
    near(
        state(&frame, "wash_left_outer").intensity,
        0.5,
        "release gone",
    );
    near(
        state(&frame, "wash_right_outer").intensity,
        1.0,
        "running kept",
    );
}

#[test]
fn a_dipped_head_moves_in_black_before_the_cue_and_a_lit_wheel_holds_its_slot() {
    let (dipped, lit) = (Slots::wheel("mover_left"), Slots::wheel("mover_right"));
    let mut engine = playing("before");
    send(&mut engine, 0.0, 8, "after", vec![dip(2.0, 1.0)]);
    for beat in [5.0, 6.0, 6.5] {
        let frame = render(&mut engine, beat);
        let what = format!("beat {beat}");
        let head = state(&frame, "mover_left");
        assert!(head.intensity > 0.0, "{what}");
        near(head.pan, 0.2, &what);
        near(head.tilt, 0.3, &what);
        assert_eq!(attribute(head, "Gobo1"), number(0.0), "{what}");
        assert!(dipped.is_low(&frame), "{what}: red");
    }
    for beat in [7.0, 7.75] {
        let frame = render(&mut engine, beat);
        let what = format!("beat {beat}");
        let head = state(&frame, "mover_left");
        near(head.intensity, 0.0, &what);
        near(head.pan, 0.9, &what);
        near(head.tilt, 0.7, &what);
        assert_eq!(attribute(head, "Gobo1"), number(128.0), "{what}");
        assert!(dipped.is_high(&frame), "{what}: blue");
        near(state(&frame, "mover_right").intensity, 1.0, &what);
        near(state(&frame, "mover_right").pan, 0.2, &what);
        assert!(lit.is_low(&frame), "{what}: lit wheel still red");
    }
    let frame = render(&mut engine, 8.0);
    let head = state(&frame, "mover_left");
    near(head.intensity, 1.0, "the one");
    near(head.pan, 0.9, "the one");
    assert_eq!(attribute(head, "Gobo1"), number(128.0));
    assert!(dipped.is_high(&frame));
}

#[test]
fn a_dark_head_moves_at_full_speed_under_the_slew_limit() {
    let mut engine = playing("before");
    engine
        .apply(0.0, &Command::SetIntensity { level: 0.0 })
        .unwrap();
    send(&mut engine, 0.0, 8, "after", vec![dip(2.0, 0.0)]);
    for frame in 0..(at(6.0) * FRAME_RATE_HZ) as usize {
        engine.render_at(frame as f64 / FRAME_RATE_HZ).unwrap();
    }
    let frame = render(&mut engine, 6.0);
    let head = state(&frame, "mover_left");
    near(head.intensity, 0.0, "dark");
    near(head.pan, 0.9, "arrived");
    for frame in (at(6.1) * FRAME_RATE_HZ) as usize..=(at(8.0) * FRAME_RATE_HZ) as usize {
        engine.render_at(frame as f64 / FRAME_RATE_HZ).unwrap();
    }
    let frame = engine.render_at(at(8.0) + 1.0 / FRAME_RATE_HZ).unwrap();
    near(state(&frame, "mover_left").pan, 0.9, "in place on the one");
    assert!(state(&frame, "mover_right").pan < 0.9, "slewing in light");
}

#[test]
fn move_in_black_renders_the_entry_on_the_bar_it_starts() {
    let mut engine = playing("before");
    send(&mut engine, 0.0, 8, "after_bars", vec![dip(2.0, 0.0)]);
    render(&mut engine, 6.0);
    let dark = state(&render(&mut engine, 7.0), "mover_left").clone();
    let on_the_one = state(&render(&mut engine, 8.0), "mover_left").clone();
    near(dark.intensity, 0.0, "dark");
    near(dark.pan, 0.1 + 2.0 / 10.0, "bar 2");
    near(dark.tilt, 0.2, "phase 0");
    near(dark.pan, on_the_one.pan, "same bar as the clock's");
    near(dark.tilt, on_the_one.tilt, "same phase as the clock's");
}

#[test]
fn move_in_black_follows_the_dip_through_a_late_beat_and_a_bar_phase_correction() {
    for (index, bar_phase) in [(7, None), (7, Some(0))] {
        let mut engine = playing("before");
        send(&mut engine, 0.0, 8, "after", vec![dip(2.0, 0.0)]);
        render(&mut engine, 6.0);
        render(&mut engine, 6.5);
        beat_message(&mut engine, at(7.2), index, bar_phase);
        for seconds in [at(7.2), at(7.4), at(7.6)] {
            let frame = engine.render_at(seconds).unwrap();
            let head = state(&frame, "mover_left");
            if head.intensity > 0.0 {
                continue;
            }
            near(head.pan, 0.9, &format!("{bar_phase:?} at {seconds} s"));
        }
    }
}

#[test]
fn only_the_dip_bank_moves_in_black() {
    let mut engine = playing("before");
    let fill = OverlayCue {
        ..overlay("fill/dark_left_mover", -2.0, 2.0)
    };
    send(&mut engine, 0.0, 8, "after", vec![fill]);
    render(&mut engine, 6.0);
    let head = state(&render(&mut engine, 7.0), "mover_left").clone();
    near(head.intensity, 0.0, "dark");
    near(head.pan, 0.2, "a fill's dark is not a move");
}

#[test]
fn a_dip_that_ends_before_the_next_cue_moves_nothing() {
    let mut engine = playing("before");
    let every_bar = OverlayCue {
        every_bars: 1,
        ..overlay("dip/dip", 1.0, 1.0)
    };
    send(&mut engine, 0.0, 4, "before", vec![every_bar]);
    send(&mut engine, 0.0, 16, "after", vec![]);
    render(&mut engine, 4.0);
    let frame = render(&mut engine, 5.5);
    let head = state(&frame, "mover_left");
    near(head.intensity, 0.0, "dipped");
    near(head.pan, 0.2, "held in the section");
    assert_eq!(attribute(head, "Gobo1"), number(0.0));
}

#[test]
fn a_held_head_keeps_its_look_where_the_entry_or_an_override_has_its_own() {
    let crossfade = Transition::Crossfade {
        duration_beats: 4.0,
    };
    let cases = [
        (cue(8, "before", Transition::Cut, vec![dip(2.0, 0.0)]), None),
        (cue(8, "after", crossfade, vec![dip(2.0, 0.0)]), None),
        (
            cue(8, "after_right", Transition::Cut, vec![dip(2.0, 0.0)]),
            None,
        ),
        (
            cue(8, "after", Transition::Cut, vec![dip(2.0, 0.0)]),
            Some(0.4),
        ),
    ];
    for (next, lit_by_override) in cases {
        let mut engine = playing("before");
        send_cue(&mut engine, 0.0, next);
        if let Some(level) = lit_by_override {
            let target = Target::Fixture(support::stage_uuid("mover_left"));
            let values = PropertyValues {
                intensity: Some(level),
                ..Default::default()
            };
            engine
                .apply(
                    0.0,
                    &Command::SetOverride {
                        value: static_look(target, values),
                    },
                )
                .unwrap();
        }
        render(&mut engine, 6.0);
        let frame = render(&mut engine, 7.0);
        let head = state(&frame, "mover_left");
        near(head.intensity, lit_by_override.unwrap_or(0.0), "held");
        near(head.pan, 0.2, "held");
        assert_eq!(attribute(head, "Gobo1"), number(0.0));
    }
}

fn scene_dmx() -> Vec<BTreeMap<u16, Vec<u8>>> {
    let mut engine = playing("before");
    send(
        &mut engine,
        0.0,
        8,
        "after",
        vec![
            enveloped("hit", 4.0, 1.0, 2.0),
            dip(1.0, 0.5),
            enveloped("tint", 6.0, 2.0, 1.0),
        ],
    );
    (0..(at(16.0) * FRAME_RATE_HZ) as usize)
        .map(|frame| {
            engine
                .render_at(frame as f64 / FRAME_RATE_HZ)
                .unwrap()
                .dmx_frames_by_universe
        })
        .collect()
}

#[test]
fn the_same_cues_render_the_same_dmx() {
    let first = scene_dmx();
    assert!(
        first.windows(2).any(|pair| pair[0] != pair[1]),
        "the scene changes"
    );
    assert_eq!(first, scene_dmx());
}

#[test]
fn eight_heavy_overlays_render_every_frame_each_inside_its_own_instruction_budget() {
    // About 30k instructions each: together past one script's 100k budget, which is per script.
    let heavy = |index: usize| {
        format!(
            "return {{render=function(ctx) local x = 0 for i = 1, 10000 do x = x + i % 7 end \
             return sf.scene {{sf.dimmer(sf.fixture(sf.members(sf.group(\"strip\"))[{}]), 1)}} \
             end}}",
            index + 1
        )
    };
    let names: Vec<String> = (0..8).map(|index| format!("heavy{index}")).collect();
    let mut scripts = vec![("base".to_owned(), BASE.to_owned())];
    scripts.extend(
        names
            .iter()
            .enumerate()
            .map(|(index, name)| (name.clone(), heavy(index))),
    );
    let mut engine = engine_on(support::stage(), &scripts);
    select(&mut engine, "base");
    send(
        &mut engine,
        0.0,
        4,
        "base",
        names.iter().map(|name| overlay(name, 0.0, 8.0)).collect(),
    );
    let strip = support::stage().groups()["strip"].clone();
    assert_eq!(strip.len(), names.len());
    let (first, last) = (
        (at(4.0) * FRAME_RATE_HZ) as usize,
        (at(8.0) * FRAME_RATE_HZ) as usize,
    );
    let started = Instant::now();
    for frame in first..last {
        let rendered = engine.render_at(frame as f64 / FRAME_RATE_HZ).unwrap();
        assert_eq!(rendered.script_error, None, "frame {frame}");
        assert_eq!(engine.overlay_error(), None, "frame {frame}");
        assert_eq!(engine.active_overlay_count(), names.len(), "frame {frame}");
        for cell in &strip {
            near(
                rendered.fixture_properties_before_strobe[cell].intensity,
                1.0,
                &format!("frame {frame} {cell}"),
            );
        }
    }
    // Reported, never asserted: wall time depends on the machine and its load.
    println!(
        "base + 8 heavy overlays: {:.2} ms per frame against a {:.0} ms frame period",
        started.elapsed().as_secs_f64() * 1e3 / (last - first) as f64,
        1e3 / FRAME_RATE_HZ
    );
}

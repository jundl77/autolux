//! Layered composition: cue overlays between the base loop and the overrides, on the generic
//! stage and a simulated clock at 120 bpm (a beat every 0.5 s).

mod support;

use std::collections::BTreeMap;
use std::time::Instant;

use starfire::engine::{Engine, RenderedFrame, perceptual_to_linear_master};
use starfire::model::{
    AttributeValue, Command, FixtureState, LightingOverride, OverlayCue, OverrideLifetime,
    PropertyValues, Rgb, RotationEntry, SectionCue, SectionIntent, Target, Transition,
};
use starfire::protocol::wire;
use starfire::script::AutoloopCatalog;

const SECONDS_PER_BEAT: f64 = 0.5;
/// Render cadence of the live loop and the replay clock.
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

const BASE: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), 0.5), sf.colour(sf.all(), sf.rgb(1, 0, 0)),
    sf.pan_tilt(sf.group("movers"), 0.2, 0.4)} end}"#;
const DARK: &str = "return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), 0)} end}";
/// Intensity on the washes only, pan alone, and colour and strobe everywhere.
const TINT: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.group("wash"), 0.25), sf.colour(sf.all(), sf.rgb(0, 0, 1)),
    {target = sf.group("movers"), values = {pan = 1}}, sf.strobe(sf.all(), 4)} end}"#;
/// A white hit decaying linearly over its length.
const HIT: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.all(), 1 - ctx.autoloop_elapsed_beats / ctx.overlay_length_beats),
    sf.colour(sf.all(), sf.rgb(1, 1, 1)), sf.pan_tilt(sf.group("movers"), 1, 0.4)} end}"#;
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
const DIP: &str = r#"return {render=function(ctx) return sf.scene {
    sf.dimmer(sf.fixture("mover_left"), 0)} end}"#;

/// Loops whose intensity is HTP; every other loop here is LTP.
const HTP: [&str; 2] = ["hit", "right_full"];

const SCRIPTS: [(&str, &str); 14] = [
    ("base", BASE),
    ("dark", DARK),
    ("tint", TINT),
    ("hit", HIT),
    ("quarter", QUARTER),
    ("three_quarters", THREE_QUARTERS),
    ("full", FULL),
    ("full_blue", FULL_BLUE),
    ("left_full", LEFT_FULL),
    ("right_full", RIGHT_FULL),
    ("breaks_at_two", BREAKS_AT_TWO),
    ("before", BEFORE),
    ("after", AFTER),
    ("dip", DIP),
];

fn engine_with(scripts: &[(String, String)], htp: &[&str]) -> Engine {
    let catalog = AutoloopCatalog::from_definitions(
        &scripts
            .iter()
            .map(|(name, source)| wire::AutoloopDefinition {
                autoloop_id: name.clone(),
                bank: "test".into(),
                lua_source: source.clone(),
                intensity_htp: htp.contains(&name.as_str()),
            })
            .collect::<Vec<_>>(),
    )
    .unwrap();
    let mut engine = Engine::new(support::stage(), catalog, support::palettes()).unwrap();
    engine
        .apply(
            0.0,
            &Command::Beat {
                beat_index: 0,
                bar_phase: Some(0),
                beats_per_bar: 4,
                bpm: 120.0,
                features: None,
            },
        )
        .unwrap();
    engine
}

fn select(engine: &mut Engine, name: &str) {
    engine
        .apply(
            0.0,
            &Command::Select {
                bank: "test".into(),
                autoloop: name.into(),
                transition: Transition::Cut,
                palette: "techno".into(),
            },
        )
        .unwrap();
}

/// The stage with this file's scripts and `base` playing.
fn playing(base: &str) -> Engine {
    let scripts: Vec<(String, String)> = SCRIPTS
        .iter()
        .map(|(name, source)| ((*name).to_owned(), (*source).to_owned()))
        .collect();
    let mut engine = engine_with(&scripts, &HTP);
    select(&mut engine, base);
    engine
}

fn at(beat: f64) -> f64 {
    beat * SECONDS_PER_BEAT
}

fn overlay(name: &str, begin_beats: f64, length_beats: f64) -> OverlayCue {
    OverlayCue {
        bank: "test".into(),
        autoloop: name.into(),
        priority: 0,
        begin_beats,
        length_beats,
        every_bars: 0,
        attack_beats: 0.0,
        release_beats: 0.0,
    }
}

fn enveloped(name: &str, length_beats: f64, attack_beats: f64, release_beats: f64) -> OverlayCue {
    OverlayCue {
        attack_beats,
        release_beats,
        ..overlay(name, 0.0, length_beats)
    }
}

/// A cue starting at beat `start` whose rotation enters `base` for 4 bars.
fn cue(start: u64, base: &str, transition: Transition, overlays: Vec<OverlayCue>) -> SectionCue {
    SectionCue {
        start_beat_index: start,
        intent: SectionIntent::Drop,
        rotation: vec![RotationEntry {
            bank: "test".into(),
            autoloop: base.into(),
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
    engine
        .apply(
            at(beat),
            &Command::CueSection {
                value: Box::new(cue),
            },
        )
        .unwrap();
}

/// A cue cutting to `base`.
fn send(engine: &mut Engine, beat: f64, start: u64, base: &str, overlays: Vec<OverlayCue>) {
    send_cue(engine, beat, cue(start, base, Transition::Cut, overlays));
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

/// Red mixed toward white by `white`.
fn pink(white: f64) -> Rgb {
    Rgb {
        red: 1.0,
        green: white,
        blue: white,
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
        // No intensity is set on the movers, and their colour wheel changes slot half-way.
        near(mover.intensity, 0.5, &what);
        near_colour(mover.colour, if level < 0.5 { RED } else { BLUE }, &what);
    }
}

#[test]
fn an_htp_overlay_takes_the_brighter_intensity_and_colours_by_its_share_of_it() {
    let mut engine = playing("base");
    send(&mut engine, 0.0, 4, "base", vec![overlay("hit", 0.0, 8.0)]);
    // Beats into the hit, its intensity 1 - e/8 against the base's 0.5, and its share of the
    // brighter of the two, which carries its white and its pan.
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
        near(state(&frame, "mover_left").pan, 0.2 + 0.8 * share, &what);
    }
    // Over a dark base the hit is the only light, so its colour holds as it fades.
    let mut engine = playing("dark");
    send(&mut engine, 0.0, 4, "dark", vec![overlay("hit", 0.0, 8.0)]);
    render(&mut engine, 4.0);
    let frame = render(&mut engine, 10.0);
    let wash = state(&frame, "wash_left_outer");
    near(wash.intensity, 0.25, "dark base");
    near_colour(wash.colour, pink(1.0), "dark base");
}

#[test]
fn higher_priority_composes_later_and_ties_keep_list_order() {
    let row = |name: &str, priority: i32| OverlayCue {
        priority,
        ..overlay(name, 0.0, 8.0)
    };
    for (rows, expected) in [
        (vec![row("quarter", 1), row("three_quarters", 0)], 0.25),
        (vec![row("three_quarters", 1), row("quarter", 0)], 0.75),
        (vec![row("quarter", 0), row("three_quarters", 0)], 0.75),
        (vec![row("three_quarters", 0), row("quarter", 0)], 0.25),
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
    // (attack, release, length) and (beats after the start, level).
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
        // Release 0 is a snap: full up to the end, gone on it.
        (
            (0.0, 0.0, 4.0),
            &[(0.0, 1.0), (3.75, 1.0), (4.0, 0.0), (5.0, 0.0)],
        ),
        // An attack longer than the row releases from where it got to.
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
    // A pre-roll row received late starts mid-way and attacks from its first frame.
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

    // A Static-mode look: an override above the specials.
    let static_look = LightingOverride {
        id: "static".into(),
        target: Target::All,
        values: PropertyValues {
            intensity: Some(0.3),
            colour: Some(RED),
            ..Default::default()
        },
        intensity_multiplier: None,
        lifetime: OverrideLifetime::UntilCleared,
        priority: 1100,
        special_fx: None,
        pan_sweep: false,
        aim_focus: false,
        pose: None,
    };
    engine
        .apply(at(5.5), &Command::SetOverride { value: static_look })
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
        // The base shows where the broken overlay was; the other overlay plays on.
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

/// The DMX byte that carries a cell's colour wheel, found as the one byte a red and a blue look
/// differ in, with its value for each.
struct Wheel {
    universe: u16,
    slot: usize,
    red: u8,
    blue: u8,
}

impl Wheel {
    fn of(label: &str) -> Self {
        let rig = support::stage();
        let encode = |colour| {
            let mut states = rig.empty_states();
            let state = states.get_mut(&support::stage_uuid(label)).unwrap();
            state.intensity = 1.0;
            state.colour = colour;
            rig.encode(&states).unwrap()
        };
        let (red, blue) = (encode(RED), encode(BLUE));
        let differing: Vec<(u16, usize)> = red
            .iter()
            .flat_map(|(universe, bytes)| {
                bytes
                    .iter()
                    .zip(&blue[universe])
                    .enumerate()
                    .filter(|(_, (red, blue))| red != blue)
                    .map(|(slot, _)| (*universe, slot))
            })
            .collect();
        assert_eq!(differing.len(), 1, "{label}: {differing:?}");
        let (universe, slot) = differing[0];
        Self {
            universe,
            slot,
            red: red[&universe][slot],
            blue: blue[&universe][slot],
        }
    }

    fn in_frame(&self, frame: &RenderedFrame) -> u8 {
        frame.dmx_frames_by_universe[&self.universe][self.slot]
    }
}

/// The stage movers' Gobo1 is one continuous wheel channel.
fn gobo(physical: f64) -> Option<AttributeValue> {
    Some(AttributeValue::Number(physical))
}

#[test]
fn a_dipped_head_moves_in_black_before_the_cue_and_a_lit_wheel_holds_its_slot() {
    let (dipped, lit) = (Wheel::of("mover_left"), Wheel::of("mover_right"));
    let (open, after) = (gobo(0.0), gobo(128.0));
    let mut engine = playing("before");
    let dip = OverlayCue {
        attack_beats: 1.0,
        ..overlay("dip", -2.0, 2.0)
    };
    send(&mut engine, 0.0, 8, "after", vec![dip]);
    // Before the dip, on its first frame, and half-way down its attack: still lit.
    for beat in [5.0, 6.0, 6.5] {
        let frame = render(&mut engine, beat);
        let what = format!("beat {beat}");
        let head = state(&frame, "mover_left");
        assert!(head.intensity > 0.0, "{what}");
        near(head.pan, 0.2, &what);
        near(head.tilt, 0.3, &what);
        assert_eq!(head.attributes.get("Gobo1").cloned(), open, "{what}");
        assert_eq!(dipped.in_frame(&frame), dipped.red, "{what}");
    }
    // Dark: the next cue's first look at its start, at intensity 0. The undipped head stays
    // lit and its wheel holds its slot.
    for beat in [7.0, 7.75] {
        let frame = render(&mut engine, beat);
        let what = format!("beat {beat}");
        let head = state(&frame, "mover_left");
        near(head.intensity, 0.0, &what);
        near(head.pan, 0.9, &what);
        near(head.tilt, 0.7, &what);
        assert_eq!(head.attributes.get("Gobo1").cloned(), after, "{what}");
        assert_eq!(dipped.in_frame(&frame), dipped.blue, "{what}");
        near(state(&frame, "mover_right").intensity, 1.0, &what);
        near(state(&frame, "mover_right").pan, 0.2, &what);
        assert_eq!(lit.in_frame(&frame), lit.red, "{what}");
    }
    // On the one the dipped head has nothing left to move.
    let frame = render(&mut engine, 8.0);
    let head = state(&frame, "mover_left");
    near(head.intensity, 1.0, "the one");
    near(head.pan, 0.9, "the one");
    assert_eq!(head.attributes.get("Gobo1").cloned(), after);
    assert_eq!(dipped.in_frame(&frame), dipped.blue);
}

#[test]
fn a_dip_that_ends_before_the_next_cue_moves_nothing() {
    let mut engine = playing("before");
    let every_bar = OverlayCue {
        every_bars: 1,
        ..overlay("dip", 1.0, 1.0)
    };
    send(&mut engine, 0.0, 4, "before", vec![every_bar]);
    send(&mut engine, 0.0, 16, "after", vec![]);
    render(&mut engine, 4.0);
    let frame = render(&mut engine, 5.5);
    let head = state(&frame, "mover_left");
    near(head.intensity, 0.0, "dipped");
    // The head comes back lit inside this section, so it keeps this section's look.
    near(head.pan, 0.2, "held in the section");
    assert_eq!(head.attributes.get("Gobo1").cloned(), gobo(0.0));
}

#[test]
fn a_cue_that_keeps_the_running_loop_or_crossfades_into_its_entry_moves_nothing() {
    let dip = || OverlayCue {
        attack_beats: 0.0,
        ..overlay("dip", -2.0, 2.0)
    };
    let crossfade = Transition::Crossfade {
        duration_beats: 4.0,
    };
    for next in [
        cue(8, "before", Transition::Cut, vec![dip()]),
        cue(8, "after", crossfade, vec![dip()]),
    ] {
        let mut engine = playing("before");
        send_cue(&mut engine, 0.0, next);
        render(&mut engine, 6.0);
        let frame = render(&mut engine, 7.0);
        let head = state(&frame, "mover_left");
        near(head.intensity, 0.0, "dipped");
        // On the one the head shows the look before, so it waits there.
        near(head.pan, 0.2, "held");
        assert_eq!(head.attributes.get("Gobo1").cloned(), gobo(0.0));
    }
}

/// Every frame's DMX for a scene with HTP and LTP overlays, a release and a dip before a cue.
fn scene_dmx() -> Vec<BTreeMap<u16, Vec<u8>>> {
    let mut engine = playing("before");
    send(
        &mut engine,
        0.0,
        8,
        "after",
        vec![
            enveloped("hit", 4.0, 1.0, 2.0),
            OverlayCue {
                attack_beats: 0.5,
                ..overlay("dip", -1.0, 1.0)
            },
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
    // About 30k Lua instructions each, so together far past one script's 100k budget, which
    // stays per script (B1). Overlay k lights strip cell k.
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
    let htp: Vec<&str> = names.iter().map(String::as_str).collect();
    let mut engine = engine_with(&scripts, &htp);
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

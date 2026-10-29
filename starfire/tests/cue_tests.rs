//! Section cues on a simulated clock: 120 bpm, four beats to the bar, a beat every 0.5 s.

mod support;

use prost::Message;
use starfire::cue::{
    CueSample, CueTimeline, InstanceState, MAX_ACTIVE_OVERLAYS, MAX_OVERLAYS, MAX_PENDING_CUES,
    MAX_ROTATION,
};
use starfire::engine::{Engine, RenderedFrame};
use starfire::model::{
    BeatFeatures, Command, OverlayCue, RenderContext, Rgb, RotationEntry, SectionCue,
    SectionIntent, Transition,
};
use starfire::protocol::{ClientMessage, MAX_MESSAGE_BYTES, apply_command, wire};
use starfire::script::AutoloopCatalog;

const SECONDS_PER_BEAT: f64 = 0.5;
const RIG: &str = r#"{"schema_version":1,"fixtures":[
    {"fixture_uuid":"left","profile":"generic_rgb","universe":0,"address":1},
    {"fixture_uuid":"right","profile":"generic_rgb","universe":0,"address":4}],
    "groups":{"wash":["left","right"]}}"#;
/// Encodes the section context into colours: beat counts over 1000, nil as 1.
const PROBE: &str = r#"
local INTENTS = {atmospheric = 0.2, breakdown = 0.4, buildup = 0.6, drop = 0.8}
local function beats(value) return value and value / 1000 or 1 end
return {render = function(ctx)
  return sf.scene {
    sf.dimmer(sf.all(), 1),
    sf.colour(sf.fixture("left"), sf.rgb(beats(ctx.section_elapsed_beats),
        beats(ctx.beats_until_next), beats(ctx.autoloop_elapsed_beats))),
    sf.colour(sf.fixture("right"), sf.rgb(INTENTS[ctx.section_intent] or 0,
        INTENTS[ctx.next_intent] or 0, ctx.seed / 4294967296)),
  }
end}"#;
/// Encodes the music fields: levels halved, nil as 1.
const MUSIC: &str = r#"
local function level(value) return value and value / 2 or 1 end
return {render = function(ctx)
  return sf.scene {
    sf.dimmer(sf.all(), 1),
    sf.colour(sf.fixture("left"), sf.rgb(level(ctx.onset), level(ctx.bass), ctx.bpm / 1000)),
    sf.colour(sf.fixture("right"), sf.rgb(level(ctx.mid), level(ctx.high), level(ctx.drop_strength))),
  }
end}"#;
const STILL: &str = "return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), 0.5)} end}";

fn engine() -> Engine {
    engine_with(&[
        ("probe", PROBE),
        ("music", MUSIC),
        ("other", PROBE),
        ("still", STILL),
    ])
}

fn engine_with(scripts: &[(&str, &str)]) -> Engine {
    let catalog = AutoloopCatalog::from_definitions(
        &scripts
            .iter()
            .map(|(name, source)| wire::AutoloopDefinition {
                autoloop_id: (*name).into(),
                bank: "test".into(),
                lua_source: (*source).into(),
                intensity_htp: false,
            })
            .collect::<Vec<_>>(),
    )
    .unwrap();
    let rig = support::rig(serde_json::from_str(RIG).unwrap());
    Engine::new(rig, catalog, support::palettes()).unwrap()
}

fn at(beat: f64) -> f64 {
    beat * SECONDS_PER_BEAT
}

fn beat_at(engine: &mut Engine, seconds: f64, index: u64, bar_phase: u32) {
    engine
        .apply(
            seconds,
            &Command::Beat {
                beat_index: index,
                bar_phase: Some(bar_phase),
                beats_per_bar: 4,
                bpm: 120.0,
                features: None,
            },
        )
        .unwrap();
}

/// The beat arriving on time: index n at beat n.
fn beat(engine: &mut Engine, index: u64) {
    beat_at(engine, at(index as f64), index, (index % 4) as u32);
}

fn select(engine: &mut Engine, seconds: f64, name: &str) {
    engine
        .apply(
            seconds,
            &Command::Select {
                bank: "test".into(),
                autoloop: name.into(),
                transition: Transition::Cut,
                palette: "techno".into(),
            },
        )
        .unwrap();
}

fn section(start: u64, intent: SectionIntent, rotation: &[(&str, u32)]) -> SectionCue {
    SectionCue {
        start_beat_index: start,
        intent,
        rotation: rotation
            .iter()
            .map(|(name, bars)| RotationEntry {
                bank: "test".into(),
                autoloop: (*name).into(),
                bars: *bars,
                transition: Transition::Cut,
            })
            .collect(),
        overlays: Vec::new(),
        palette: "techno".into(),
        drop_strength: None,
        seed: 7,
    }
}

fn overlay(begin_beats: f64, length_beats: f64, every_bars: u32, release_beats: f64) -> OverlayCue {
    OverlayCue {
        bank: "test".into(),
        autoloop: "still".into(),
        priority: 0,
        begin_beats,
        length_beats,
        every_bars,
        attack_beats: 0.0,
        release_beats,
    }
}

fn send(engine: &mut Engine, seconds: f64, cue: SectionCue) -> anyhow::Result<()> {
    engine.apply(
        seconds,
        &Command::CueSection {
            value: Box::new(cue),
        },
    )
}

struct Probe {
    section_elapsed: Option<f64>,
    until_next: Option<f64>,
    autoloop_elapsed: Option<f64>,
    intent: f64,
    next_intent: f64,
    seed: f64,
    autoloop: Option<String>,
}

fn beats(value: f64) -> Option<f64> {
    (value < 1.0).then_some(value * 1000.0)
}

fn colours(frame: &RenderedFrame) -> (Rgb, Rgb) {
    let states = &frame.fixture_properties_before_strobe;
    (states["left"].colour, states["right"].colour)
}

fn probe(engine: &mut Engine, beat: f64) -> Probe {
    let frame = engine.render_at(at(beat)).unwrap();
    assert!(frame.script_error.is_none(), "{:?}", frame.script_error);
    let (left, right) = colours(&frame);
    Probe {
        section_elapsed: beats(left.red),
        until_next: beats(left.green),
        autoloop_elapsed: beats(left.blue),
        intent: right.red,
        next_intent: right.green,
        seed: right.blue * 4_294_967_296.0,
        autoloop: frame.active_autoloop,
    }
}

fn near(actual: Option<f64>, expected: f64) {
    let actual = actual.expect("value present");
    assert!((actual - expected).abs() < 1e-9, "{actual} != {expected}");
}

#[test]
fn a_cue_activates_on_the_first_frame_at_its_start_beat() {
    let mut engine = engine();
    beat(&mut engine, 0);
    select(&mut engine, 0.0, "other");
    send(
        &mut engine,
        at(2.0),
        section(8, SectionIntent::Drop, &[("probe", 16)]),
    )
    .unwrap();
    let before = probe(&mut engine, 7.95);
    assert_eq!(before.autoloop.as_deref(), Some("other"));
    assert_eq!(before.intent, 0.0);
    near(before.section_elapsed, 7.95);
    assert_eq!(before.next_intent, 0.8);
    near(before.until_next, 0.05);
    // No beat message: the extrapolated clock reaches the start.
    let started = probe(&mut engine, 8.0);
    assert_eq!(started.autoloop.as_deref(), Some("probe"));
    near(started.section_elapsed, 0.0);
    near(started.autoloop_elapsed, 0.0);
    assert_eq!(started.intent, 0.8);
    assert_eq!((started.next_intent, started.until_next), (0.0, None));
    assert_eq!(started.seed, 7.0);
    assert_eq!(engine.pending_cue_count(), 0);
}

#[test]
fn the_start_beat_message_itself_activates_the_cue_on_its_frame() {
    let mut engine = engine();
    beat(&mut engine, 0);
    send(
        &mut engine,
        at(1.0),
        section(4, SectionIntent::Buildup, &[("probe", 4)]),
    )
    .unwrap();
    beat(&mut engine, 4);
    let started = probe(&mut engine, 4.0);
    assert_eq!(started.autoloop.as_deref(), Some("probe"));
    near(started.section_elapsed, 0.0);
}

#[test]
fn a_bar_phase_correction_moves_a_pending_cue_with_the_grid() {
    let mut engine = engine();
    beat(&mut engine, 0);
    select(&mut engine, 0.0, "other");
    send(
        &mut engine,
        at(1.0),
        section(8, SectionIntent::Drop, &[("probe", 16)]),
    )
    .unwrap();
    // Index 5 is said to be phase 2: the grid jumps forward a beat, so index 8 is grid 9.
    beat_at(&mut engine, at(5.0), 5, 2);
    near(probe(&mut engine, 5.0).until_next, 3.0);
    // A beat earlier in time the grid reads 8, which the cue no longer is.
    let frame = engine.render_at(at(7.0)).unwrap();
    assert_eq!(frame.beat_position, 8.0);
    assert_eq!(frame.active_autoloop.as_deref(), Some("other"));
    let started = probe(&mut engine, 8.0);
    assert_eq!(started.autoloop.as_deref(), Some("probe"));
    near(started.section_elapsed, 0.0);
}

#[test]
fn a_late_cue_starts_on_the_next_bar_line_with_its_rotation_counted_from_the_true_start() {
    let mut engine = engine();
    beat(&mut engine, 0);
    select(&mut engine, 0.0, "other");
    let mut late = section(4, SectionIntent::Drop, &[("probe", 16)]);
    late.overlays = vec![overlay(0.0, 2.0, 0, 0.0)];
    // Received at beat 5, a beat after its start: not mid-bar, on beat 8.
    send(&mut engine, at(5.0), late).unwrap();
    let waiting = probe(&mut engine, 7.9);
    assert_eq!(waiting.autoloop.as_deref(), Some("other"));
    near(waiting.until_next, 0.1);
    assert_eq!(engine.active_overlay_count(), 0);
    let started = probe(&mut engine, 8.0);
    assert_eq!(started.autoloop.as_deref(), Some("probe"));
    near(started.section_elapsed, 4.0);
    near(started.autoloop_elapsed, 4.0);
    // The entry hit fires on the bar line, for its whole length.
    assert_eq!(engine.active_overlay_count(), 1);
    probe(&mut engine, 9.9);
    assert_eq!(engine.active_overlay_count(), 1);
    probe(&mut engine, 10.0);
    assert_eq!(engine.active_overlay_count(), 0);
}

#[test]
fn a_resent_cue_keeps_its_section_and_never_refires_its_entry() {
    let mut engine = engine();
    beat(&mut engine, 0);
    let mut cue = section(4, SectionIntent::Drop, &[("probe", 16)]);
    cue.overlays = vec![overlay(0.0, 2.0, 0, 0.0)];
    send(&mut engine, at(1.0), cue.clone()).unwrap();
    probe(&mut engine, 4.0);
    assert_eq!(engine.active_overlay_count(), 1);
    probe(&mut engine, 6.5);
    send(&mut engine, at(6.5), cue).unwrap();
    let replaced = probe(&mut engine, 8.0);
    near(replaced.section_elapsed, 4.0);
    near(replaced.autoloop_elapsed, 4.0);
    assert_eq!(engine.active_overlay_count(), 0);
}

#[test]
fn a_replacement_drops_pending_cues_at_or_after_its_start_and_releases_their_preroll() {
    let mut engine = engine();
    beat(&mut engine, 0);
    select(&mut engine, 0.0, "other");
    let mut drop = section(16, SectionIntent::Drop, &[("probe", 16)]);
    drop.overlays = vec![overlay(-8.0, 8.0, 0, 2.0)];
    send(&mut engine, at(1.0), drop).unwrap();
    send(
        &mut engine,
        at(1.0),
        section(32, SectionIntent::Breakdown, &[("probe", 16)]),
    )
    .unwrap();
    probe(&mut engine, 7.9);
    assert_eq!(engine.active_overlay_count(), 0);
    probe(&mut engine, 8.0);
    assert_eq!(engine.active_overlay_count(), 1);
    // The decoder changes its mind: the section at 16 is a buildup after all.
    send(
        &mut engine,
        at(10.0),
        section(16, SectionIntent::Buildup, &[("other", 16)]),
    )
    .unwrap();
    assert_eq!(engine.pending_cue_count(), 1);
    let replaced = probe(&mut engine, 10.0);
    assert_eq!(replaced.next_intent, 0.6);
    assert_eq!(engine.active_overlay_count(), 1, "the riser releases");
    probe(&mut engine, 11.9);
    assert_eq!(engine.active_overlay_count(), 1);
    probe(&mut engine, 12.0);
    assert_eq!(engine.active_overlay_count(), 0);
    let started = probe(&mut engine, 16.0);
    assert_eq!(started.intent, 0.6);
    assert_eq!(started.autoloop.as_deref(), Some("other"));
}

#[test]
fn the_latest_of_several_cues_passed_in_one_frame_after_a_resume_wins() {
    let mut engine = engine();
    beat(&mut engine, 0);
    select(&mut engine, 0.0, "still");
    for (start, intent) in [
        (4, SectionIntent::Buildup),
        (8, SectionIntent::Drop),
        (12, SectionIntent::Breakdown),
    ] {
        send(
            &mut engine,
            at(0.5),
            section(start, intent, &[("probe", 4)]),
        )
        .unwrap();
    }
    probe(&mut engine, 0.5);
    engine
        .apply(at(1.0), &Command::SetPaused { paused: true })
        .unwrap();
    // Beats keep arriving while paused; the frozen clock only follows them on the next beat.
    for index in 2..14 {
        beat(&mut engine, index);
    }
    engine
        .apply(at(14.0), &Command::SetPaused { paused: false })
        .unwrap();
    assert_eq!(probe(&mut engine, 14.0).autoloop.as_deref(), Some("still"));
    beat(&mut engine, 14);
    let resumed = probe(&mut engine, 14.0);
    assert_eq!(resumed.autoloop.as_deref(), Some("probe"));
    assert_eq!(resumed.intent, 0.4);
    near(resumed.section_elapsed, 2.0);
    assert_eq!(engine.pending_cue_count(), 0);
}

#[test]
fn section_elapsed_stays_at_zero_when_a_late_beat_re_anchors_before_the_start() {
    let mut engine = engine();
    beat(&mut engine, 0);
    beat(&mut engine, 2);
    send(
        &mut engine,
        at(2.0),
        section(4, SectionIntent::Drop, &[("probe", 1), ("other", 1)]),
    )
    .unwrap();
    // The clock runs on past beat 4 before beat 3 arrives late and pulls it back.
    let started = probe(&mut engine, 4.2);
    near(started.section_elapsed, 0.2);
    beat_at(&mut engine, at(4.3), 3, 3);
    let pulled_back = probe(&mut engine, 4.3);
    assert_eq!(pulled_back.autoloop.as_deref(), Some("probe"));
    near(pulled_back.section_elapsed, 0.0);
    assert_eq!(
        pulled_back.intent, 0.8,
        "an active cue never returns to pending"
    );
    assert_eq!(engine.pending_cue_count(), 0);
}

#[test]
fn rotation_entries_change_on_bar_lines_from_the_start_and_cycle() {
    let mut engine = engine();
    beat(&mut engine, 0);
    send(
        &mut engine,
        at(1.0),
        section(4, SectionIntent::Drop, &[("probe", 1), ("other", 2)]),
    )
    .unwrap();
    for (beat, autoloop, entry_elapsed) in [
        (4.0, "probe", 0.0),
        (7.9, "probe", 3.9),
        (8.0, "other", 0.0),
        (9.0, "other", 1.0),
        (15.9, "other", 7.9),
        (16.0, "probe", 0.0),
        (20.0, "other", 0.0),
    ] {
        let frame = probe(&mut engine, beat);
        assert_eq!(frame.autoloop.as_deref(), Some(autoloop), "beat {beat}");
        near(frame.autoloop_elapsed, entry_elapsed);
        near(frame.section_elapsed, beat - 4.0);
    }
}

#[test]
fn a_cue_whose_entry_is_the_running_loop_does_not_restart_it() {
    let mut engine = engine();
    beat(&mut engine, 0);
    select(&mut engine, 0.0, "probe");
    send(
        &mut engine,
        at(1.0),
        section(4, SectionIntent::Buildup, &[("probe", 4)]),
    )
    .unwrap();
    let continued = probe(&mut engine, 5.0);
    near(continued.section_elapsed, 1.0);
    near(continued.autoloop_elapsed, 5.0);
}

#[test]
fn play_autoloop_and_stop_clear_every_cue() {
    let mut engine = engine();
    beat(&mut engine, 0);
    let mut drop = section(8, SectionIntent::Drop, &[("probe", 4)]);
    drop.overlays = vec![overlay(-4.0, 4.0, 0, 4.0)];
    send(&mut engine, at(0.5), drop.clone()).unwrap();
    send(
        &mut engine,
        at(0.5),
        section(16, SectionIntent::Breakdown, &[("probe", 4)]),
    )
    .unwrap();
    probe(&mut engine, 5.0);
    assert_eq!(engine.active_overlay_count(), 1);
    select(&mut engine, at(5.0), "still");
    assert_eq!(engine.pending_cue_count(), 0);
    let cleared = probe(&mut engine, 8.0);
    assert_eq!(cleared.autoloop.as_deref(), Some("still"));
    assert_eq!(
        engine.active_overlay_count(),
        1,
        "PlayAutoloop releases overlays"
    );

    send(
        &mut engine,
        at(9.0),
        section(12, SectionIntent::Drop, &[("probe", 4)]),
    )
    .unwrap();
    engine.apply(at(9.0), &Command::Stop).unwrap();
    assert_eq!(engine.pending_cue_count(), 0);
    assert_eq!(engine.active_overlay_count(), 0, "Stop drops them");
    let stopped = engine.render_at(at(12.0)).unwrap();
    assert_eq!(stopped.active_autoloop, None);
}

#[test]
fn clear_cues_ends_the_section_but_the_base_look_plays_on() {
    let mut engine = engine();
    beat(&mut engine, 0);
    send(
        &mut engine,
        at(0.5),
        section(4, SectionIntent::Drop, &[("probe", 4)]),
    )
    .unwrap();
    send(
        &mut engine,
        at(0.5),
        section(20, SectionIntent::Breakdown, &[("other", 4)]),
    )
    .unwrap();
    probe(&mut engine, 4.0);
    engine.apply(at(6.0), &Command::ClearCues).unwrap();
    let cleared = probe(&mut engine, 6.0);
    assert_eq!(cleared.autoloop.as_deref(), Some("probe"));
    assert_eq!((cleared.section_elapsed, cleared.intent), (None, 0.0));
    assert_eq!(engine.pending_cue_count(), 0);
    assert_eq!(probe(&mut engine, 21.0).autoloop.as_deref(), Some("probe"));
}

#[test]
fn a_fifth_pending_cue_is_refused_and_changes_nothing() {
    let mut engine = engine();
    beat(&mut engine, 0);
    for start in (8..).step_by(4).take(MAX_PENDING_CUES) {
        send(
            &mut engine,
            at(0.5),
            section(start, SectionIntent::Drop, &[("probe", 1)]),
        )
        .unwrap();
    }
    let fifth = section(100, SectionIntent::Drop, &[("probe", 1)]);
    assert!(send(&mut engine, at(0.5), fifth).is_err());
    assert_eq!(engine.pending_cue_count(), MAX_PENDING_CUES);
    // A cue before them all supersedes every one.
    send(
        &mut engine,
        at(0.5),
        section(4, SectionIntent::Buildup, &[("probe", 1)]),
    )
    .unwrap();
    assert_eq!(engine.pending_cue_count(), 1);
}

#[test]
fn invalid_cues_are_refused_before_anything_changes() {
    let mut engine = engine();
    beat(&mut engine, 0);
    let valid = section(8, SectionIntent::Drop, &[("probe", 1)]);
    let mut cases = Vec::new();
    let mut cue = valid.clone();
    cue.rotation.clear();
    cases.push(cue);
    let mut cue = valid.clone();
    cue.rotation = vec![valid.rotation[0].clone(); MAX_ROTATION + 1];
    cases.push(cue);
    let mut cue = valid.clone();
    cue.rotation[0].bars = 0;
    cases.push(cue);
    let mut cue = valid.clone();
    cue.rotation[0].autoloop = "missing".into();
    cases.push(cue);
    let mut cue = valid.clone();
    cue.palette = "missing".into();
    cases.push(cue);
    let mut cue = valid.clone();
    cue.drop_strength = Some(1.5);
    cases.push(cue);
    let mut cue = section(8, SectionIntent::Buildup, &[("probe", 1)]);
    cue.drop_strength = Some(0.5);
    cases.push(cue);
    let mut cue = valid.clone();
    cue.overlays = vec![overlay(0.0, 1.0, 0, 0.0); MAX_OVERLAYS + 1];
    cases.push(cue);
    for bad in [
        overlay(-65.0, 1.0, 0, 0.0),
        overlay(1025.0, 1.0, 0, 0.0),
        overlay(4.0, 1.0, 1, 0.0),
        overlay(0.0, 3.0, 1, 2.0),
        overlay(0.0, 0.0, 0, 0.0),
        overlay(0.0, 1.0, 0, -1.0),
        overlay(0.0, f64::NAN, 0, 0.0),
    ] {
        let mut cue = valid.clone();
        cue.overlays = vec![bad];
        cases.push(cue);
    }
    for cue in cases {
        assert!(send(&mut engine, at(0.5), cue.clone()).is_err(), "{cue:?}");
        assert_eq!(engine.pending_cue_count(), 0);
    }
    send(&mut engine, at(0.5), valid).unwrap();
}

#[test]
fn context_fields_are_nil_until_there_is_something_to_say() {
    let mut engine = engine();
    select(&mut engine, 0.0, "music");
    let nothing = colours(&engine.render_at(0.0).unwrap());
    assert_eq!(
        nothing.0,
        Rgb {
            red: 1.0,
            green: 1.0,
            blue: 0.0
        }
    );
    assert_eq!(
        nothing.1,
        Rgb {
            red: 1.0,
            green: 1.0,
            blue: 1.0
        }
    );
    engine
        .apply(
            0.0,
            &Command::Beat {
                beat_index: 0,
                bar_phase: Some(0),
                beats_per_bar: 4,
                bpm: 120.0,
                features: Some(BeatFeatures {
                    onset: 1.0,
                    bass: 0.8,
                    mid: 0.5,
                    high: 0.2,
                }),
            },
        )
        .unwrap();
    let mut drop = section(4, SectionIntent::Drop, &[("music", 4)]);
    drop.drop_strength = Some(0.6);
    send(&mut engine, at(1.0), drop).unwrap();
    let measured = colours(&engine.render_at(at(1.0)).unwrap());
    assert_eq!(
        measured.0,
        Rgb {
            red: 0.5,
            green: 0.4,
            blue: 0.12
        }
    );
    assert_eq!(
        measured.1,
        Rgb {
            red: 0.25,
            green: 0.1,
            blue: 1.0
        },
        "pending: no drop strength"
    );
    // A beat without features holds the last measurement.
    beat(&mut engine, 1);
    let started = colours(&engine.render_at(at(4.0)).unwrap());
    assert_eq!(
        started.0,
        Rgb {
            red: 0.5,
            green: 0.4,
            blue: 0.12
        }
    );
    assert_eq!(
        started.1,
        Rgb {
            red: 0.25,
            green: 0.1,
            blue: 0.3
        }
    );
    assert!(
        engine
            .apply(
                at(4.0),
                &Command::Beat {
                    beat_index: 5,
                    bar_phase: None,
                    beats_per_bar: 4,
                    bpm: 120.0,
                    features: Some(BeatFeatures {
                        onset: 1.5,
                        ..Default::default()
                    }),
                },
            )
            .is_err()
    );
}

#[test]
fn a_pending_cue_before_any_beat_is_unresolved_then_resolves_late_on_a_bar_line() {
    let mut engine = engine();
    select(&mut engine, 0.0, "other");
    send(
        &mut engine,
        0.0,
        section(2, SectionIntent::Drop, &[("probe", 4)]),
    )
    .unwrap();
    let unresolved = probe(&mut engine, 0.0);
    assert_eq!((unresolved.next_intent, unresolved.until_next), (0.0, None));
    beat_at(&mut engine, at(1.0), 3, 3);
    let resolved = probe(&mut engine, 1.0);
    assert_eq!(resolved.next_intent, 0.8);
    near(resolved.until_next, 1.0);
    let started = probe(&mut engine, 2.0);
    near(started.section_elapsed, 2.0);
}

#[test]
fn overlay_instances_run_their_window_then_release_and_repeat_on_phrase_lines() {
    let mut engine = engine();
    beat(&mut engine, 0);
    let mut drop = section(8, SectionIntent::Drop, &[("probe", 16)]);
    drop.overlays = vec![overlay(-4.0, 4.0, 0, 2.0), overlay(1.0, 1.0, 2, 0.0)];
    send(&mut engine, at(0.5), drop).unwrap();
    for (beat, live) in [
        (3.9, 0),
        (4.0, 1),
        (7.9, 1),
        (8.0, 1),
        (9.0, 2),
        (9.9, 2),
        (10.0, 0),
        (16.9, 0),
        (17.0, 1),
        (18.0, 0),
    ] {
        probe(&mut engine, beat);
        assert_eq!(engine.active_overlay_count(), live, "beat {beat}");
    }
}

#[test]
fn overlays_beyond_the_cap_are_not_started_and_say_why() {
    let mut engine = engine();
    beat(&mut engine, 0);
    let mut first = section(4, SectionIntent::Drop, &[("probe", 16)]);
    first.overlays = vec![overlay(0.0, 16.0, 0, 0.0); MAX_ACTIVE_OVERLAYS];
    send(&mut engine, at(0.5), first).unwrap();
    let mut next = section(12, SectionIntent::Breakdown, &[("probe", 16)]);
    next.overlays = vec![overlay(-2.0, 2.0, 0, 0.0)];
    send(&mut engine, at(0.5), next).unwrap();
    probe(&mut engine, 4.0);
    assert_eq!(engine.active_overlay_count(), MAX_ACTIVE_OVERLAYS);
    assert_eq!(engine.overlay_error(), None);
    probe(&mut engine, 10.0);
    assert_eq!(engine.active_overlay_count(), MAX_ACTIVE_OVERLAYS);
    assert!(engine.overlay_error().unwrap().contains("not started"));
    let frame = engine.render_at(at(10.0)).unwrap();
    assert!(
        frame.script_error.is_none(),
        "a refused overlay never stops the show"
    );
}

#[test]
fn pause_drops_releasing_overlays() {
    let mut engine = engine();
    beat(&mut engine, 0);
    let mut cue = section(4, SectionIntent::Drop, &[("probe", 16)]);
    cue.overlays = vec![overlay(0.0, 1.0, 0, 8.0)];
    send(&mut engine, at(0.5), cue).unwrap();
    probe(&mut engine, 4.5);
    probe(&mut engine, 5.5);
    assert_eq!(engine.active_overlay_count(), 1);
    engine
        .apply(at(5.5), &Command::SetPaused { paused: true })
        .unwrap();
    assert_eq!(engine.active_overlay_count(), 0);
}

fn worst_case_cue() -> wire::CueSection {
    let id = "i".repeat(128);
    let crossfade = Some(wire::play_autoloop::Transition {
        crossfade_beats: Some(f64::MAX),
    });
    wire::CueSection {
        command_id: u64::MAX,
        start_beat_index: u64::MAX,
        intent: wire::SectionIntent::Breakdown as i32,
        rotation: vec![
            wire::cue_section::BaseLook {
                autoloop_id: id.clone(),
                bars: u32::MAX,
                transition: crossfade,
            };
            MAX_ROTATION
        ],
        overlays: vec![
            wire::cue_section::Overlay {
                autoloop_id: id.clone(),
                priority: i32::MIN,
                begin_beats: f64::MAX,
                length_beats: f64::MAX,
                every_bars: u32::MAX,
                attack_beats: f64::MAX,
                release_beats: f64::MAX,
            };
            MAX_OVERLAYS
        ],
        palette: id,
        drop_strength: Some(f64::MAX),
        seed: u32::MAX,
    }
}

#[test]
fn the_largest_representable_cue_fits_the_realtime_cap() {
    let cue = worst_case_cue();
    assert!(
        cue.encoded_len() <= MAX_MESSAGE_BYTES,
        "{}",
        cue.encoded_len()
    );
    let mut frame = Vec::new();
    ClientMessage::CueSection(cue).write(&mut frame).unwrap();
}

#[test]
fn a_wire_cue_resolves_autoloop_ids_and_needs_an_intent() {
    let mut engine = engine();
    beat(&mut engine, 0);
    let wire_cue = |intent: wire::SectionIntent, id: &str| {
        ClientMessage::CueSection(wire::CueSection {
            command_id: 1,
            start_beat_index: 4,
            intent: intent as i32,
            rotation: vec![wire::cue_section::BaseLook {
                autoloop_id: id.into(),
                bars: 2,
                transition: None,
            }],
            overlays: Vec::new(),
            palette: "techno".into(),
            drop_strength: None,
            seed: 3,
        })
    };
    assert!(
        apply_command(
            &mut engine,
            at(0.5),
            &wire_cue(wire::SectionIntent::Unspecified, "probe")
        )
        .is_err()
    );
    assert!(
        apply_command(
            &mut engine,
            at(0.5),
            &wire_cue(wire::SectionIntent::Drop, "missing")
        )
        .is_err()
    );
    apply_command(
        &mut engine,
        at(0.5),
        &wire_cue(wire::SectionIntent::Drop, "probe"),
    )
    .unwrap();
    let started = probe(&mut engine, 4.0);
    assert_eq!((started.intent, started.seed), (0.8, 3.0));
    apply_command(
        &mut engine,
        at(4.5),
        &ClientMessage::ClearCues(wire::ClearCues { command_id: 2 }),
    )
    .unwrap();
    assert_eq!(probe(&mut engine, 4.5).section_elapsed, None);
}

/// The three numbers `expression` yields, through the left cell's colour.
fn lua(expression: &str, context: RenderContext) -> anyhow::Result<[f64; 3]> {
    let source = format!(
        "return {{render=function(ctx) local r, g, b = {expression}
         return {{sf.colour(sf.fixture('left'), sf.rgb(r, g, b))}} end}}"
    );
    let catalog = AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
        autoloop_id: "helpers".into(),
        bank: "test".into(),
        lua_source: source,
        intensity_htp: false,
    }])
    .unwrap();
    let rig = support::rig(serde_json::from_str(RIG).unwrap());
    let palettes = support::palettes();
    let mut script = catalog
        .instantiate("test", "helpers", &rig, &palettes)
        .unwrap();
    let effects = script.render(&context, &palettes.slots("techno").unwrap())?;
    let colour = effects[0].values.colour.unwrap();
    Ok([colour.red, colour.green, colour.blue])
}

fn with(edit: impl FnOnce(&mut RenderContext)) -> RenderContext {
    let mut context = RenderContext::default();
    edit(&mut context);
    context
}

#[test]
fn sf_random_is_a_pure_hash_of_the_seed_and_integer_indices() {
    let seeded = |seed| with(|context| context.seed = seed);
    let [first, again, other_cell] = lua(
        "sf.random(ctx, 1, 2), sf.random(ctx, 1, 2), sf.random(ctx, 2, 2)",
        seeded(7),
    )
    .unwrap();
    assert_eq!(first, again);
    assert_ne!(first, other_cell);
    let [reseeded, ..] = lua("sf.random(ctx, 1, 2), 0, 0", seeded(8)).unwrap();
    assert_ne!(first, reseeded);
    let [unset, zeros, third] = lua(
        "sf.random(ctx), sf.random(ctx, 0, 0, 0), sf.random(ctx, 1, 2, 3)",
        seeded(7),
    )
    .unwrap();
    assert_eq!(unset, zeros);
    assert_ne!(third, first);
    for value in [first, other_cell, reseeded, unset] {
        assert!((0.0..1.0).contains(&value));
    }
    assert!(lua("sf.random(ctx, 1.5), 0, 0", seeded(7)).is_err());
    assert!(lua("sf.random(ctx, 'left'), 0, 0", seeded(7)).is_err());
}

#[test]
fn sf_countdown_steps_once_per_beat_over_the_last_beats() {
    let counting = |left| {
        lua(
            "(sf.countdown(ctx, 8) or 10) / 10, 0, 0",
            with(|context| context.beats_until_next = left),
        )
        .unwrap()[0]
    };
    assert_eq!(counting(None), 1.0);
    assert_eq!(counting(Some(8.5)), 1.0);
    assert_eq!(counting(Some(8.0)), 0.8);
    assert_eq!(counting(Some(7.2)), 0.8);
    assert_eq!(counting(Some(7.0)), 0.7);
    assert_eq!(counting(Some(0.3)), 0.1);
    assert_eq!(counting(Some(0.0)), 0.0);
}

#[test]
fn sf_tension_eases_in_then_bends_to_the_known_change_without_a_jump() {
    let tension = |section: Option<f64>, entry: f64, left: Option<f64>, known: Option<f64>| {
        lua(
            "0, sf.tension(ctx)",
            with(|context| {
                context.section_elapsed_beats = section;
                context.autoloop_elapsed_beats = entry;
                context.beats_until_next = left;
                context.next_known_at = known;
            }),
        )
        .unwrap()
    };
    // Unknown change: (elapsed / 128)^3, the entry's beats without a section.
    assert_eq!(tension(Some(64.0), 0.0, None, None)[1..], [0.125, 0.125]);
    assert_eq!(tension(None, 64.0, None, None)[1..], [0.125, 0.125]);
    assert_eq!(tension(Some(200.0), 0.0, None, None)[1..], [1.0, 1.0]);
    let [_, level, stepped] = tension(Some(66.0), 0.0, None, None);
    assert!(
        level > 0.125 && stepped == 0.125,
        "the second value is the last downbeat's"
    );
    // The change becomes known at 96 with 32 beats left: no jump there, full at the change.
    let eased = 0.75_f64.powi(3);
    assert_eq!(tension(Some(96.0), 0.0, Some(32.0), Some(96.0))[1], eased);
    let halfway = eased + (1.0 - eased) * 0.5;
    assert_eq!(
        tension(Some(112.0), 0.0, Some(16.0), Some(96.0))[1..],
        [halfway, halfway]
    );
    let [_, level, stepped] = tension(Some(114.0), 0.0, Some(14.0), Some(96.0));
    assert!(level > halfway && stepped == halfway);
    assert_eq!(tension(Some(128.0), 0.0, Some(0.0), Some(96.0))[1], 1.0);
}

#[test]
fn next_known_at_is_the_section_time_the_next_cue_arrived() {
    const KNOWN: &str = r#"return {render = function(ctx)
      return {sf.colour(sf.fixture("left"), sf.rgb((ctx.next_known_at or 1000) / 1000, 0, 0))}
    end}"#;
    let mut engine = engine_with(&[("known", KNOWN)]);
    beat(&mut engine, 0);
    send(
        &mut engine,
        at(0.5),
        section(4, SectionIntent::Buildup, &[("known", 16)]),
    )
    .unwrap();
    let known = |engine: &mut Engine, beat: f64| {
        colours(&engine.render_at(at(beat)).unwrap()).0.red * 1000.0
    };
    assert_eq!(
        known(&mut engine, 20.0),
        1000.0,
        "nil while the change is unknown"
    );
    send(
        &mut engine,
        at(20.0),
        section(36, SectionIntent::Drop, &[("known", 16)]),
    )
    .unwrap();
    for beat in [20.0, 24.0, 30.5] {
        assert!(
            (known(&mut engine, beat) - 16.0).abs() < 1e-9,
            "beat {beat}"
        );
    }
}

#[test]
fn sf_finale_hands_over_continuously_and_its_last_bar_is_full() {
    let finale = |left: Option<f64>| {
        lua(
            "sf.finale(ctx, 16), sf.finale(ctx, 16, 0.7)",
            with(|context| context.beats_until_next = left),
        )
        .unwrap()
    };
    // No next cue, or one further than the finale: exactly the base level, so no jump.
    assert_eq!(finale(None), [0.0, 0.7, 0.7]);
    assert_eq!(finale(Some(20.0))[..2], [0.0, 0.7]);
    assert_eq!(finale(Some(16.0))[..2], [0.0, 0.7]);
    let [bare, driven, stepped] = finale(Some(7.0));
    assert!((bare - 9.0 / 16.0).abs() < 1e-12);
    assert!((driven - (0.7 + 0.3 * (9.0 / 16.0))).abs() < 1e-12);
    assert!(
        (stepped - (0.7 + 0.3 * 0.75)).abs() < 1e-12,
        "each bar at its end's level"
    );
    assert_eq!(finale(Some(3.5))[2], 1.0, "the last bar is full");
}

#[test]
fn phrase_helpers_count_from_the_section_start() {
    let at = |section: Option<f64>, beat: f64, left: Option<f64>| {
        with(|context| {
            context.section_elapsed_beats = section;
            context.beat_position = beat;
            context.bar_index = (beat / 4.0) as u64;
            context.beats_until_next = left;
        })
    };
    const BAR_AND_PHRASE: &str = "sf.bar(ctx) / 100,
        (function() local index, phase = sf.phrase(ctx, 4) return index / 100, phase end)()";
    let [bar, index, phase] = lua(BAR_AND_PHRASE, at(Some(36.0), 100.0, None)).unwrap();
    assert_eq!((bar, index, phase), (0.09, 0.02, 0.25));
    let [bar, index, phase] = lua(BAR_AND_PHRASE, at(None, 100.0, None)).unwrap();
    assert_eq!((bar, index, phase), (0.25, 0.06, 0.25));
    let until = |left| {
        lua(
            "(sf.bars_until_next(ctx) or 10) / 10, 0, 0",
            at(None, 0.0, left),
        )
    };
    assert_eq!(until(Some(6.0)).unwrap()[0], 0.15);
    assert_eq!(until(None).unwrap()[0], 1.0);
}

#[test]
fn render_cannot_keep_state_in_globals() {
    assert!(lua("0, 0, 0", RenderContext::default()).is_ok());
    let error = lua(
        "(function() leak = 1 end)() or 0, 0, 0",
        RenderContext::default(),
    )
    .unwrap_err();
    assert!(format!("{error:#}").contains("read-only"), "{error:#}");
    assert!(
        lua(
            "(function() sf = nil end)() or 0, 0, 0",
            RenderContext::default()
        )
        .is_err()
    );
}

#[test]
fn music_helpers_read_the_beat_and_fall_back_to_a_mid_level() {
    let [onset, bass, hz] = lua(
        "sf.onset(ctx), sf.band(ctx, 'bass'), sf.beat_hz(ctx, 2) / 10",
        with(|context| {
            context.onset = Some(0.9);
            context.bpm = 150.0;
        }),
    )
    .unwrap();
    assert_eq!((onset, bass, hz), (0.9, 0.5, 0.5));
    assert!(lua("sf.band(ctx, 'treble'), 0, 0", RenderContext::default()).is_err());
    let [intent_known, elapsed, _] = lua(
        "(sf.section(ctx) == 'drop') and 1 or 0, select(2, sf.section(ctx)) / 100, 0",
        with(|context| {
            context.section_intent = Some(SectionIntent::Drop);
            context.section_elapsed_beats = Some(12.0);
        }),
    )
    .unwrap();
    assert_eq!((intent_known, elapsed), (1.0, 0.12));
}

/// Beat `beat` of a clock whose index n is grid beat n and that never re-anchored.
fn sample(beat: f64) -> CueSample {
    CueSample {
        beat_position: beat,
        elapsed_musical_beats: beat,
        beats_per_bar: 4,
        anchor: Some((0, 0)),
    }
}

#[test]
fn a_late_cue_skips_its_preroll_as_past_and_enters_with_its_entry_rows() {
    let mut timeline = CueTimeline::default();
    let mut late = section(4, SectionIntent::Drop, &[("probe", 16)]);
    late.overlays = vec![overlay(-1.0, 1.0, 0, 0.0), overlay(0.0, 2.0, 0, 0.0)];
    timeline.receive(late, sample(5.0)).unwrap();
    for beat in [5.0, 7.0, 7.5] {
        timeline.advance(sample(beat));
        assert_eq!(
            timeline.active_overlay_count(),
            0,
            "beat {beat}: no dip mid-bar"
        );
    }
    timeline.advance(sample(8.0));
    let [hit] = timeline.instances() else {
        panic!("only the entry hit plays")
    };
    assert_eq!(hit.overlay.begin_beats, 0.0);
    assert_eq!(hit.nominal_end(&sample(8.0)), Some(10.0));
}

#[test]
fn a_resent_cue_carries_its_running_overlays_on() {
    let mut timeline = CueTimeline::default();
    let mut drop = section(16, SectionIntent::Drop, &[("probe", 16)]);
    drop.overlays = vec![overlay(-8.0, 8.0, 0, 2.0), overlay(0.0, 6.0, 2, 0.0)];
    timeline.receive(drop.clone(), sample(1.0)).unwrap();
    timeline.advance(sample(10.0));
    let riser = timeline.instances()[0].clone();
    assert!(matches!(riser.state, InstanceState::Running(_)));
    // A level change re-sends the cue: the riser neither restarts nor releases.
    drop.seed = 9;
    timeline.receive(drop.clone(), sample(10.0)).unwrap();
    timeline.advance(sample(10.5));
    let [carried] = timeline.instances() else {
        panic!("one riser")
    };
    assert_eq!((carried.order, carried.state), (riser.order, riser.state));
    assert_eq!(carried.seed, 9);

    timeline.advance(sample(17.0));
    let fill = timeline
        .instances()
        .iter()
        .find(|instance| instance.overlay.every_bars == 2)
        .unwrap()
        .clone();
    timeline.receive(drop, sample(17.0)).unwrap();
    // The re-sent cue takes over on the next bar line; the fill playing then carries on.
    timeline.advance(sample(20.0));
    let fills: Vec<_> = timeline
        .instances()
        .iter()
        .filter(|instance| instance.overlay.every_bars == 2)
        .collect();
    assert_eq!(fills.len(), 1);
    assert_eq!((fills[0].order, fills[0].state), (fill.order, fill.state));
    timeline.advance(sample(24.0));
    assert_eq!(
        timeline
            .instances()
            .iter()
            .filter(|instance| instance.overlay.every_bars == 2)
            .count(),
        1,
        "the next repetition starts once"
    );
}

#[test]
fn a_stopped_row_never_repeats_while_its_cue_plays() {
    let mut timeline = CueTimeline::default();
    let mut drop = section(0, SectionIntent::Drop, &[("probe", 16)]);
    drop.overlays = vec![overlay(0.0, 1.0, 1, 0.0)];
    timeline.receive(drop, sample(0.0)).unwrap();
    timeline.advance(sample(0.0));
    let order = timeline.instances()[0].order;
    timeline.stop_row(order);
    for beat in [0.5, 4.0, 8.0, 12.0] {
        timeline.advance(sample(beat));
        assert_eq!(timeline.instances().len(), 0, "beat {beat}");
    }
}

#[test]
fn a_resent_cue_is_no_next_section_and_the_dip_sets_the_finale_target() {
    let mut timeline = CueTimeline::default();
    let buildup = section(0, SectionIntent::Buildup, &[("probe", 16)]);
    timeline.receive(buildup.clone(), sample(0.0)).unwrap();
    timeline.advance(sample(0.0));
    timeline.receive(buildup, sample(2.0)).unwrap();
    let context = timeline.context(&sample(2.0));
    assert_eq!(
        (context.next_intent, context.beats_until_next),
        (None, None)
    );

    let mut drop = section(32, SectionIntent::Drop, &[("probe", 16)]);
    drop.overlays = vec![
        OverlayCue {
            bank: "dip".into(),
            ..overlay(-2.0, 2.0, 0, 0.0)
        },
        overlay(-16.0, 16.0, 0, 0.0),
    ];
    timeline.receive(drop, sample(3.0)).unwrap();
    let context = timeline.context(&sample(20.0));
    assert_eq!(context.next_intent, Some(SectionIntent::Drop));
    assert_eq!(
        (context.beats_until_next, context.beats_until_dip),
        (Some(12.0), Some(10.0))
    );
    assert_eq!(timeline.context(&sample(31.0)).beats_until_dip, Some(0.0));
}

#[test]
fn a_resent_cue_keeps_the_running_loops_seed() {
    let mut engine = engine();
    beat(&mut engine, 0);
    send(
        &mut engine,
        at(0.5),
        section(4, SectionIntent::Drop, &[("probe", 16)]),
    )
    .unwrap();
    assert_eq!(probe(&mut engine, 5.0).seed, 7.0);
    let mut recue = section(4, SectionIntent::Drop, &[("probe", 16)]);
    recue.seed = 11;
    send(&mut engine, at(5.0), recue).unwrap();
    assert_eq!(
        probe(&mut engine, 9.0).seed,
        7.0,
        "sf.random never jumps under a loop"
    );
}

#[test]
fn a_selection_without_a_cue_counts_phrase_time_from_its_bar_line() {
    let mut engine = engine();
    beat(&mut engine, 0);
    select(&mut engine, at(6.25), "probe");
    let frame = probe(&mut engine, 7.0);
    near(frame.section_elapsed, 3.0);
    near(frame.autoloop_elapsed, 0.75);
    assert_eq!(frame.intent, 0.0, "no section intent without a cue");
}

#[test]
fn metrics_at_their_largest_fit_the_realtime_cap() {
    use starfire::cue::MAX_OVERLAY_ERROR_BYTES;
    use starfire::engine::MAX_SCRIPT_ERROR_BYTES;
    use starfire::protocol::MAX_ERROR_BYTES;
    let metrics = wire::Metrics {
        elapsed_seconds: f64::MAX,
        rendered_frames: u64::MAX,
        missed_render_deadlines: u64::MAX,
        queued_commands: u32::MAX,
        last_render_duration_seconds: f64::MAX,
        active_bank: "b".repeat(64),
        active_autoloop: "a".repeat(128),
        script_error: "s".repeat(MAX_SCRIPT_ERROR_BYTES),
        runtime_error: "r".repeat(MAX_ERROR_BYTES),
        last_processed_command_id: u64::MAX,
        run_id: u64::MAX,
        pending_cues: u32::MAX,
        overlay_error: "o".repeat(MAX_OVERLAY_ERROR_BYTES),
        active_overlay_count: u32::MAX,
    };
    assert!(
        metrics.encoded_len() <= MAX_MESSAGE_BYTES,
        "{}",
        metrics.encoded_len()
    );
}

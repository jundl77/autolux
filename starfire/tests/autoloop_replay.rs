mod support;

use std::fmt;
use std::fs;
use std::path::PathBuf;

use starfire::engine::{Engine, RenderedFrame};
use starfire::fixtures::FixtureRig;
use starfire::model::{
    BeatFeatures, Command, FixtureState, RenderContext, Rgb, SectionIntent, Transition,
};
use starfire::protocol::wire;
use starfire::replay::{Replay, dmx_digest, render_replay};
use starfire::script::AutoloopCatalog;

const FLOAT_TOLERANCE: f64 = 1e-9;
const GOLDEN_INTENSITY: f64 = 1.0;
const SOFT_INTENSITY: f64 = 0.3;
const REPLAY_BPM: f64 = 120.0;
const SECONDS_PER_BEAT: f64 = 60.0 / REPLAY_BPM;
const MIN_REPLAY_SECONDS: f64 = 16.0 * 4.0 * SECONDS_PER_BEAT;
const RAMP_PLATEAU_BEATS: f64 = 128.0;
const WRITE_GOLDEN_VAR: &str = "STARFIRE_WRITE_GOLDEN";
const STANDBY_BANK: &str = "standby";
const WHITE: Rgb = Rgb {
    red: 1.0,
    green: 1.0,
    blue: 1.0,
};
const OVERLAY_BANKS: [&str; 5] = ["hit", "fill", "riser", "dip", "accent"];
const LOOP_GROUPS: [&str; 3] = ["wash", "strip", "movers"];
/// Base loops leave the headroom above for HTP hits and risers.
const BASE_PEAK: f64 = 0.9;
/// A level swing smaller than this is shimmer, not a pulse.
const PULSE_DEPTH: f64 = 0.05;
/// Half-beat steps are the fastest a base loop pulses; faster reads as a strobe.
const MAX_PULSES_PER_BEAT: f64 = 2.0;
/// The finale replay: the drop is cued at beat 92 for beat 128, its riser runs from 111 and its
/// dip blacks out beat 127.
const DROP_BEAT: f64 = 128.0;
const DIP_BEAT: f64 = 127.0;
const RISER_BEAT: f64 = 111.0;
const COUNT_STEPS: usize = 8;
/// Heads slew back to the base after a moving overlay ends; compare once they are home.
const OVERLAY_SETTLE_BEATS: f64 = 4.0;
/// Worst frames on the stage rig, well inside the 100k per-script budget (B1): a heavier loop
/// is a regression to look at, not a bound to raise.
const LEAN_BASE_INSTRUCTIONS: u32 = 8_000;
const LEAN_OVERLAY_INSTRUCTIONS: u32 = 4_000;

struct Look {
    bank: String,
    autoloop: String,
    /// 0 for an overlay, which has a length per cue instead.
    loop_beats: f64,
    source: String,
}

impl Look {
    fn ramps(&self) -> bool {
        self.bank == "buildup"
    }

    fn is_overlay(&self) -> bool {
        OVERLAY_BANKS.contains(&self.bank.as_str())
    }

    /// An overlay's `-- strobe: yes`; base loops never strobe.
    fn strobes(&self) -> bool {
        self.source
            .lines()
            .any(|line| line.trim_end() == "-- strobe: yes")
    }

    fn follows_music(&self) -> bool {
        self.bank != STANDBY_BANK
    }

    fn engine(&self, select_beat: f64) -> Engine {
        engine(&self.bank, &self.autoloop, select_beat)
    }
}

impl fmt::Display for Look {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}/{}", self.bank, self.autoloop)
    }
}

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

/// Every base loop (the overlays have their own replays and checks).
fn looks() -> Vec<Look> {
    every_look()
        .into_iter()
        .filter(|look| !look.is_overlay())
        .collect()
}

fn overlays() -> Vec<Look> {
    every_look().into_iter().filter(Look::is_overlay).collect()
}

fn every_look() -> Vec<Look> {
    support::show_catalog()
        .list()
        .into_iter()
        .map(|entry| {
            let path = root().join(format!(
                "../show/autoloops/{}/{}.lua",
                entry.bank, entry.autoloop
            ));
            let source = fs::read_to_string(path).unwrap();
            if OVERLAY_BANKS.contains(&entry.bank.as_str()) {
                return Look {
                    bank: entry.bank,
                    autoloop: entry.autoloop,
                    loop_beats: 0.0,
                    source,
                };
            }
            let loop_beats = source
                .lines()
                .next()
                .and_then(|line| line.strip_prefix("-- "))
                .and_then(|line| line.split_once("-beat loop"))
                .and_then(|(beats, _)| beats.parse::<f64>().ok())
                .filter(|beats| *beats == 8.0 || *beats == 16.0);
            Look {
                loop_beats: loop_beats.unwrap_or_else(|| {
                    panic!(
                        "{}/{}: header must start with `-- 8-beat loop` or `-- 16-beat loop`",
                        entry.bank, entry.autoloop
                    )
                }),
                bank: entry.bank,
                autoloop: entry.autoloop,
                source,
            }
        })
        .collect()
}

const META_KEYS: [(&str, &[&str]); 3] = [
    ("energy", &["1", "2", "3"]),
    ("movement", &["still", "sweep", "snap"]),
    ("texture", &["wash", "beam", "pixel", "mixed"]),
];

fn metadata(look: &Look) -> Result<[String; 3], String> {
    let mut values: [Option<String>; 3] = Default::default();
    let block = look.source.lines().skip(1).map_while(|line| {
        let (key, value) = line.strip_prefix("-- ")?.split_once(": ")?;
        let slot = META_KEYS.iter().position(|(name, _)| *name == key)?;
        Some((slot, key, value))
    });
    for (slot, key, value) in block {
        if !META_KEYS[slot].1.contains(&value) {
            return Err(format!(
                "`{key}: {value}` is not one of {:?}",
                META_KEYS[slot].1
            ));
        }
        if values[slot].replace(value.to_owned()).is_some() {
            return Err(format!("`{key}` declared twice"));
        }
    }
    let missing: Vec<_> = META_KEYS
        .iter()
        .zip(&values)
        .filter(|(_, value)| value.is_none())
        .map(|((key, _), _)| *key)
        .collect();
    if !missing.is_empty() {
        return Err(format!(
            "missing metadata {missing:?} after the loop-length line"
        ));
    }
    Ok(values.map(Option::unwrap))
}

fn looks_in(banks: &[&str]) -> Vec<Look> {
    let looks: Vec<_> = every_look()
        .into_iter()
        .filter(|look| banks.contains(&look.bank.as_str()))
        .collect();
    assert!(!looks.is_empty(), "no autoloops in {banks:?}");
    looks
}

fn replay_for(look: &Look) -> Replay {
    let own = root().join(format!(
        "tests/replays/{}/{}.json",
        look.bank, look.autoloop
    ));
    if own.exists() {
        return Replay::load(&own).unwrap();
    }
    let default = root().join(format!("tests/replays/{}.json", look.bank));
    let replay = Replay::load(&default)
        .unwrap_or_else(|error| panic!("{look}: no replay at {own:?} or {default:?}: {error}"));
    substitute(replay, look)
}

/// The finale replay with the buildup under test as the build.
fn finale_for(look: &Look) -> Replay {
    substitute(
        Replay::load(&root().join("tests/replays/buildup_finale.json")).unwrap(),
        look,
    )
}

/// The look under test takes the place of its bank's loop: in selections, in the first rotation
/// entry and in the first overlay row.
fn substitute(mut replay: Replay, look: &Look) -> Replay {
    for event in &mut replay.events {
        match &mut event.command {
            Command::Select { bank, autoloop, .. } if *bank == look.bank => {
                *autoloop = look.autoloop.clone();
            }
            Command::CueSection { value } => {
                let first_entry = value
                    .rotation
                    .first_mut()
                    .filter(|entry| entry.bank == look.bank);
                let first_row = value
                    .overlays
                    .first_mut()
                    .filter(|row| row.bank == look.bank);
                if let Some(entry) = first_entry {
                    entry.autoloop = look.autoloop.clone();
                }
                if let Some(row) = first_row {
                    row.autoloop = look.autoloop.clone();
                }
            }
            _ => {}
        }
    }
    replay
}

/// The same replay without the named overlay banks' rows.
fn without_overlays(mut replay: Replay, banks: &[&str]) -> Replay {
    for event in &mut replay.events {
        if let Command::CueSection { value } = &mut event.command {
            value
                .overlays
                .retain(|row| !banks.contains(&row.bank.as_str()));
        }
    }
    replay
}

/// The (bank, autoloop) pairs the replay plays as its base.
fn base_looks_of(replay: &Replay) -> Vec<(String, String)> {
    replay
        .events
        .iter()
        .flat_map(|event| match &event.command {
            Command::Select { bank, autoloop, .. } => vec![(bank.clone(), autoloop.clone())],
            Command::CueSection { value } => value
                .rotation
                .iter()
                .map(|entry| (entry.bank.clone(), entry.autoloop.clone()))
                .collect(),
            _ => Vec::new(),
        })
        .collect()
}

fn loop_cells() -> Vec<String> {
    let groups = support::stage().groups().clone();
    LOOP_GROUPS
        .iter()
        .flat_map(|group| groups[*group].clone())
        .collect()
}

/// The summed intensity of every cell a loop drives.
fn total_light(frame: &RenderedFrame, cells: &[String]) -> f64 {
    cells
        .iter()
        .map(|id| frame.fixture_properties_before_strobe[id].intensity)
        .sum()
}

fn frames_between(frames: &[RenderedFrame], from: f64, to: f64) -> Vec<&RenderedFrame> {
    frames
        .iter()
        .filter(|frame| frame.beat_position >= from && frame.beat_position < to)
        .collect()
}

fn check_golden(look: &Look, digest: &str) {
    let path = root().join(format!(
        "tests/golden/{}/{}.sha256",
        look.bank, look.autoloop
    ));
    let Ok(expected) = fs::read_to_string(&path) else {
        return;
    };
    if std::env::var(WRITE_GOLDEN_VAR).as_deref() == Ok("1") {
        fs::write(&path, format!("{digest}\n")).unwrap();
        return;
    }
    assert_eq!(
        digest,
        expected.trim(),
        "{look}: review semantic checks, then re-record with {WRITE_GOLDEN_VAR}=1"
    );
}

fn engine(bank: &str, autoloop: &str, select_beat: f64) -> Engine {
    engine_at_intensity(bank, autoloop, select_beat, GOLDEN_INTENSITY)
}

fn engine_at_intensity(bank: &str, autoloop: &str, select_beat: f64, intensity: f64) -> Engine {
    let mut engine = Engine::new(
        support::stage(),
        support::show_catalog(),
        support::palettes(),
    )
    .unwrap();
    engine
        .apply(0.0, &Command::SetIntensity { level: intensity })
        .unwrap();
    let anchor_beat = select_beat.floor();
    engine
        .apply(
            0.0,
            &Command::Beat {
                beat_index: anchor_beat as u64,
                bar_phase: Some((anchor_beat as u32) % 4),
                beats_per_bar: 4,
                bpm: REPLAY_BPM,
                features: None,
            },
        )
        .unwrap();
    engine
        .apply(
            (select_beat - anchor_beat) * SECONDS_PER_BEAT,
            &Command::Select {
                bank: bank.into(),
                autoloop: autoloop.into(),
                transition: Transition::Cut,
                palette: "techno".into(),
            },
        )
        .unwrap();
    engine
}

fn render(engine: &mut Engine, beats_after_anchor: f64) -> RenderedFrame {
    engine
        .render_at(beats_after_anchor * SECONDS_PER_BEAT)
        .unwrap()
}

fn wash_level(engine: &mut Engine, beats_after_anchor: f64) -> f64 {
    render(engine, beats_after_anchor).fixture_properties_before_strobe
        [&support::stage().groups()["wash"][0]]
        .intensity
}

fn set_rms(engine: &mut Engine, level: f64) {
    engine
        .apply(0.0, &Command::SetNormalizedRmsLevel { level })
        .unwrap();
}

fn same_state(first: &FixtureState, second: &FixtureState) -> bool {
    [
        (first.intensity, second.intensity),
        (first.pan, second.pan),
        (first.tilt, second.tilt),
        (first.colour.red, second.colour.red),
        (first.colour.green, second.colour.green),
        (first.colour.blue, second.colour.blue),
    ]
    .iter()
    .all(|(left, right)| (left - right).abs() <= FLOAT_TOLERANCE)
}

fn check_replay(look: &Look) {
    let replay = replay_for(look);
    assert!(
        replay.duration_seconds >= MIN_REPLAY_SECONDS,
        "{look}: replay under 16 bars"
    );
    let first = render_replay(&replay, support::stage(), support::show_catalog()).unwrap();
    let cell_count = support::stage().cell_ids().len();
    let bases = base_looks_of(&replay);
    let mut strobed = false;
    for frame in &first {
        assert!(
            frame.script_error.is_none(),
            "{look}: {:?}",
            frame.script_error
        );
        let active = (
            frame.active_bank.clone().unwrap_or_default(),
            frame.active_autoloop.clone().unwrap_or_default(),
        );
        assert!(
            bases.contains(&active),
            "{look}: {active:?} is not the replay's base"
        );
        if !look.is_overlay() {
            assert_eq!(active.0, look.bank, "{look}: another bank took over");
        }
        assert_eq!(frame.fixture_properties_before_strobe.len(), cell_count);
        for (id, state) in &frame.fixture_properties_before_strobe {
            assert!(
                state.strobe_hz == 0.0 || look.strobes(),
                "{look} strobes {id} but does not declare `-- strobe: yes`"
            );
            strobed |= state.strobe_hz > 0.0;
        }
    }
    assert_eq!(
        strobed,
        look.strobes(),
        "{look}: its strobe header and its frames disagree"
    );

    let second = render_replay(&replay, support::stage(), support::show_catalog()).unwrap();
    let digest = dmx_digest(&first);
    assert_eq!(digest, dmx_digest(&second), "{look} is not deterministic");

    // Duplicate selections and re-sent cues cannot restart the current autoloop.
    let mut without_duplicate = replay.clone();
    let mut seen = Vec::new();
    without_duplicate.events.retain(|event| {
        let repeat = matches!(
            event.command,
            Command::Select { .. } | Command::CueSection { .. }
        ) && seen.contains(&event.command);
        seen.push(event.command.clone());
        !repeat
    });
    let continued = render_replay(
        &without_duplicate,
        support::stage(),
        support::show_catalog(),
    )
    .unwrap();
    assert_eq!(
        digest,
        dmx_digest(&continued),
        "{look} restarted on reselect"
    );

    // Frame rate changes sampling density, never the authored effect at a given time.
    let mut slower = replay.clone();
    slower.frame_rate_hz = replay.frame_rate_hz / 2;
    let slower = render_replay(&slower, support::stage(), support::show_catalog()).unwrap();
    for (index, frame) in slower.iter().enumerate() {
        assert_eq!(
            frame.dmx_frames_by_universe,
            first[index * 2].dmx_frames_by_universe,
            "{look} depends on frame rate"
        );
    }

    check_golden(look, &digest);
}

#[test]
fn every_autoloop_replays_deterministically_and_matches_any_golden() {
    let looks = every_look();
    std::thread::scope(|scope| {
        for look in &looks {
            scope.spawn(move || check_replay(look));
        }
    });
}

#[test]
fn every_autoloop_repeats_on_its_declared_loop_length() {
    for look in looks() {
        let start = if look.ramps() {
            RAMP_PLATEAU_BEATS
        } else {
            0.0
        };
        let (mut first, mut repeated) = (look.engine(0.0), look.engine(0.0));
        for step in 0..(2.0 * look.loop_beats / 0.07) as usize {
            let beat = start + step as f64 * 0.07;
            let a = render(&mut first, beat);
            let b = render(&mut repeated, beat + look.loop_beats);
            for (id, state) in &a.fixture_properties_before_strobe {
                assert!(
                    same_state(state, &b.fixture_properties_before_strobe[id]),
                    "{look}: {id} changed after one loop at beat {beat}"
                );
            }
        }
    }
}

#[test]
fn every_autoloop_declares_selection_metadata_that_fits_its_bank() {
    let mut problems = Vec::new();
    for look in looks().into_iter().filter(Look::follows_music) {
        match metadata(&look) {
            Ok([energy, ..]) => {
                let energy: u8 = energy.parse().unwrap();
                let fits = match look.bank.as_str() {
                    "atmospheric" | "breakdown" => energy <= 2,
                    "drop" | "buildup" => energy >= 2,
                    _ => true,
                };
                if !fits {
                    problems.push(format!("{look}: energy {energy} does not fit its bank"));
                }
            }
            Err(error) => problems.push(format!("{look}: {error}")),
        }
    }
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}

#[test]
fn no_autoloop_ever_strobes_even_when_loud() {
    for look in looks() {
        let mut engine = look.engine(0.0);
        set_rms(&mut engine, 0.9);
        for step in 0..=48 * 4 * 4 {
            let frame = render(&mut engine, step as f64 * 0.25);
            for (id, state) in &frame.fixture_properties_before_strobe {
                assert_eq!(state.strobe_hz, 0.0, "{look}: {id} strobes at step {step}");
            }
        }
    }
}

#[test]
fn selecting_mid_bar_keeps_the_musical_phrase() {
    // Phrase time counts from the bar line at or before a selection, so a loop selected mid-bar
    // renders as if selected on that downbeat.
    for look in looks().iter().filter(|look| !look.ramps()) {
        let mut on_the_one = look.engine(0.0);
        let mut mid_bar = look.engine(2.25);
        for beat in [3.0, 3.1, 4.0, 6.5, 9.0, 13.7] {
            let expected = render(&mut on_the_one, beat);
            let actual = render(&mut mid_bar, beat - 2.0);
            assert_eq!(
                expected.dmx_frames_by_universe, actual.dmx_frames_by_universe,
                "{look} selected mid-bar left the phrase of its downbeat"
            );
        }
    }
}

#[test]
fn measured_energy_scales_levels_and_reach_but_not_colour_or_structure() {
    // Builds never follow loudness: their RMS falls as the music high-passes (C5).
    let rig = support::stage();
    for look in looks()
        .into_iter()
        .filter(|look| look.follows_music() && !look.ramps())
    {
        let mut engine = look.engine(4.0);
        set_rms(&mut engine, 1.0);
        let strong = engine.render_at(0.25).unwrap();
        engine
            .apply(0.25, &Command::SetNormalizedRmsLevel { level: 0.0 })
            .unwrap();
        let quiet = engine.render_at(0.25).unwrap();
        for (id, strong) in &strong.fixture_properties_before_strobe {
            let quiet = &quiet.fixture_properties_before_strobe[id];
            assert!(
                quiet.intensity <= strong.intensity + FLOAT_TOLERANCE,
                "{look}: {id}"
            );
            // Reach is how far a head points from its rest beam on its focus point.
            if let Some((pan, tilt)) = rig.focus(id).and_then(|focus| rig.aim_at(id, focus)) {
                let reach = |state: &FixtureState| (state.pan - pan).hypot(state.tilt - tilt);
                assert!(
                    reach(quiet) <= reach(strong) + FLOAT_TOLERANCE,
                    "{look}: {id} reaches further when quiet"
                );
            }
            assert_eq!(
                strong.colour, quiet.colour,
                "{look}: {id} colour follows RMS"
            );
            assert_eq!(strong.strobe_hz, quiet.strobe_hz);
        }
        assert_ne!(
            strong.dmx_frames_by_universe, quiet.dmx_frames_by_universe,
            "{look}"
        );
    }
}

#[test]
fn silence_drops_wash_to_a_tenth_of_loud_level() {
    for look in looks()
        .into_iter()
        .filter(|look| look.follows_music() && !look.ramps())
    {
        let mean_wash = |level: f64| {
            let mut engine = look.engine(0.0);
            set_rms(&mut engine, level);
            let steps = 32 * 4 * 4;
            (0..steps)
                .map(|step| wash_level(&mut engine, step as f64 * 0.25))
                .sum::<f64>()
                / steps as f64
        };
        let (silent, loud) = (mean_wash(0.0), mean_wash(0.9));
        assert!(
            silent < 0.15 * loud || (loud == 0.0 && silent == 0.0),
            "{look}: silent wash {silent} vs loud {loud}"
        );
    }
}

#[test]
fn standby_is_a_still_dim_warm_look_that_ignores_beat_and_loudness() {
    for look in looks_in(&[STANDBY_BANK]) {
        let mut engine = look.engine(0.0);
        let reference = render(&mut engine, 0.0);
        for (step, level) in [0.0, 0.9, 0.3, 1.0]
            .into_iter()
            .cycle()
            .take(64)
            .enumerate()
        {
            let beat = step as f64 * 0.37;
            engine
                .apply(
                    beat * SECONDS_PER_BEAT,
                    &Command::SetNormalizedRmsLevel { level },
                )
                .unwrap();
            let frame = render(&mut engine, beat);
            assert_eq!(
                frame.dmx_frames_by_universe, reference.dmx_frames_by_universe,
                "{look} moved with the music at beat {beat}"
            );
        }
        for (id, state) in &reference.fixture_properties_before_strobe {
            assert_eq!(state.strobe_hz, 0.0, "{look}: {id} strobes");
            assert!(state.intensity <= 0.2, "{look}: {id} is not dim");
            if state.intensity > 0.0 {
                assert_eq!(state.colour, WHITE, "{look}: {id} is not sf.white()");
            }
        }
        for id in &support::stage().groups()["wash"] {
            assert!(
                reference.fixture_properties_before_strobe[id].intensity > 0.0,
                "{look}: {id} is dark"
            );
        }
    }
}

fn peaks(levels: &[f64]) -> usize {
    let mut rising = false;
    let mut count = 0;
    for pair in levels.windows(2) {
        let change = pair[1] - pair[0];
        if change > FLOAT_TOLERANCE {
            rising = true;
        } else if change < -FLOAT_TOLERANCE {
            count += usize::from(rising);
            rising = false;
        }
    }
    count
}

#[test]
fn atmospheric_and_breakdown_washes_swell_at_most_once_per_bar() {
    const BEATS: usize = 64;
    const STEPS_PER_BEAT: usize = 8;
    for look in looks_in(&["atmospheric", "breakdown"]) {
        let mut engine = look.engine(0.0);
        let frames: Vec<_> = (0..=BEATS * STEPS_PER_BEAT)
            .map(|step| render(&mut engine, step as f64 / STEPS_PER_BEAT as f64))
            .collect();
        for id in &support::stage().groups()["wash"] {
            let levels: Vec<_> = frames
                .iter()
                .map(|frame| frame.fixture_properties_before_strobe[id].intensity)
                .collect();
            let count = peaks(&levels);
            assert!(
                count <= BEATS / 4,
                "{look}: {id} has {count} peaks in {BEATS} beats"
            );
        }
    }
}

#[test]
fn drop_washes_are_brightest_just_after_beat_edges() {
    for look in looks_in(&["drop"]) {
        let mut engine = look.engine(0.0);
        for beat in 0..16 {
            let hit = wash_level(&mut engine, beat as f64 + 0.1);
            let mid_beat = wash_level(&mut engine, beat as f64 + 0.5);
            let on_the_one = beat % 4 == 0;
            assert!(
                hit > mid_beat || (!on_the_one && hit >= mid_beat - FLOAT_TOLERANCE),
                "{look} beat {beat}: {hit} vs mid-beat {mid_beat}"
            );
        }
    }
}

#[test]
fn low_intensity_softens_drop_beat_edges_without_flattening_them() {
    for look in looks_in(&["drop"]) {
        let beat_contrast = |intensity: f64| {
            let mut engine = engine_at_intensity(&look.bank, &look.autoloop, 0.0, intensity);
            (0..16)
                .map(|beat| {
                    wash_level(&mut engine, beat as f64 + 0.1)
                        - wash_level(&mut engine, beat as f64 + 0.5)
                })
                .sum::<f64>()
        };
        let (full, soft) = (
            beat_contrast(GOLDEN_INTENSITY),
            beat_contrast(SOFT_INTENSITY),
        );
        assert!(
            soft < full && soft > FLOAT_TOLERANCE,
            "{look}: contrast {soft} at {SOFT_INTENSITY} vs {full} at {GOLDEN_INTENSITY}"
        );
    }
}

#[test]
fn drop_kick_washes_hit_on_every_beat() {
    let mut engine = engine("drop", "kick", 0.0);
    for beat in 0..16 {
        let hit = wash_level(&mut engine, beat as f64 + 0.1);
        let offbeat = wash_level(&mut engine, beat as f64 + 0.5);
        assert!(hit > offbeat, "beat {beat}: {hit} <= {offbeat}");
    }
}

fn inline_engine(autoloop_id: &str, source: &str) -> Engine {
    let catalog = AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
        autoloop_id: autoloop_id.into(),
        bank: "test".into(),
        lua_source: source.into(),
        intensity_htp: false,
    }])
    .unwrap();
    let mut engine = Engine::new(support::stage(), catalog, support::palettes()).unwrap();
    engine
        .apply(
            0.0,
            &Command::Beat {
                beat_index: 0,
                bar_phase: Some(0),
                beats_per_bar: 4,
                bpm: REPLAY_BPM,
                features: None,
            },
        )
        .unwrap();
    engine
        .apply(
            0.0,
            &Command::Select {
                bank: "test".into(),
                autoloop: autoloop_id.into(),
                transition: Transition::Cut,
                palette: "techno".into(),
            },
        )
        .unwrap();
    engine
}

fn run_inline(autoloop_id: &str, source: &str) -> RenderedFrame {
    let frame = inline_engine(autoloop_id, source).render_at(0.0).unwrap();
    assert!(frame.script_error.is_none(), "{:?}", frame.script_error);
    frame
}

#[test]
fn loudness_gain_floors_silence_and_saturates_when_loud() {
    run_inline(
        "gain",
        r#"
        local function near(a, b) return math.abs(a - b) < 1e-12 end
        assert(near(sf.loudness_gain(0), 0.08))
        assert(near(sf.loudness_gain(0.8), 1) and near(sf.loudness_gain(1), 1))
        assert(near(sf.loudness_gain(nil), sf.loudness_gain(0.6)))
        assert(sf.loudness_gain(0.2) < sf.loudness_gain(0.4))
        return { render = function(ctx) return {} end }
    "#,
    );
}

#[test]
fn palettes_swap_by_name_and_grid_helpers_follow_the_bar() {
    let frame = run_inline(
        "house",
        r#"
        assert(sf.beat_in_bar(5.5) == 1.5 and sf.bar(5.5) == 1)
        assert(sf.phrase_phase(8, 4) == 0.5 and sf.phrase_phase(16, 4) == 0)
        assert(sf.on_beat(3, 0.5) == 1 and sf.on_beat(3.5, 0.5) == 0)
        assert(sf.on_beat(3.5, 0.25, 0.5) == 1 and sf.on_beat(3.25, 0.25, 0.5) == 0)
        assert(sf.side(1, 2) == -1 and sf.side(2, 2) == 1)
        local mid = sf.mix(sf.rgb(0, 0, 1), sf.rgb(1, 0, 0), 0.25)
        assert(mid.red == 0.25 and mid.green == 0 and mid.blue == 0.75)
        return { render = function(ctx)
            return { sf.colour(sf.all(), sf.palette("house").cold), sf.dimmer(sf.all(), 1) }
        end }
    "#,
    );
    assert_eq!(
        frame.fixture_properties_before_strobe[&support::stage().groups()["strip"][0]].colour,
        Rgb {
            red: 0.0,
            green: 0.85,
            blue: 1.0
        }
    );
}

#[test]
fn the_same_autoloops_work_on_a_different_rig_through_named_groups() {
    let rig =
        FixtureRig::load(&root().join("../autolux/tests/data/generic_rig/generic.rig.pb")).unwrap();
    for look in every_look() {
        let mut replay = replay_for(&look);
        // Long enough for the overlay replays' rows, which begin at beat 16.
        replay.duration_seconds = if look.is_overlay() { 12.0 } else { 8.0 };
        replay
            .events
            .retain(|event| event.at_seconds <= replay.duration_seconds);
        let rendered = render_replay(&replay, rig.clone(), support::show_catalog()).unwrap();
        for frame in rendered {
            assert!(
                frame.script_error.is_none(),
                "{look}: {:?}",
                frame.script_error
            );
            assert_eq!(frame.dmx_frames_by_universe.len(), 2);
            assert!(
                frame
                    .fixture_properties_before_strobe
                    .values()
                    .all(|state| (0.0..=1.0).contains(&state.intensity)
                        && (state.strobe_hz == 0.0 || look.strobes())),
                "{look}"
            );
        }
    }
}

#[test]
fn one_loop_under_two_palettes_changes_only_colour() {
    let mut techno = engine("atmospheric", "dawn", 0.0);
    let mut house = engine("atmospheric", "dawn", 0.0);
    house
        .set_parameter(
            0.0,
            &wire::Parameter {
                r#type: wire::ParameterType::Palette as i32,
                value: Some(wire::parameter::Value::Text("house".into())),
                ..Default::default()
            },
        )
        .unwrap();
    let mut recoloured = 0;
    for step in 0..64 {
        let beats = f64::from(step) / 4.0;
        let warm = render(&mut techno, beats).fixture_properties_before_strobe;
        let cool = render(&mut house, beats).fixture_properties_before_strobe;
        for (fixture, state) in &warm {
            let other = &cool[fixture];
            assert_eq!(
                (state.intensity, state.pan, state.tilt, &state.attributes),
                (other.intensity, other.pan, other.tilt, &other.attributes),
                "{fixture} at beat {beats}"
            );
            recoloured += usize::from(state.colour != other.colour);
        }
    }
    assert!(recoloured > 0, "the palette must reach the rendered colour");
}

#[test]
fn the_bar_emits_the_bytes_the_eight_pixel_fixtures_did() {
    // The stage's bar at 57-80. The bytes were captured when it was eight 3-channel fixtures,
    // before it became one 24-channel fixture.
    const BAR: std::ops::Range<usize> = 56..80;
    let rain = [
        (
            0.5,
            [
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 52, 58, 69, 18, 20, 24,
            ],
        ),
        (
            1.25,
            [
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 52, 58, 69, 18, 20, 24, 0, 0, 0,
            ],
        ),
        (
            3.6,
            [
                52, 58, 69, 18, 20, 24, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            ],
        ),
    ];
    let kick = [(0.5, [118, 0, 3]), (1.25, [157, 129, 94])];
    let mut rain_engine = inline_engine(
        "rain",
        r#"
        return { render = function(ctx)
            local level = sf.loudness_gain(ctx.normalized_rms_level)
            return sf.scene { sf.colour(sf.group("strip"), sf.palette().cold),
                sf.each(sf.group("strip"), function(pixel, index, count)
                    local head = count - math.floor(ctx.beat_position % 16) % count
                    local lit = index == head and 0.35 or index == head % count + 1 and 0.12 or 0
                    return sf.dimmer(pixel, lit * level)
                end) }
        end }
    "#,
    );
    for (seconds, expected) in rain {
        let frame = rain_engine.render_at(seconds).unwrap();
        assert_eq!(frame.dmx_frames_by_universe[&0][BAR], expected, "{seconds}");
    }
    let mut kick_engine = inline_engine(
        "kick",
        r#"
        return { render = function(ctx)
            local palette = sf.palette()
            local accent = math.floor(ctx.beat_position % 8 / 2) % 2 == 0
            return { sf.colour(sf.group("strip"), accent and palette.accent or palette.primary),
                sf.dimmer(sf.group("strip"), 0.8 * sf.loudness_gain(ctx.normalized_rms_level)) }
        end }
    "#,
    );
    for (seconds, pixel) in kick {
        let frame = kick_engine.render_at(seconds).unwrap();
        assert_eq!(
            frame.dmx_frames_by_universe[&0][BAR],
            pixel.repeat(8),
            "{seconds}"
        );
    }
}

/// Pulses in a level sequence: a rise of at least PULSE_DEPTH, then a fall of as much.
fn pulses(levels: &[f64]) -> usize {
    let Some(&first) = levels.first() else {
        return 0;
    };
    let (mut low, mut high, mut rising, mut count) = (first, first, false, 0);
    for &level in levels {
        if rising {
            high = high.max(level);
            if high - level >= PULSE_DEPTH {
                count += 1;
                rising = false;
                low = level;
            }
        } else {
            low = low.min(level);
            if level - low >= PULSE_DEPTH {
                rising = true;
                high = level;
            }
        }
    }
    count
}

fn assert_pulses_at_most_half_beats(
    look: &Look,
    frames: &[&RenderedFrame],
    beats: f64,
    when: &str,
) {
    for id in loop_cells() {
        let levels: Vec<_> = frames
            .iter()
            .map(|frame| frame.fixture_properties_before_strobe[&id].intensity)
            .collect();
        let count = pulses(&levels);
        assert!(
            count as f64 <= MAX_PULSES_PER_BEAT * beats + 1.0,
            "{look}: {id} pulses {count} times in {beats} beats {when}"
        );
    }
}

#[test]
fn base_loops_pulse_no_faster_than_half_beat_steps() {
    const BEATS: f64 = 32.0;
    const STEPS_PER_BEAT: f64 = 16.0;
    for look in looks().into_iter().filter(Look::follows_music) {
        let start = if look.ramps() {
            RAMP_PLATEAU_BEATS - BEATS
        } else {
            0.0
        };
        let mut engine = look.engine(0.0);
        set_rms(&mut engine, 0.9);
        let frames: Vec<_> = (0..(BEATS * STEPS_PER_BEAT) as usize)
            .map(|step| render(&mut engine, start + step as f64 / STEPS_PER_BEAT))
            .collect();
        assert_pulses_at_most_half_beats(&look, &frames.iter().collect::<Vec<_>>(), BEATS, "");
        if look.ramps() {
            let finale = without_overlays(finale_for(&look), &["riser", "hit"]);
            let frames = render_replay(&finale, support::stage(), support::show_catalog()).unwrap();
            let last = frames_between(&frames, DIP_BEAT - BEATS, DIP_BEAT);
            assert_pulses_at_most_half_beats(&look, &last, BEATS, "into a cued drop");
        }
    }
}

#[test]
fn base_loops_peak_at_most_0_9() {
    for look in looks().into_iter().filter(Look::follows_music) {
        let mut engine = look.engine(0.0);
        set_rms(&mut engine, 1.0);
        let mut frames: Vec<_> = (0..64 * 10)
            .map(|step| render(&mut engine, step as f64 * 0.1))
            .collect();
        if look.ramps() {
            let finale = without_overlays(finale_for(&look), &["riser", "hit"]);
            frames
                .extend(render_replay(&finale, support::stage(), support::show_catalog()).unwrap());
        }
        for frame in frames
            .iter()
            .filter(|frame| frame.active_bank.as_deref() == Some(look.bank.as_str()))
        {
            for (id, state) in &frame.fixture_properties_before_strobe {
                assert!(
                    state.intensity <= BASE_PEAK + FLOAT_TOLERANCE,
                    "{look}: {id} at {} on beat {}",
                    state.intensity,
                    frame.beat_position
                );
            }
        }
    }
}

#[test]
fn buildup_regime_u_never_restarts() {
    // With no drop known the build only climbs: each 16-beat stride peaks at least as high as the
    // last, up to the plateau.
    let cells = loop_cells();
    for look in looks_in(&["buildup"]) {
        let frames = render_replay(
            &replay_for(&look),
            support::stage(),
            support::show_catalog(),
        )
        .unwrap();
        let mut previous = 0.0;
        for stride in 0..(RAMP_PLATEAU_BEATS / 16.0) as usize {
            let from = stride as f64 * 16.0;
            let peak = frames_between(&frames, from, from + 16.0)
                .iter()
                .map(|frame| total_light(frame, &cells))
                .fold(0.0, f64::max);
            assert!(
                peak >= previous - FLOAT_TOLERANCE,
                "{look}: beats {from}.. peak {peak} below the stride before, {previous}"
            );
            previous = peak;
        }
        let first = frames_between(&frames, 0.0, 16.0)
            .iter()
            .map(|frame| total_light(frame, &cells))
            .fold(0.0, f64::max);
        assert!(
            previous > first,
            "{look}: the build never climbed ({first} to {previous})"
        );
    }
}

#[test]
fn buildup_finale_rises_into_the_drop_and_the_last_beat_is_dark() {
    let cells = loop_cells();
    for look in looks_in(&["buildup"]) {
        let frames = render_replay(
            &finale_for(&look),
            support::stage(),
            support::show_catalog(),
        )
        .unwrap();
        for frame in &frames {
            assert!(
                frame.script_error.is_none(),
                "{look}: {:?}",
                frame.script_error
            );
        }
        let mean = |from: f64, to: f64| {
            let window = frames_between(&frames, from, to);
            window
                .iter()
                .map(|frame| total_light(frame, &cells))
                .sum::<f64>()
                / window.len() as f64
        };
        let (approach, finale) = (
            mean(RISER_BEAT - 16.0, RISER_BEAT),
            mean(RISER_BEAT + 1.0, DIP_BEAT),
        );
        assert!(
            finale > approach,
            "{look}: finale mean {finale} <= {approach} before it"
        );
        for frame in frames_between(&frames, DIP_BEAT + 0.05, DROP_BEAT - 0.05) {
            assert_eq!(
                total_light(frame, &cells),
                0.0,
                "{look}: lit in the dip at {}",
                frame.beat_position
            );
        }
        let entry = frames_between(&frames, DROP_BEAT, DROP_BEAT + 0.5);
        assert!(
            entry
                .iter()
                .all(|frame| frame.active_bank.as_deref() == Some("drop")),
            "{look}"
        );
        assert!(
            total_light(entry[0], &cells) > 0.0,
            "{look}: the drop's one is dark"
        );
    }
}

#[test]
fn countdown_shows_8_distinct_steps_over_the_last_8_beats() {
    let look = looks_in(&["buildup"])
        .into_iter()
        .find(|look| look.autoloop == "countdown")
        .unwrap();
    let frames = render_replay(
        &finale_for(&look),
        support::stage(),
        support::show_catalog(),
    )
    .unwrap();
    let strip = support::stage().groups()["strip"].clone();
    // Each step is the strip's light in units of one full cell; a half-lit edge pair is a step.
    let light: Vec<_> = (0..COUNT_STEPS)
        .map(|beat| {
            let at = DIP_BEAT - COUNT_STEPS as f64 + beat as f64 + 0.5;
            let frame = frames
                .iter()
                .find(|frame| frame.beat_position >= at)
                .unwrap();
            strip
                .iter()
                .map(|id| frame.fixture_properties_before_strobe[id].intensity)
                .collect::<Vec<_>>()
        })
        .collect();
    let full_cell = light[0].iter().copied().fold(0.0, f64::max);
    let steps: Vec<_> = light
        .iter()
        .map(|levels| (levels.iter().sum::<f64>() / full_cell * 100.0).round() / 100.0)
        .collect();
    assert!(
        steps.windows(2).all(|pair| pair[1] < pair[0]) && steps[COUNT_STEPS - 1] > 0.0,
        "countdown must step down once a beat to its last cell: {steps:?}"
    );
}

#[test]
fn beat_features_change_levels_not_colour_or_structure() {
    let features = |level: f64| BeatFeatures {
        onset: level,
        bass: level,
        mid: level,
        high: level,
    };
    for look in looks().into_iter().filter(Look::follows_music) {
        let (mut strong, mut weak) = (look.engine(0.0), look.engine(0.0));
        for (engine, level) in [(&mut strong, 1.0), (&mut weak, 0.1)] {
            engine
                .apply(
                    SECONDS_PER_BEAT,
                    &Command::Beat {
                        beat_index: 1,
                        bar_phase: Some(1),
                        beats_per_bar: 4,
                        bpm: REPLAY_BPM,
                        features: Some(features(level)),
                    },
                )
                .unwrap();
        }
        for step in 0..16 {
            let beat = 1.0 + step as f64 * 0.0625;
            let (a, b) = (render(&mut strong, beat), render(&mut weak, beat));
            for (id, state) in &a.fixture_properties_before_strobe {
                let other = &b.fixture_properties_before_strobe[id];
                assert_eq!(
                    (
                        state.colour,
                        state.pan,
                        state.tilt,
                        &state.attributes,
                        state.strobe_hz
                    ),
                    (
                        other.colour,
                        other.pan,
                        other.tilt,
                        &other.attributes,
                        other.strobe_hz
                    ),
                    "{look}: {id} follows the beat's features beyond its level at beat {beat}"
                );
            }
        }
    }
}

#[test]
fn overlays_declared_without_strobe_never_strobe_and_strobing_ones_do() {
    for look in overlays() {
        let frames = render_replay(
            &replay_for(&look),
            support::stage(),
            support::show_catalog(),
        )
        .unwrap();
        let strobing = frames.iter().any(|frame| {
            frame
                .fixture_properties_before_strobe
                .values()
                .any(|state| state.strobe_hz > 0.0)
        });
        assert_eq!(
            strobing,
            look.strobes(),
            "{look}: strobes {strobing} against its header"
        );
    }
}

#[test]
fn overlays_end_within_length_plus_release() {
    for look in overlays() {
        let replay = replay_for(&look);
        let end = replay
            .events
            .iter()
            .find_map(|event| match &event.command {
                Command::CueSection { value } => value
                    .overlays
                    .first()
                    .map(|row| row.begin_beats + row.length_beats + row.release_beats),
                _ => None,
            })
            .unwrap();
        let bank = [look.bank.as_str()];
        let with = render_replay(&replay, support::stage(), support::show_catalog()).unwrap();
        let without = render_replay(
            &without_overlays(replay, &bank),
            support::stage(),
            support::show_catalog(),
        )
        .unwrap();
        let touched = with
            .iter()
            .zip(&without)
            .any(|(a, b)| a.dmx_frames_by_universe != b.dmx_frames_by_universe);
        assert!(touched, "{look} never shows");
        for (a, b) in with.iter().zip(&without) {
            if a.beat_position >= end + OVERLAY_SETTLE_BEATS {
                assert_eq!(
                    a.dmx_frames_by_universe, b.dmx_frames_by_universe,
                    "{look} still shows at beat {}, after its end at {end}",
                    a.beat_position
                );
            }
        }
    }
}

/// Contexts covering both build regimes and an overlay's whole length, for instruction counts.
fn contexts(look: &Look) -> Vec<RenderContext> {
    let length = 16.0;
    let mut contexts = Vec::new();
    for step in 0..(DROP_BEAT as usize * 4) {
        let beat = step as f64 * 0.25;
        let base = RenderContext {
            time_seconds: beat * SECONDS_PER_BEAT,
            beat_position: beat,
            bar_phase: beat % 4.0,
            bar_index: (beat / 4.0) as u64,
            autoloop_elapsed_beats: if look.is_overlay() {
                beat % length
            } else {
                beat
            },
            normalized_rms_level: Some(0.6),
            bpm: REPLAY_BPM,
            section_intent: Some(SectionIntent::Buildup),
            section_elapsed_beats: Some(beat),
            onset: Some(0.8),
            bass: Some(0.8),
            mid: Some(0.5),
            high: Some(0.3),
            overlay_length_beats: look.is_overlay().then_some(length),
            ..RenderContext::default()
        };
        contexts.push(base);
        contexts.push(RenderContext {
            next_intent: Some(SectionIntent::Drop),
            beats_until_next: Some(DROP_BEAT - beat),
            beats_until_dip: Some((DIP_BEAT - beat).max(0.0)),
            next_known_at: Some(beat.min(DROP_BEAT - 36.0)),
            ..base
        });
    }
    contexts
}

#[test]
fn every_loop_stays_lean_inside_the_instruction_budget() {
    let (catalog, rig, palettes) = (
        support::show_catalog(),
        support::stage(),
        support::palettes(),
    );
    let slots = palettes.slots("techno").unwrap();
    let mut report = Vec::new();
    for look in every_look() {
        let mut script = catalog
            .instantiate(&look.bank, &look.autoloop, &rig, &palettes)
            .unwrap();
        let worst = contexts(&look)
            .iter()
            .map(|context| {
                script
                    .render(context, &slots)
                    .unwrap_or_else(|error| panic!("{look}: {error:#}"));
                script.instructions_used()
            })
            .max()
            .unwrap();
        let lean = if look.is_overlay() {
            LEAN_OVERLAY_INSTRUCTIONS
        } else {
            LEAN_BASE_INSTRUCTIONS
        };
        report.push(format!("{look:>24} {worst:>6}"));
        assert!(
            worst <= lean,
            "{look}: {worst} instructions in its worst frame, lean is {lean}"
        );
    }
    println!(
        "worst-frame Lua instructions per loop:\n{}",
        report.join("\n")
    );
}

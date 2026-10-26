use std::fmt;
use std::fs;
use std::path::PathBuf;

use starfire::engine::{Engine, RenderedFrame};
use starfire::fixtures::FixtureRig;
use starfire::model::{Command, FixtureState, Rgb, Transition};
use starfire::protocol::wire;
use starfire::replay::{Replay, dmx_digest, render_replay};
use starfire::script::AutoloopCatalog;

const FLOAT_TOLERANCE: f64 = 1e-9;
const ROOM_FIXTURE_COUNT: usize = 14;
const REPLAY_BPM: f64 = 120.0;
const SECONDS_PER_BEAT: f64 = 60.0 / REPLAY_BPM;
const MIN_REPLAY_SECONDS: f64 = 16.0 * 4.0 * SECONDS_PER_BEAT;
const RAMP_PLATEAU_BEATS: f64 = 128.0;
const WASH: [&str; 4] = ["wash_left", "wash_mid_left", "wash_mid_right", "wash_right"];
const WRITE_GOLDEN_VAR: &str = "STARFIRE_WRITE_GOLDEN";
const STANDBY_BANK: &str = "standby";
const WARM_WHITE: Rgb = Rgb {
    red: 1.0,
    green: 0.82,
    blue: 0.6,
};

struct Look {
    bank: String,
    autoloop: String,
    loop_beats: f64,
    source: String,
}

impl Look {
    fn ramps(&self) -> bool {
        self.bank == "buildup"
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

fn room() -> FixtureRig {
    FixtureRig::load(&root().join("../rigs/room.rig.pb")).unwrap()
}

fn catalog() -> AutoloopCatalog {
    AutoloopCatalog::load(&root().join("../autoloops")).unwrap()
}

fn looks() -> Vec<Look> {
    catalog()
        .list()
        .into_iter()
        .map(|entry| {
            let path = root().join(format!(
                "../autoloops/{}/{}.lua",
                entry.bank, entry.autoloop
            ));
            let source = fs::read_to_string(path).unwrap();
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
    ("family", &["cold", "warm", "accent"]),
    ("movement", &["still", "sweep", "snap"]),
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
    let looks: Vec<_> = looks()
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
    let mut replay = Replay::load(&default)
        .unwrap_or_else(|error| panic!("{look}: no replay at {own:?} or {default:?}: {error}"));
    for event in &mut replay.events {
        if let Command::Select { bank, autoloop, .. } = &mut event.command
            && *bank == look.bank
        {
            *autoloop = look.autoloop.clone();
        }
    }
    replay
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
    let mut engine = Engine::new(room(), catalog()).unwrap();
    let anchor_beat = select_beat.floor();
    engine
        .apply(
            0.0,
            &Command::Beat {
                beat_index: anchor_beat as u64,
                bar_phase: Some((anchor_beat as u32) % 4),
                beats_per_bar: 4,
                bpm: REPLAY_BPM,
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
    render(engine, beats_after_anchor).fixture_properties_before_strobe["wash_left"].intensity
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
    let first = render_replay(&replay, room(), catalog()).unwrap();
    for frame in &first.frames {
        assert!(
            frame.script_error.is_none(),
            "{look}: {:?}",
            frame.script_error
        );
        assert_eq!(frame.active_bank.as_deref(), Some(look.bank.as_str()));
        assert_eq!(
            frame.active_autoloop.as_deref(),
            Some(look.autoloop.as_str())
        );
        assert_eq!(
            frame.fixture_properties_before_strobe.len(),
            ROOM_FIXTURE_COUNT
        );
        for (id, state) in &frame.fixture_properties_before_strobe {
            assert!(state.strobe_hz == 0.0, "{look} strobes {id}");
        }
    }

    let second = render_replay(&replay, room(), catalog()).unwrap();
    let digest = dmx_digest(&first.frames);
    assert_eq!(
        digest,
        dmx_digest(&second.frames),
        "{look} is not deterministic"
    );

    // Duplicate selections cannot restart the current autoloop.
    let mut without_duplicate = replay.clone();
    without_duplicate.events.retain(|event| {
        event.at_seconds == 0.0 || !matches!(event.command, Command::Select { .. })
    });
    let continued = render_replay(&without_duplicate, room(), catalog()).unwrap();
    assert_eq!(
        digest,
        dmx_digest(&continued.frames),
        "{look} restarted on reselect"
    );

    // Frame rate changes sampling density, never the authored effect at a given time.
    let mut slower = replay.clone();
    slower.frame_rate_hz = replay.frame_rate_hz / 2;
    let slower = render_replay(&slower, room(), catalog()).unwrap();
    for (index, frame) in slower.frames.iter().enumerate() {
        assert_eq!(
            frame.dmx_frames_by_universe,
            first.frames[index * 2].dmx_frames_by_universe,
            "{look} depends on frame rate"
        );
    }

    check_golden(look, &digest);
}

#[test]
fn every_autoloop_replays_deterministically_and_matches_any_golden() {
    let looks = looks();
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
    for look in looks().iter().filter(|look| !look.ramps()) {
        let mut on_the_one = look.engine(0.0);
        let mut mid_bar = look.engine(2.25);
        for beat in [3.0, 3.1, 4.0, 6.5, 9.0, 13.7] {
            let expected = render(&mut on_the_one, beat);
            let actual = render(&mut mid_bar, beat - 2.0);
            assert_eq!(
                expected.dmx_frames_by_universe, actual.dmx_frames_by_universe,
                "{look} restarted on selection"
            );
        }
    }
}

#[test]
fn measured_energy_scales_levels_and_reach_but_not_colour_or_structure() {
    for look in looks().into_iter().filter(Look::follows_music) {
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
            assert!(
                (quiet.pan - 0.5).abs() <= (strong.pan - 0.5).abs() + FLOAT_TOLERANCE,
                "{look}: {id} reaches further when quiet"
            );
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
    for look in looks().into_iter().filter(Look::follows_music) {
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
                assert_eq!(state.colour, WARM_WHITE, "{look}: {id} is not warm white");
            }
        }
        for id in WASH {
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
        for id in WASH {
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
fn buildup_peaks_rise_across_the_ramp() {
    for look in looks_in(&["buildup"]) {
        let mut engine = look.engine(0.0);
        let mut bar_peak = |bar: f64| {
            (0..80)
                .map(|step| wash_level(&mut engine, bar * 4.0 + step as f64 * 0.05))
                .fold(0.0, f64::max)
        };
        let early = bar_peak(2.0);
        let late = bar_peak(30.0);
        assert!(
            late > early,
            "{look}: bar 30 peak {late} <= bar 2 peak {early}"
        );
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
fn drop_kick_washes_hit_on_every_beat() {
    let mut engine = engine("drop", "kick", 0.0);
    for beat in 0..16 {
        let hit = wash_level(&mut engine, beat as f64 + 0.1);
        let offbeat = wash_level(&mut engine, beat as f64 + 0.5);
        assert!(hit > offbeat, "beat {beat}: {hit} <= {offbeat}");
    }
}

#[test]
fn breakdown_wide_space_washes_drift_linearly_between_beats() {
    let mut engine = engine("breakdown", "wide_space", 0.0);
    let levels: Vec<_> = (0..=128)
        .map(|step| wash_level(&mut engine, step as f64 * 0.5))
        .collect();
    for beat in levels.windows(3).step_by(2) {
        assert!((beat[1] - (beat[0] + beat[2]) / 2.0).abs() <= FLOAT_TOLERANCE);
    }
}

fn run_inline(autoloop_id: &str, source: &str) -> RenderedFrame {
    let catalog = AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
        autoloop_id: autoloop_id.into(),
        bank: "test".into(),
        lua_source: source.into(),
    }])
    .unwrap();
    let mut engine = Engine::new(room(), catalog).unwrap();
    engine
        .apply(
            0.0,
            &Command::Select {
                bank: "test".into(),
                autoloop: autoloop_id.into(),
                transition: Transition::Cut,
            },
        )
        .unwrap();
    let frame = engine.render_at(0.0).unwrap();
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
        assert(sf.side(1) == -1 and sf.side(2) == 1)
        local mid = sf.mix(sf.rgb(0, 0, 1), sf.rgb(1, 0, 0), 0.25)
        assert(mid.red == 0.25 and mid.green == 0 and mid.blue == 0.75)
        return { render = function(ctx)
            return { sf.colour(sf.all(), sf.palette("house").cold), sf.dimmer(sf.all(), 1) }
        end }
    "#,
    );
    assert_eq!(
        frame.fixture_properties_before_strobe["cf805_pixel_1"].colour,
        Rgb {
            red: 0.0,
            green: 0.85,
            blue: 1.0
        }
    );
}

#[test]
fn the_same_autoloops_work_on_a_different_rig_through_named_groups() {
    let rig = FixtureRig::load(&root().join("../rigs/generic.rig.pb")).unwrap();
    for look in looks() {
        let mut replay = replay_for(&look);
        replay.duration_seconds = 8.0;
        replay
            .events
            .retain(|event| event.at_seconds <= replay.duration_seconds);
        let rendered = render_replay(&replay, rig.clone(), catalog()).unwrap();
        for frame in rendered.frames {
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
                    .all(|state| (0.0..=1.0).contains(&state.intensity) && state.strobe_hz == 0.0),
                "{look}"
            );
        }
    }
}

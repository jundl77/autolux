mod support;

use starfire::engine::Engine;
use starfire::fixtures::FixtureRig;
use starfire::model::{AttributeValue, FixtureState};
use starfire::protocol::{ClientMessage, apply_command, wire};
use starfire::script::AutoloopCatalog;
use starfire::special::capabilities;

const SECONDS_PER_BEAT: f64 = 0.5;
const DARK_LOOK: &str = "standby/dark";

const SPINNER: std::ops::Range<usize> = 0..3;
const SCANNER: std::ops::Range<usize> = 10..16;

fn synthetic_rig() -> wire::RigDefinition {
    let fixture = |id: &str, profile: &str, address| wire::Fixture {
        area: wire::StageArea::Centre as i32,
        fixture_uuid: id.into(),
        name: id.into(),
        profile_id: profile.into(),
        universe: 0,
        address,
        ..Default::default()
    };
    let group = |id: &str, members: &[&str]| wire::FixtureGroup {
        group_id: id.into(),
        fixture_uuids: members.iter().map(|member| (*member).into()).collect(),
        ..Default::default()
    };
    let profile = |id: &str, source: &str| support::compiled_profile(source, id);
    wire::RigDefinition {
        fixtures: vec![
            fixture("spinner", "wheel_spinner", SPINNER.start as u32 + 1),
            fixture("scanner", "beam_scanner", SCANNER.start as u32 + 1),
        ],
        groups: vec![
            group("spinners", &["spinner"]),
            group("scanners", &["scanner"]),
            group("fx", &["spinner", "scanner"]),
        ],
        profiles: vec![
            profile("wheel_spinner", "wheel_spinner#3ch"),
            profile("beam_scanner", "beam_scanner#6ch"),
        ],
    }
}

fn engine(rig: &FixtureRig) -> Engine {
    let catalog = AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
        autoloop_id: DARK_LOOK.into(),
        bank: "standby".into(),
        lua_source: "return { render = function() return sf.scene { sf.dimmer(sf.all(), 0) } end }"
            .into(),
        intensity_htp: false,
    }])
    .unwrap();
    let mut engine = Engine::new(rig.clone(), catalog, support::palettes()).unwrap();
    for command in [
        ClientMessage::UpdateBeat(wire::UpdateBeat {
            command_id: 1,
            tempo_bpm: 60.0 / SECONDS_PER_BEAT,
            beat_index: 1,
            bar_phase: Some(0),
            beats_per_bar: 4,
            features: None,
        }),
        ClientMessage::PlayAutoloop(wire::PlayAutoloop {
            command_id: 1,
            autoloop_id: DARK_LOOK.into(),
            transition: Some(wire::Transition::default()),
            palette: "techno".into(),
        }),
    ] {
        apply_command(&mut engine, 0.0, &command).unwrap();
    }
    engine
}

fn special(group: &str, value: wire::SpecialFx) -> ClientMessage {
    ClientMessage::SetActiveEffect(wire::SetActiveEffect {
        command_id: 1,
        effect_id: "special".into(),
        target: Some(wire::set_active_effect::Target::Group(group.into())),
        priority: 100,
        special_fx: Some(Box::new(value)),
        ..Default::default()
    })
}

fn steady(colour_slot: &str, energy: f32, speed: f32) -> wire::SpecialFx {
    wire::SpecialFx {
        colour_slot: colour_slot.into(),
        energy,
        speed,
        ..Default::default()
    }
}

fn released_over(release_beats: f64) -> wire::Envelope {
    wire::Envelope {
        attack_beats: 0.0,
        release_beats,
    }
}

fn fire(engine: &mut Engine, beat: f64, command: &ClientMessage) {
    apply_command(engine, beat * SECONDS_PER_BEAT, command).unwrap();
}

fn dmx(engine: &mut Engine, beat: f64) -> Vec<u8> {
    engine
        .render_at(beat * SECONDS_PER_BEAT)
        .unwrap()
        .dmx_frames_by_universe[&0]
        .clone()
}

fn reference(rig: &FixtureRig, fixture: &str, state: FixtureState) -> Vec<u8> {
    let mut states = rig.empty_states();
    states.insert(fixture.into(), state);
    rig.encode(&states).unwrap()[&0].clone()
}

fn primary() -> starfire::model::Rgb {
    support::palettes().slots("techno").unwrap().primary
}

fn unset(beat: f64, engine: &mut Engine) {
    unset_with(beat, engine, false);
}

fn unset_instantly(beat: f64, engine: &mut Engine) {
    unset_with(beat, engine, true);
}

fn unset_with(beat: f64, engine: &mut Engine, instantaneous_release: bool) {
    fire(
        engine,
        beat,
        &ClientMessage::UnsetActiveEffect(wire::UnsetActiveEffect {
            command_id: 1,
            effect_id: "special".into(),
            instantaneous_release,
        }),
    );
}

#[test]
fn capabilities_follow_each_profiles_attribute_families() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let report = |name: &str, colour, speed, pulse, pattern, cell_count| wire::GroupCapability {
        name: name.into(),
        cell_count,
        colour,
        speed,
        pulse,
        pattern,
        area: Some(wire::StageArea::Centre as i32),
        ..Default::default()
    };
    assert_eq!(
        capabilities(&rig),
        vec![
            report("fx", true, true, true, true, 2),
            report("scanners", true, true, true, true, 1),
            report("spinners", true, true, true, false, 1),
        ]
    );
}

#[test]
fn the_stage_reports_what_its_profiles_declare() {
    let rig = support::stage();
    let report: Vec<_> = capabilities(&rig)
        .into_iter()
        .map(|group| {
            (
                group.name,
                group.cell_count,
                [group.colour, group.speed, group.pulse, group.pattern],
            )
        })
        .collect();
    assert_eq!(
        report,
        vec![
            ("fx".to_owned(), 1, [true, true, true, false]),
            ("movers".to_owned(), 2, [true, true, true, false]),
            ("strip".to_owned(), 8, [true, false, false, false]),
            ("wash".to_owned(), 4, [true, false, false, false]),
        ]
    );
    let places: Vec<_> = capabilities(&rig)
        .into_iter()
        .map(|group| (group.area.map(|_| group.area()), group.centre_x_metres))
        .collect();
    use wire::StageArea as A;
    assert_eq!(
        places,
        [
            (Some(A::Centre), 0.0),
            (None, 0.0),
            (Some(A::UpCentre), 0.0),
            (None, 0.0),
        ]
    );
}

#[test]
fn colour_energy_and_speed_reach_wheel_shutter_and_rotation_attributes() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let mut engine = engine(&rig);
    fire(
        &mut engine,
        0.0,
        &special("spinners", steady("primary", 1.0, 0.5)),
    );
    let lit = reference(
        &rig,
        "spinner",
        FixtureState {
            intensity: 1.0,
            colour: primary(),
            ..FixtureState::default()
        },
    );
    let frame = dmx(&mut engine, 1.0);
    assert_eq!(frame[SPINNER.start], lit[SPINNER.start]);
    assert_ne!(frame[SPINNER.start], 0);
    assert_eq!(&frame[SPINNER.start + 1..SPINNER.end], &[255, 128]);

    fire(
        &mut engine,
        1.0,
        &special("spinners", steady("primary", 0.0, 1.0)),
    );
    assert_eq!(&dmx(&mut engine, 1.5)[SPINNER], &[0, 255, 255]);
}

#[test]
fn colour_energy_and_speed_reach_mixing_and_a_centred_pan_scan() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let mut engine = engine(&rig);
    fire(
        &mut engine,
        0.0,
        &special("scanners", steady("primary", 0.5, 1.0)),
    );
    let colour = primary();
    let scaled = |channel: f64| (channel * 0.5 * 255.0).round() as u8;
    let start = dmx(&mut engine, 0.0);
    assert_eq!(start[SCANNER.start], 128);
    assert_eq!(
        &start[SCANNER.start + 2..SCANNER.start + 5],
        &[
            scaled(colour.red),
            scaled(colour.green),
            scaled(colour.blue)
        ]
    );
    // Rendered frame by frame as a show is, so each head's pan memory follows the scan out to
    // the end stops rather than taking a whole-turn alias nearer its last sparse sample.
    let mut rendered = 0;
    let mut follow = |until_beat: f64| {
        let mut frame = Vec::new();
        while f64::from(rendered) < until_beat * 40.0 {
            rendered += 1;
            frame = dmx(&mut engine, f64::from(rendered) / 40.0);
        }
        frame
    };
    assert_eq!(follow(0.5)[SCANNER.start], 255);
    let end = follow(1.5);
    assert_eq!(end[SCANNER.start], 0);
    assert_eq!(end[SCANNER.end - 1], 0);
}

#[test]
fn pulses_step_the_pattern_wheel_or_gate_the_shutter_on_the_beat_grid() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let mut engine = engine(&rig);
    let pulsed = wire::SpecialFx {
        pulse_beats: Some(1),
        pattern: Some(0.5),
        ..steady("primary", 1.0, 0.0)
    };
    fire(&mut engine, 0.0, &special("fx", pulsed));
    let gobo = SCANNER.end - 1;
    let shutter = SPINNER.start + 1;
    for (beat, slot, shutter_byte) in [
        (0.25, 128, 255),
        (0.75, 128, 0),
        (1.25, 192, 255),
        (2.25, 0, 255),
        (3.75, 64, 0),
    ] {
        let frame = dmx(&mut engine, beat);
        assert_eq!(
            (frame[gobo], frame[shutter]),
            (slot, shutter_byte),
            "{beat}"
        );
    }
}

#[test]
fn attack_and_release_follow_the_musical_clock_without_snapping() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let mut engine = engine(&rig);
    let eased = wire::SpecialFx {
        envelope: Some(wire::Envelope {
            attack_beats: 4.0,
            release_beats: 4.0,
        }),
        ..steady("primary", 1.0, 0.0)
    };
    fire(&mut engine, 0.0, &special("scanners", eased));
    let red = SCANNER.start + 2;
    let level = |level: f64| (primary().red * level * 255.0).round() as u8;
    assert_eq!(dmx(&mut engine, 0.0)[red], 0);
    assert_eq!(dmx(&mut engine, 2.0)[red], level(0.5));
    assert_eq!(dmx(&mut engine, 4.0)[red], level(1.0));
    unset(6.0, &mut engine);
    assert_eq!(dmx(&mut engine, 6.0)[red], level(1.0));
    assert_eq!(dmx(&mut engine, 7.0)[red], level(0.75));
    assert_eq!(dmx(&mut engine, 9.0)[red], level(0.25));
    assert_eq!(dmx(&mut engine, 10.0)[red], 0);
}

#[test]
fn an_operator_cut_drops_the_special_this_frame_off_the_beat() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let red = SCANNER.start + 2;
    let mut engine = engine(&rig);
    let texture = wire::SpecialFx {
        envelope: Some(released_over(64.0)),
        ..steady("primary", 1.0, 0.0)
    };
    fire(&mut engine, 0.0, &special("scanners", texture));
    assert_ne!(dmx(&mut engine, 4.0)[red], 0);
    unset_instantly(4.3, &mut engine);
    assert_eq!(dmx(&mut engine, 4.3)[red], 0);
}

#[test]
fn a_cut_also_drops_a_special_already_fading_under_its_id() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let red = SCANNER.start + 2;
    let texture = wire::SpecialFx {
        envelope: Some(released_over(64.0)),
        ..steady("primary", 1.0, 0.0)
    };
    for replace in [false, true] {
        let mut engine = engine(&rig);
        fire(&mut engine, 0.0, &special("scanners", texture.clone()));
        if replace {
            fire(&mut engine, 4.0, &special("scanners", texture.clone()));
        } else {
            unset(4.0, &mut engine);
        }
        assert_ne!(dmx(&mut engine, 4.5)[red], 0, "replace {replace}");
        unset_instantly(4.5, &mut engine);
        assert_eq!(dmx(&mut engine, 4.5)[red], 0, "replace {replace}");
    }
}

fn paused(value: bool) -> ClientMessage {
    ClientMessage::SetParameter(wire::SetParameter {
        command_id: 1,
        parameter: Some(wire::Parameter {
            r#type: wire::ParameterType::Paused as i32,
            value: Some(wire::parameter::Value::Boolean(value)),
            fade_seconds: 0.0,
            group: String::new(),
        }),
    })
}

#[test]
fn a_replacement_crossfades_through_the_releasing_special() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let mut engine = engine(&rig);
    let first = wire::SpecialFx {
        envelope: Some(released_over(2.0)),
        ..steady("primary", 1.0, 0.0)
    };
    fire(&mut engine, 0.0, &special("scanners", first));
    let second = wire::SpecialFx {
        envelope: Some(wire::Envelope {
            attack_beats: 2.0,
            release_beats: 0.0,
        }),
        ..steady("primary", 0.0, 0.0)
    };
    fire(&mut engine, 1.0, &special("scanners", second));
    let red = SCANNER.start + 2;
    let full = (primary().red * 255.0).round() as u8;
    assert_eq!(dmx(&mut engine, 1.0)[red], full);
    assert_eq!(
        dmx(&mut engine, 2.0)[red],
        (primary().red * 0.25 * 255.0).round() as u8
    );
    assert_eq!(dmx(&mut engine, 3.0)[red], 0);
}

#[test]
fn an_absent_group_renders_nothing_and_low_intensity_gates_pulse_but_only_slows_speed() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let mut engine = engine(&rig);
    let dark = dmx(&mut engine, 0.0);
    fire(
        &mut engine,
        0.0,
        &special("absent", steady("primary", 1.0, 1.0)),
    );
    assert_eq!(dmx(&mut engine, 0.5), dark);

    fire(
        &mut engine,
        0.5,
        &ClientMessage::SetParameter(wire::SetParameter {
            command_id: 1,
            parameter: Some(wire::Parameter {
                r#type: wire::ParameterType::Intensity as i32,
                value: Some(wire::parameter::Value::Number(0.2)),
                fade_seconds: 0.0,
                ..Default::default()
            }),
        }),
    );
    let pulsed = wire::SpecialFx {
        pulse_beats: Some(1),
        ..steady("primary", 1.0, 1.0)
    };
    fire(&mut engine, 0.5, &special("spinners", pulsed));
    assert_eq!(
        &dmx(&mut engine, 0.75)[SPINNER.start + 1..SPINNER.end],
        &[255, 51]
    );
}

#[test]
fn two_keys_on_one_channel_resolve_to_the_higher_priority_layer() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let gobo = SCANNER.end - 1;
    let layer = |id: &str, key: &str, preset: &str, priority| {
        ClientMessage::SetActiveEffect(wire::SetActiveEffect {
            command_id: 1,
            effect_id: id.into(),
            target: Some(wire::set_active_effect::Target::Fixture("scanner".into())),
            priority,
            attributes: [(
                key.into(),
                wire::set_active_effect::AttributeValue {
                    value: Some(wire::set_active_effect::attribute_value::Value::Preset(
                        preset.into(),
                    )),
                },
            )]
            .into(),
            ..Default::default()
        })
    };
    for (low, high, expected) in [
        (("Gobo1", "Dots"), ("Base.Gobo1", "Tunnel"), 192),
        (("Base.Gobo1", "Tunnel"), ("Gobo1", "Dots"), 64),
    ] {
        let mut engine = engine(&rig);
        fire(&mut engine, 0.0, &layer("high", high.0, high.1, 20));
        fire(&mut engine, 0.0, &layer("low", low.0, low.1, 10));
        let frame = engine.render_at(0.0).unwrap();
        assert_eq!(frame.script_error, None);
        assert_eq!(frame.dmx_frames_by_universe[&0][gobo], expected);
    }
}

#[test]
fn invalid_specials_are_rejected() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let mut engine = engine(&rig);
    for invalid in [
        steady("primary", 1.5, 0.0),
        steady("primary", 1.0, f32::NAN),
        wire::SpecialFx {
            pulse_beats: Some(0),
            ..steady("primary", 1.0, 0.0)
        },
        wire::SpecialFx {
            envelope: Some(wire::Envelope {
                attack_beats: -1.0,
                release_beats: 0.0,
            }),
            ..steady("primary", 1.0, 0.0)
        },
        wire::SpecialFx {
            pattern: Some(2.0),
            ..steady("primary", 1.0, 0.0)
        },
    ] {
        assert!(apply_command(&mut engine, 0.0, &special("fx", invalid)).is_err());
    }
}

#[test]
fn the_stage_fx_group_spins_colours_and_gates_to_its_dark_slot() {
    let definition = support::stage_definition();
    let rig = support::stage();
    let member = &rig.groups()["fx"][0];
    let address = definition
        .fixtures
        .iter()
        .find(|fixture| &fixture.fixture_uuid == member)
        .unwrap()
        .address as usize
        - 1;
    let catalog = support::show_catalog();
    let mut engine = Engine::new(rig.clone(), catalog, support::palettes()).unwrap();
    for command in [
        ClientMessage::UpdateBeat(wire::UpdateBeat {
            command_id: 1,
            tempo_bpm: 60.0 / SECONDS_PER_BEAT,
            beat_index: 1,
            bar_phase: Some(0),
            beats_per_bar: 4,
            features: None,
        }),
        ClientMessage::PlayAutoloop(wire::PlayAutoloop {
            command_id: 1,
            autoloop_id: "standby/white".into(),
            transition: Some(wire::Transition::default()),
            palette: "techno".into(),
        }),
    ] {
        apply_command(&mut engine, 0.0, &command).unwrap();
    }
    let profile = rig.profile(member).unwrap();
    let fixture = address..address + profile.footprint();
    let rotation = profile
        .functions()
        .into_iter()
        .find(|function| function.attribute.ends_with("Rotate"))
        .unwrap();
    let spinning = |intensity| FixtureState {
        intensity,
        colour: primary(),
        attributes: [(
            rotation.key.clone(),
            AttributeValue::Number(rotation.physical_to),
        )]
        .into(),
        ..FixtureState::default()
    };
    let dark = reference(
        &rig,
        member,
        FixtureState {
            colour: primary(),
            ..FixtureState::default()
        },
    );
    assert_eq!(
        dmx(&mut engine, 0.0)[fixture.clone()],
        dark[fixture.clone()]
    );
    let pulsed = wire::SpecialFx {
        pulse_beats: Some(1),
        ..steady("primary", 1.0, 1.0)
    };
    fire(&mut engine, 0.0, &special("fx", pulsed));
    let open = dmx(&mut engine, 0.25);
    assert_eq!(
        open[fixture.clone()],
        reference(&rig, member, spinning(1.0))[fixture.clone()]
    );
    assert_ne!(open[fixture.clone()], dark[fixture.clone()]);
    assert_eq!(
        dmx(&mut engine, 0.75)[fixture.clone()],
        reference(&rig, member, spinning(0.0))[fixture]
    );
}

#[test]
fn pause_homes_the_dimmerless_fx_fixture_to_its_gdtf_defaults_and_play_resumes_its_render() {
    let definition = support::stage_definition();
    let rig = support::stage();
    let member = &rig.groups()["fx"][0];
    let address = definition
        .fixtures
        .iter()
        .find(|fixture| &fixture.fixture_uuid == member)
        .unwrap()
        .address as usize
        - 1;
    let fixture = address..address + rig.profile(member).unwrap().footprint();
    let mut engine = engine(&rig);
    fire(
        &mut engine,
        0.0,
        &special("fx", steady("primary", 1.0, 1.0)),
    );
    let spinning = dmx(&mut engine, 1.0)[fixture.clone()].to_vec();
    let mut idle = rig.empty_states();
    idle.get_mut(member.as_str()).unwrap().output_blackout = true;
    let defaults = rig.encode(&idle).unwrap()[&0][fixture.clone()].to_vec();
    assert_eq!(defaults, [0, 0, 0, 0]);
    assert_ne!(spinning, defaults);

    fire(&mut engine, 1.0, &paused(true));
    assert_eq!(dmx(&mut engine, 1.0)[fixture.clone()], defaults[..]);
    assert_eq!(dmx(&mut engine, 3.0)[fixture.clone()], defaults[..]);
    fire(&mut engine, 3.0, &paused(false));
    assert_eq!(dmx(&mut engine, 3.0)[fixture], spinning[..]);
}

fn parameter(kind: wire::ParameterType, value: f64) -> ClientMessage {
    ClientMessage::SetParameter(wire::SetParameter {
        command_id: 1,
        parameter: Some(wire::Parameter {
            r#type: kind as i32,
            value: Some(wire::parameter::Value::Number(value)),
            fade_seconds: 0.0,
            ..Default::default()
        }),
    })
}

#[test]
fn an_unknown_colour_slot_leaves_colour_to_the_look() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let mut engine = engine(&rig);
    fire(
        &mut engine,
        0.0,
        &special("scanners", steady("neon", 1.0, 0.0)),
    );
    assert_eq!(
        &dmx(&mut engine, 0.5)[SCANNER.start + 2..SCANNER.start + 5],
        &[255, 255, 255]
    );
}

#[test]
fn an_intensity_change_bends_the_scan_without_jumping_it() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let mut engine = engine(&rig);
    fire(
        &mut engine,
        0.0,
        &special("scanners", steady("primary", 1.0, 1.0)),
    );
    let pan = |engine: &mut Engine, beat: f64| {
        engine
            .render_at(beat * SECONDS_PER_BEAT)
            .unwrap()
            .fixture_properties_before_strobe["scanner"]
            .pan
    };
    assert!((pan(&mut engine, 0.25) - 0.75).abs() < 1e-9);
    fire(
        &mut engine,
        0.25,
        &parameter(wire::ParameterType::Intensity, 0.5),
    );
    assert!((pan(&mut engine, 1.0) - 0.875).abs() < 1e-9);
}

#[test]
fn pulses_lengthen_to_the_strobe_rate_cap_and_gates_stop_after_the_burst() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let mut engine = engine(&rig);
    fire(
        &mut engine,
        0.0,
        &parameter(wire::ParameterType::StrobeMaxRateHz, 1.0),
    );
    fire(
        &mut engine,
        0.0,
        &parameter(wire::ParameterType::StrobeMaxBurstSeconds, 1.0),
    );
    let pulsed = wire::SpecialFx {
        pulse_beats: Some(1),
        ..steady("primary", 1.0, 0.0)
    };
    fire(&mut engine, 0.0, &special("fx", pulsed));
    let shutter = SPINNER.start + 1;
    let gobo = SCANNER.end - 1;
    for (beat, shutter_byte, slot) in [
        (0.25, 255, 0),
        (0.75, 255, 0),
        (1.25, 0, 0),
        (1.75, 0, 0),
        (2.25, 255, 64),
        (3.25, 255, 64),
        (3.75, 255, 64),
        (4.25, 255, 128),
    ] {
        let frame = dmx(&mut engine, beat);
        assert_eq!(
            (frame[shutter], frame[gobo]),
            (shutter_byte, slot),
            "{beat}"
        );
    }
}

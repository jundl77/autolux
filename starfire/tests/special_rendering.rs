mod support;

use starfire::engine::Engine;
use starfire::fixtures::FixtureRig;
use starfire::model::{AttributeValue, FixtureState};
use starfire::protocol::{ClientMessage, apply_command, wire};
use starfire::script::AutoloopCatalog;
use starfire::special::capabilities;

const SECONDS_PER_BEAT: f64 = 0.5;
const DARK_LOOK: &str = "standby/dark";

const WHEEL_SPINNER: &str = r#"<GDTF DataVersion="1.2"><FixtureType Name="wheel_spinner" ShortName="Spinner" LongName="Spinner" Manufacturer="Autolux" Description="Colour wheel, shutter and rotation" FixtureTypeID="0b8f3c1e-5d2a-4c7e-9a61-2f4d8e7b1c30">
  <AttributeDefinitions><FeatureGroups><FeatureGroup Name="Color" Pretty="Color"><Feature Name="Color"/></FeatureGroup><FeatureGroup Name="Beam" Pretty="Beam"><Feature Name="Beam"/></FeatureGroup></FeatureGroups><Attributes>
    <Attribute Name="Color1" Pretty="C1" Feature="Color.Color" PhysicalUnit="None"/>
    <Attribute Name="Shutter1" Pretty="Sh1" Feature="Beam.Beam" PhysicalUnit="None"/>
    <Attribute Name="Effects1PosRotate" Pretty="Rotate" Feature="Beam.Beam" PhysicalUnit="AngularSpeed"/>
  </Attributes></AttributeDefinitions>
  <Wheels><Wheel Name="Colours">
    <Slot Name="Black" Color="0.3127,0.329,0"/>
    <Slot Name="Red" Color="0.64,0.33,21.26"/>
    <Slot Name="Green" Color="0.3,0.6,71.52"/>
    <Slot Name="Blue" Color="0.15,0.06,7.22"/>
  </Wheel></Wheels>
  <Geometries><Geometry Name="Base" Position="{1,0,0,0}{0,1,0,0}{0,0,1,0}{0,0,0,1}"/></Geometries>
  <DMXModes><DMXMode Name="3ch" Geometry="Base"><DMXChannels>
    <DMXChannel DMXBreak="1" Offset="1" Geometry="Base" InitialFunction="Base_Color1.Color1.Color1" Highlight="None"><LogicalChannel Attribute="Color1" Snap="Yes">
      <ChannelFunction Name="Color1" Attribute="Color1" DMXFrom="0/1" Default="0/1" PhysicalFrom="1" PhysicalTo="4" Wheel="Colours">
        <ChannelSet Name="Black" DMXFrom="0/1" PhysicalFrom="1" PhysicalTo="1" WheelSlotIndex="1"/>
        <ChannelSet Name="Red" DMXFrom="64/1" PhysicalFrom="2" PhysicalTo="2" WheelSlotIndex="2"/>
        <ChannelSet Name="Green" DMXFrom="128/1" PhysicalFrom="3" PhysicalTo="3" WheelSlotIndex="3"/>
        <ChannelSet Name="Blue" DMXFrom="192/1" PhysicalFrom="4" PhysicalTo="4" WheelSlotIndex="4"/>
      </ChannelFunction></LogicalChannel></DMXChannel>
    <DMXChannel DMXBreak="1" Offset="2" Geometry="Base" InitialFunction="Base_Shutter1.Shutter1.Shutter1" Highlight="None"><LogicalChannel Attribute="Shutter1" Snap="Yes">
      <ChannelFunction Name="Shutter1" Attribute="Shutter1" DMXFrom="0/1" Default="255/1" PhysicalFrom="0" PhysicalTo="1"/></LogicalChannel></DMXChannel>
    <DMXChannel DMXBreak="1" Offset="3" Geometry="Base" InitialFunction="Base_Effects1PosRotate.Effects1PosRotate.Effects1PosRotate" Highlight="None"><LogicalChannel Attribute="Effects1PosRotate" Snap="No">
      <ChannelFunction Name="Effects1PosRotate" Attribute="Effects1PosRotate" DMXFrom="0/1" Default="0/1" PhysicalFrom="0" PhysicalTo="180"/></LogicalChannel></DMXChannel>
  </DMXChannels></DMXMode></DMXModes>
</FixtureType></GDTF>"#;

const BEAM_SCANNER: &str = r#"<GDTF DataVersion="1.2"><FixtureType Name="beam_scanner" ShortName="Scanner" LongName="Scanner" Manufacturer="Autolux" Description="Pan, tilt, RGB mixing and a pattern wheel" FixtureTypeID="5c2e9d47-1b3a-4f68-8e05-7a9c3d2b6f14">
  <AttributeDefinitions><FeatureGroups><FeatureGroup Name="Position" Pretty="Position"><Feature Name="PanTilt"/></FeatureGroup><FeatureGroup Name="Color" Pretty="Color"><Feature Name="RGB"/></FeatureGroup><FeatureGroup Name="Gobo" Pretty="Gobo"><Feature Name="Gobo"/></FeatureGroup></FeatureGroups><Attributes>
    <Attribute Name="Pan" Pretty="P" Feature="Position.PanTilt" PhysicalUnit="Angle"/>
    <Attribute Name="Tilt" Pretty="T" Feature="Position.PanTilt" PhysicalUnit="Angle"/>
    <Attribute Name="ColorAdd_R" Pretty="R" Feature="Color.RGB" PhysicalUnit="ColorComponent"/>
    <Attribute Name="ColorAdd_G" Pretty="G" Feature="Color.RGB" PhysicalUnit="ColorComponent"/>
    <Attribute Name="ColorAdd_B" Pretty="B" Feature="Color.RGB" PhysicalUnit="ColorComponent"/>
    <Attribute Name="Gobo1" Pretty="G1" Feature="Gobo.Gobo" PhysicalUnit="None"/>
  </Attributes></AttributeDefinitions>
  <Geometries><Geometry Name="Base" Position="{1,0,0,0}{0,1,0,0}{0,0,1,0}{0,0,0,1}"/></Geometries>
  <DMXModes><DMXMode Name="6ch" Geometry="Base"><DMXChannels>
    <DMXChannel DMXBreak="1" Offset="1" Geometry="Base" InitialFunction="Base_Pan.Pan.Pan" Highlight="None"><LogicalChannel Attribute="Pan">
      <ChannelFunction Name="Pan" Attribute="Pan" DMXFrom="0/1" Default="128/1" PhysicalFrom="0" PhysicalTo="540"/></LogicalChannel></DMXChannel>
    <DMXChannel DMXBreak="1" Offset="2" Geometry="Base" InitialFunction="Base_Tilt.Tilt.Tilt" Highlight="None"><LogicalChannel Attribute="Tilt">
      <ChannelFunction Name="Tilt" Attribute="Tilt" DMXFrom="0/1" Default="128/1" PhysicalFrom="0" PhysicalTo="270"/></LogicalChannel></DMXChannel>
    <DMXChannel DMXBreak="1" Offset="3" Geometry="Base" InitialFunction="Base_ColorAdd_R.ColorAdd_R.ColorAdd_R" Highlight="None"><LogicalChannel Attribute="ColorAdd_R">
      <ChannelFunction Name="ColorAdd_R" Attribute="ColorAdd_R" DMXFrom="0/1" Default="0/1" PhysicalFrom="0" PhysicalTo="100"/></LogicalChannel></DMXChannel>
    <DMXChannel DMXBreak="1" Offset="4" Geometry="Base" InitialFunction="Base_ColorAdd_G.ColorAdd_G.ColorAdd_G" Highlight="None"><LogicalChannel Attribute="ColorAdd_G">
      <ChannelFunction Name="ColorAdd_G" Attribute="ColorAdd_G" DMXFrom="0/1" Default="0/1" PhysicalFrom="0" PhysicalTo="100"/></LogicalChannel></DMXChannel>
    <DMXChannel DMXBreak="1" Offset="5" Geometry="Base" InitialFunction="Base_ColorAdd_B.ColorAdd_B.ColorAdd_B" Highlight="None"><LogicalChannel Attribute="ColorAdd_B">
      <ChannelFunction Name="ColorAdd_B" Attribute="ColorAdd_B" DMXFrom="0/1" Default="0/1" PhysicalFrom="0" PhysicalTo="100"/></LogicalChannel></DMXChannel>
    <DMXChannel DMXBreak="1" Offset="6" Geometry="Base" InitialFunction="Base_Gobo1.Gobo1.Gobo1" Highlight="None"><LogicalChannel Attribute="Gobo1" Snap="Yes">
      <ChannelFunction Name="Gobo1" Attribute="Gobo1" DMXFrom="0/1" Default="0/1" PhysicalFrom="0" PhysicalTo="3">
        <ChannelSet Name="Open" DMXFrom="0/1" PhysicalFrom="0" PhysicalTo="0"/>
        <ChannelSet Name="Dots" DMXFrom="64/1" PhysicalFrom="1" PhysicalTo="1"/>
        <ChannelSet Name="Lines" DMXFrom="128/1" PhysicalFrom="2" PhysicalTo="2"/>
        <ChannelSet Name="Tunnel" DMXFrom="192/1" PhysicalFrom="3" PhysicalTo="3"/>
      </ChannelFunction></LogicalChannel></DMXChannel>
  </DMXChannels></DMXMode></DMXModes>
</FixtureType></GDTF>"#;

const SPINNER: std::ops::Range<usize> = 0..3;
const SCANNER: std::ops::Range<usize> = 10..16;

fn synthetic_rig() -> wire::RigDefinition {
    let fixture = |id: &str, profile: &str, address| wire::Fixture {
        fixture_id: id.into(),
        profile_id: profile.into(),
        universe: 0,
        address,
        ..Default::default()
    };
    let group = |id: &str, members: &[&str]| wire::FixtureGroup {
        group_id: id.into(),
        fixture_ids: members.iter().map(|member| (*member).into()).collect(),
    };
    let profile = |id: &str, gdtf_xml: &str, dmx_mode: &str| wire::FixtureProfile {
        profile_id: id.into(),
        gdtf_xml: gdtf_xml.into(),
        dmx_mode: dmx_mode.into(),
    };
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
            profile("wheel_spinner", WHEEL_SPINNER, "3ch"),
            profile("beam_scanner", BEAM_SCANNER, "6ch"),
        ],
    }
}

fn engine(rig: &FixtureRig) -> Engine {
    let catalog = AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
        autoloop_id: DARK_LOOK.into(),
        bank: "standby".into(),
        lua_source: "return { render = function() return sf.scene { sf.dimmer(sf.all(), 0) } end }"
            .into(),
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
        }),
        ClientMessage::PlayAutoloop(wire::PlayAutoloop {
            command_id: 1,
            autoloop_id: DARK_LOOK.into(),
            transition: Some(wire::play_autoloop::Transition::default()),
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
        special_fx: Some(value),
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
    fire(
        engine,
        beat,
        &ClientMessage::UnsetActiveEffect(wire::UnsetActiveEffect {
            command_id: 1,
            effect_id: "special".into(),
        }),
    );
}

#[test]
fn capabilities_follow_each_profiles_attribute_families() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let report = |name: &str, colour, speed, pulse, pattern, fixture_count| wire::GroupCapability {
        name: name.into(),
        fixture_count,
        colour,
        speed,
        pulse,
        pattern,
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
fn the_real_rig_reports_what_its_profiles_declare() {
    let rig = support::room();
    let report: Vec<_> = capabilities(&rig)
        .into_iter()
        .map(|group| {
            (
                group.name,
                group.fixture_count,
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
    assert_eq!(dmx(&mut engine, 0.5)[SCANNER.start], 255);
    assert_eq!(dmx(&mut engine, 1.5)[SCANNER.start], 0);
    assert_eq!(dmx(&mut engine, 1.5)[SCANNER.end - 1], 0);
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
        attack_beats: 4.0,
        release_beats: 4.0,
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
fn a_replacement_crossfades_through_the_releasing_special() {
    let rig = FixtureRig::from_wire(&synthetic_rig()).unwrap();
    let mut engine = engine(&rig);
    let first = wire::SpecialFx {
        release_beats: 2.0,
        ..steady("primary", 1.0, 0.0)
    };
    fire(&mut engine, 0.0, &special("scanners", first));
    let second = wire::SpecialFx {
        attack_beats: 2.0,
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
            attack_beats: -1.0,
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
fn the_real_rig_fx_group_spins_colours_and_gates_to_its_dark_slot() {
    let definition = support::room_definition();
    let rig = support::room();
    let member = &rig.groups()["fx"][0];
    let address = definition
        .fixtures
        .iter()
        .find(|fixture| &fixture.fixture_id == member)
        .unwrap()
        .address as usize
        - 1;
    let catalog = AutoloopCatalog::load(
        &std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../autoloops"),
    )
    .unwrap();
    let mut engine = Engine::new(rig.clone(), catalog, support::palettes()).unwrap();
    for command in [
        ClientMessage::UpdateBeat(wire::UpdateBeat {
            command_id: 1,
            tempo_bpm: 60.0 / SECONDS_PER_BEAT,
            beat_index: 1,
            bar_phase: Some(0),
            beats_per_bar: 4,
        }),
        ClientMessage::PlayAutoloop(wire::PlayAutoloop {
            command_id: 1,
            autoloop_id: "standby/warm_white".into(),
            transition: Some(wire::play_autoloop::Transition::default()),
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

fn parameter(kind: wire::ParameterType, value: f64) -> ClientMessage {
    ClientMessage::SetParameter(wire::SetParameter {
        command_id: 1,
        parameter: Some(wire::Parameter {
            r#type: kind as i32,
            value: Some(wire::parameter::Value::Number(value)),
            fade_seconds: 0.0,
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

mod support;

use starfire::engine::Engine;
use starfire::fixtures::FixtureRig;
use starfire::gdtf::GdtfProfile;
use starfire::model::{AttributeValue, Command, FixtureState, Rgb, Transition};
use starfire::protocol::wire;
use starfire::script::AutoloopCatalog;

fn description(channels: &str) -> String {
    format!(
        r#"<GDTF DataVersion="1.2"><FixtureType ShortName="Test" LongName="Test" Description="Test fixture" Name="Test" Manufacturer="Autolux" FixtureTypeID="85176e68-73d4-4217-9bfd-7791b08b01ef">
      <AttributeDefinitions><FeatureGroups><FeatureGroup Name="Control" Pretty="Control"><Feature Name="Control"/></FeatureGroup></FeatureGroups><Attributes>
      <Attribute Name="Fog1" Pretty="Test" Feature="Control.Control" PhysicalUnit="None"/><Attribute Name="Gobo1" Pretty="Test" Feature="Control.Control" PhysicalUnit="None"/>
      <Attribute Name="Zoom" Pretty="Test" Feature="Control.Control" PhysicalUnit="None"/><Attribute Name="Mode" Pretty="Test" Feature="Control.Control" PhysicalUnit="None"/>
      <Attribute Name="Dimmer" Pretty="Test" Feature="Control.Control" PhysicalUnit="None"/><Attribute Name="Pan" Pretty="Test" Feature="Control.Control" PhysicalUnit="None"/>
      <Attribute Name="ColorAdd_R" Pretty="Red" Feature="Control.Control" PhysicalUnit="Percent"/>
      <Attribute Name="ColorAdd_G" Pretty="Green" Feature="Control.Control" PhysicalUnit="Percent"/>
      <Attribute Name="ColorAdd_B" Pretty="Blue" Feature="Control.Control" PhysicalUnit="Percent"/>
      <Attribute Name="ColorAdd_W" Pretty="White" Feature="Control.Control" PhysicalUnit="Percent"/>
      <Attribute Name="ColorAdd_A" Pretty="Amber" Feature="Control.Control" PhysicalUnit="Percent"/>
      <Attribute Name="ColorSub_UV" Pretty="UV filter" Feature="Control.Control" PhysicalUnit="Percent"/>
      </Attributes></AttributeDefinitions>
      <Geometries><Geometry Position="{{1,0,0,0}}{{0,1,0,0}}{{0,0,1,0}}{{0,0,0,1}}" Name="Base"><Geometry Position="{{1,0,0,0}}{{0,1,0,0}}{{0,0,1,0}}{{0,0,0,1}}" Name="Head1"/><Geometry Position="{{1,0,0,0}}{{0,1,0,0}}{{0,0,1,0}}{{0,0,0,1}}" Name="Head2"/></Geometry></Geometries>
      <DMXModes><DMXMode Name="Control" Geometry="Base"><DMXChannels>{channels}</DMXChannels></DMXMode></DMXModes>
    </FixtureType></GDTF>"#
    )
}

fn channel(attribute: &str, geometry: &str, offsets: &str, functions: &str) -> String {
    format!(
        r#"<DMXChannel Offset="{offsets}" Highlight="None" Geometry="{geometry}"><LogicalChannel Attribute="{attribute}">{functions}</LogicalChannel></DMXChannel>"#
    )
}

fn function(attribute: &str, extra: &str) -> String {
    format!(
        r#"<ChannelFunction Name="Control" Attribute="{attribute}" PhysicalFrom="0" PhysicalTo="100" {extra}/>"#
    )
}

fn profile(channels: &str) -> GdtfProfile {
    GdtfProfile::from_xml(&description(channels), "Control").unwrap()
}

fn values(items: &[(&str, AttributeValue)]) -> FixtureState {
    FixtureState {
        attributes: items
            .iter()
            .map(|(key, value)| ((*key).into(), value.clone()))
            .collect(),
        ..FixtureState::default()
    }
}

fn encode(profile: &GdtfProfile, state: &FixtureState) -> Vec<u8> {
    let mut output = vec![0; profile.footprint()];
    profile.encode(state, &mut output).unwrap();
    output
}

#[test]
fn physical_values_preserve_all_channel_resolutions_and_arbitrary_offsets() {
    for (offsets, expected) in [
        ("1", vec![128]),
        ("2,1", vec![0, 128]),
        ("3,1,2", vec![0, 0, 128]),
        ("4,1,3,2", vec![0, 0, 0, 128]),
    ] {
        let profile = profile(&channel("Fog1", "Base", offsets, &function("Fog1", "")));
        assert_eq!(
            encode(&profile, &values(&[("Fog1", AttributeValue::Number(50.0))])),
            expected
        );
        assert_eq!(
            encode(
                &profile,
                &values(&[("Fog1", AttributeValue::Number(100.0))])
            ),
            vec![255; expected.len()]
        );
    }
}

#[test]
fn defaults_use_standard_byte_mirroring_and_byte_shifting() {
    for (default, expected) in [("18/1", vec![18, 18]), ("18/1s", vec![18, 0])] {
        let profile = profile(&channel(
            "Fog1",
            "Base",
            "1,2",
            &function("Fog1", &format!(r#"Default="{default}""#)),
        ));
        assert_eq!(encode(&profile, &FixtureState::default()), expected);
    }
}

#[test]
fn optional_highlight_defaults_and_oversized_dmx_literals_are_handled() {
    let channels = channel("Fog1", "Base", "1", &function("Fog1", ""));
    let omitted = channels.replace(" Highlight=\"None\"", "");
    assert_eq!(
        encode(
            &profile(&omitted),
            &values(&[("Fog1", AttributeValue::Number(50.0))])
        ),
        [128]
    );
    for invalid in ["256/1", "65536/2", "0/8"] {
        let channels = channel(
            "Fog1",
            "Base",
            "1",
            &function("Fog1", &format!(r#"Default="{invalid}""#)),
        );
        assert!(
            GdtfProfile::from_xml(&description(&channels), "Control").is_err(),
            "{invalid}"
        );
    }
    let nested = format!("{}{}", "<x>".repeat(129), "</x>".repeat(129));
    assert!(
        GdtfProfile::from_xml(&nested, "Control")
            .unwrap_err()
            .to_string()
            .contains("nesting")
    );
}

#[test]
fn physical_pan_values_obey_patch_calibration() {
    let xml = description(&channel("Pan", "Base", "1,2", &function("Pan", "")));
    let rig = FixtureRig::from_wire(&wire::RigDefinition {
        fixtures: vec![wire::Fixture {
            fixture_id: "mover".into(),
            profile_id: "test".into(),
            address: 1,
            pan: Some(wire::fixture::MovementRange {
                minimum: Some(0.25),
                maximum: Some(0.75),
                inverted: true,
            }),
            ..Default::default()
        }],
        profiles: vec![wire::FixtureProfile {
            profile_id: "test".into(),
            gdtf_xml: xml,
            dmx_mode: "Control".into(),
        }],
        ..Default::default()
    })
    .unwrap();
    let mut states = rig.empty_states();
    states.insert(
        "mover".into(),
        values(&[("Pan", AttributeValue::Number(0.0))]),
    );
    assert_eq!(&rig.encode(&states).unwrap()[&0][..2], [191, 255]);
    states.insert(
        "mover".into(),
        values(&[("Pan", AttributeValue::Number(100.0))]),
    );
    assert_eq!(&rig.encode(&states).unwrap()[&0][..2], [64, 0]);
}

#[test]
fn geometry_qualification_keeps_cells_independent_and_rejects_ambiguous_names() {
    let channels = channel("Fog1", "Head1", "1", &function("Fog1", ""))
        + &channel("Fog1", "Head2", "3", &function("Fog1", ""));
    let profile = profile(&channels);
    let state = values(&[
        ("Head1.Fog1", AttributeValue::Number(100.0)),
        ("Head2_Fog1.Fog1.Control", AttributeValue::Number(50.0)),
    ]);
    assert_eq!(encode(&profile, &state), [255, 0, 128]);
    assert!(
        profile
            .validate_attributes(&values(&[("Fog1", AttributeValue::Number(50.0))]).attributes)
            .is_err()
    );
}

#[test]
fn physical_ranges_can_be_inverted_and_must_not_be_exceeded() {
    let profile = profile(&channel(
        "Zoom",
        "Base",
        "1,2",
        r#"<ChannelFunction Name="Zoom" Attribute="Zoom" PhysicalFrom="60" PhysicalTo="10"/>"#,
    ));
    assert_eq!(
        encode(&profile, &values(&[("Zoom", AttributeValue::Number(60.0))])),
        [0, 0]
    );
    assert_eq!(
        encode(&profile, &values(&[("Zoom", AttributeValue::Number(10.0))])),
        [255, 255]
    );
    for invalid in [f64::NAN, 9.0, 61.0] {
        assert!(
            profile
                .validate_attributes(
                    &values(&[("Zoom", AttributeValue::Number(invalid))]).attributes
                )
                .is_err()
        );
    }
}

#[test]
fn channel_sets_are_explicit_presets_and_do_not_crossfade_through_other_gobos() {
    let profile = profile(&channel(
        "Gobo1",
        "Base",
        "1",
        r#"<ChannelFunction Name="Gobo" Attribute="Gobo1" PhysicalFrom="0" PhysicalTo="1"><ChannelSet Name="Open" DMXFrom="0/1"/><ChannelSet Name="Stars" DMXFrom="128/1"/></ChannelFunction>"#,
    ));
    let open = AttributeValue::Preset("Open".into());
    let stars = AttributeValue::Preset("Stars".into());
    assert_eq!(
        encode(&profile, &values(&[("Gobo1", stars.clone())])),
        [128]
    );
    assert_eq!(
        profile.interpolate_attribute("Gobo1", &open, &stars, 0.75),
        open
    );
    assert_eq!(
        profile.interpolate_attribute("Gobo1", &open, &stars, 1.0),
        stars
    );
    assert!(
        profile
            .validate_attributes(&values(&[("Gobo1", AttributeValue::Number(0.5))]).attributes)
            .is_err()
    );
}

#[test]
fn mode_master_is_resolved_before_dependent_channels_and_inactive_values_fail() {
    let dependent = channel(
        "Fog1",
        "Base",
        "1",
        &function(
            "Fog1",
            r#"ModeMaster="Base_Mode" ModeFrom="128/1" ModeTo="255/1""#,
        ),
    );
    let master = channel("Mode", "Base", "2", &function("Mode", ""));
    let profile = profile(&(dependent + &master));
    let active = values(&[
        ("Mode", AttributeValue::Number(100.0)),
        ("Fog1", AttributeValue::Number(50.0)),
    ]);
    assert_eq!(encode(&profile, &active), [128, 255]);
    assert!(
        profile
            .encode(
                &values(&[("Fog1", AttributeValue::Number(50.0))]),
                &mut [0; 2]
            )
            .is_err()
    );
}

#[test]
fn function_ranges_follow_numeric_order_at_every_resolution_without_changing_initial_function() {
    for bytes in 1..=4 {
        let split = 1_u64 << (8 * bytes - 1);
        let offsets = (1..=bytes)
            .map(|value| value.to_string())
            .collect::<Vec<_>>()
            .join(",");
        let functions = format!(
            r#"
          <ChannelFunction Name="High" Attribute="Zoom" DMXFrom="{split}/{bytes}" Default="{split}/{bytes}" PhysicalFrom="0" PhysicalTo="100"/>
          <ChannelFunction Name="Low" Attribute="Fog1" DMXFrom="0/1" PhysicalFrom="0" PhysicalTo="100"/>"#
        );
        let profile = profile(&channel("Fog1", "Base", &offsets, &functions));
        let mut initial = vec![0; bytes];
        initial[0] = 128;
        assert_eq!(encode(&profile, &FixtureState::default()), initial);
        let mut expected = vec![255; bytes];
        expected[0] = 127;
        assert_eq!(
            encode(
                &profile,
                &values(&[("Fog1", AttributeValue::Number(100.0))])
            ),
            expected
        );
        assert_eq!(
            encode(
                &profile,
                &values(&[("Zoom", AttributeValue::Number(100.0))])
            ),
            vec![255; bytes]
        );
    }
}

#[test]
fn function_bounds_cross_logical_channels_on_the_same_physical_channel() {
    let channels = r#"<DMXChannel Offset="1" Geometry="Base">
      <LogicalChannel Attribute="Zoom"><ChannelFunction Name="High" Attribute="Zoom" DMXFrom="128/1" PhysicalFrom="0" PhysicalTo="100"/></LogicalChannel>
      <LogicalChannel Attribute="Fog1"><ChannelFunction Name="Low" Attribute="Fog1" DMXFrom="0/1" PhysicalFrom="0" PhysicalTo="100"/></LogicalChannel>
    </DMXChannel>"#;
    let profile = profile(channels);
    assert_eq!(
        encode(
            &profile,
            &values(&[("Fog1", AttributeValue::Number(100.0))])
        ),
        [127]
    );
    assert_eq!(
        encode(&profile, &values(&[("Zoom", AttributeValue::Number(0.0))])),
        [128]
    );
}

#[test]
fn repeated_function_starts_are_valid_in_mutually_exclusive_modes() {
    let dependent = channel(
        "Fog1",
        "Base",
        "1",
        r#"
      <ChannelFunction Name="LowMode" Attribute="Fog1" DMXFrom="0/1" ModeMaster="Base_Mode" ModeFrom="0/1" ModeTo="127/1" PhysicalFrom="0" PhysicalTo="100"/>
      <ChannelFunction Name="HighMode" Attribute="Fog1" DMXFrom="0/1" ModeMaster="Base_Mode" ModeFrom="128/1" ModeTo="255/1" PhysicalFrom="0" PhysicalTo="100"/>
      <ChannelFunction Name="Zoom" Attribute="Zoom" DMXFrom="128/1" ModeMaster="Base_Mode" ModeFrom="128/1" ModeTo="255/1" PhysicalFrom="0" PhysicalTo="100"/>"#,
    );
    let profile = profile(&(dependent + &channel("Mode", "Base", "2", &function("Mode", ""))));
    for (mode, expected) in [(0.0, [128, 0]), (100.0, [64, 255])] {
        assert_eq!(
            encode(
                &profile,
                &values(&[
                    ("Fog1", AttributeValue::Number(50.0)),
                    ("Mode", AttributeValue::Number(mode))
                ])
            ),
            expected
        );
    }
}

#[test]
fn unconditional_function_range_expands_when_the_next_function_is_inactive() {
    let dependent = channel(
        "Fog1",
        "Base",
        "1",
        r#"
      <ChannelFunction Name="Fog" Attribute="Fog1" DMXFrom="0/1" PhysicalFrom="0" PhysicalTo="100"/>
      <ChannelFunction Name="Zoom" Attribute="Zoom" DMXFrom="128/1" ModeMaster="Base_Mode" ModeFrom="128/1" ModeTo="255/1" PhysicalFrom="0" PhysicalTo="100"/>"#,
    );
    let profile = profile(&(dependent + &channel("Mode", "Base", "2", &function("Mode", ""))));
    for (mode, expected) in [(0.0, [128, 0]), (100.0, [64, 255])] {
        assert_eq!(
            encode(
                &profile,
                &values(&[
                    ("Fog1", AttributeValue::Number(50.0)),
                    ("Mode", AttributeValue::Number(mode))
                ])
            ),
            expected
        );
    }
}

#[test]
fn a_function_mode_master_requires_its_own_dmx_range_to_be_active() {
    let dependent = channel(
        "Fog1",
        "Base",
        "1",
        &function(
            "Fog1",
            r#"ModeMaster="Base_Mode.Mode.Enabled" ModeFrom="0/1" ModeTo="255/1""#,
        ),
    );
    let master = channel(
        "Mode",
        "Base",
        "2",
        r#"
      <ChannelFunction Name="Idle" Attribute="Mode" DMXFrom="0/1" PhysicalFrom="0" PhysicalTo="100"/>
      <ChannelFunction Name="Enabled" Attribute="Mode" DMXFrom="128/1" PhysicalFrom="0" PhysicalTo="100"/>"#,
    );
    let profile = profile(&(dependent + &master));
    let fog = ("Fog1", AttributeValue::Number(50.0));
    assert!(
        profile
            .encode(
                &values(&[
                    fog.clone(),
                    ("Base_Mode.Mode.Idle", AttributeValue::Number(100.0))
                ]),
                &mut [0; 2]
            )
            .is_err()
    );
    assert_eq!(
        encode(
            &profile,
            &values(&[fog, ("Base_Mode.Mode.Enabled", AttributeValue::Number(0.0))])
        ),
        [128, 128]
    );
}

#[test]
fn a_function_mode_master_requires_its_own_mode_condition_to_be_active() {
    let dependent = channel(
        "Fog1",
        "Base",
        "1",
        &function(
            "Fog1",
            r#"ModeMaster="Base_Mode.Mode.Enabled" ModeFrom="0/1" ModeTo="255/1""#,
        ),
    );
    let master = channel(
        "Mode",
        "Base",
        "2",
        r#"
      <ChannelFunction Name="Idle" Attribute="Mode" DMXFrom="0/1" ModeMaster="Head1_Mode" ModeFrom="0/1" ModeTo="127/1"/>
      <ChannelFunction Name="Enabled" Attribute="Mode" DMXFrom="0/1" ModeMaster="Head1_Mode" ModeFrom="128/1" ModeTo="255/1"/>"#,
    );
    let gate = channel("Mode", "Head1", "3", &function("Mode", ""));
    let profile = profile(&(dependent + &master + &gate));
    let fog = ("Fog1", AttributeValue::Number(50.0));
    assert!(
        profile
            .encode(&values(std::slice::from_ref(&fog)), &mut [0; 3])
            .is_err()
    );
    assert_eq!(
        encode(
            &profile,
            &values(&[fog, ("Head1.Mode", AttributeValue::Number(100.0))])
        ),
        [128, 0, 255]
    );
}

#[test]
fn channel_sets_are_sorted_and_cannot_escape_a_mode_dependent_function_range() {
    let dependent = channel(
        "Fog1",
        "Base",
        "1",
        r#"
      <ChannelFunction Name="Fog" Attribute="Fog1" DMXFrom="0/1" PhysicalFrom="0" PhysicalTo="100">
        <ChannelSet Name="Boost" DMXFrom="200/1" PhysicalFrom="50" PhysicalTo="100"/>
        <ChannelSet Name="Low" DMXFrom="0/1" PhysicalFrom="0" PhysicalTo="49"/>
      </ChannelFunction>
      <ChannelFunction Name="Zoom" Attribute="Zoom" DMXFrom="128/1" ModeMaster="Base_Mode" ModeFrom="128/1" ModeTo="255/1"/>
    "#,
    );
    let profile = profile(&(dependent + &channel("Mode", "Base", "2", &function("Mode", ""))));
    assert_eq!(
        encode(
            &profile,
            &values(&[("Fog1", AttributeValue::Preset("Boost".into()))])
        ),
        [200, 0]
    );
    assert!(
        profile
            .encode(
                &values(&[
                    ("Fog1", AttributeValue::Preset("Boost".into())),
                    ("Mode", AttributeValue::Number(100.0))
                ]),
                &mut [0; 2]
            )
            .is_err()
    );
    assert_eq!(
        encode(
            &profile,
            &values(&[
                ("Fog1", AttributeValue::Number(49.0)),
                ("Mode", AttributeValue::Number(100.0))
            ])
        ),
        [127, 255]
    );
}

#[test]
fn malformed_offsets_cycles_and_unsupported_modes_fail_at_initialization() {
    let normal = channel("Fog1", "Base", "1", &function("Fog1", ""));
    for invalid in [
        normal.replace("Offset=\"1\"", "Offset=\"0\""),
        normal.replace("Offset=\"1\"", "Offset=\"1,1\""),
        normal.replace("Offset=\"1\"", "Offset=\"513\""),
        normal.replace("Offset=\"1\"", "Offset=\"1,2,3,4,5\""),
        normal.replace("Offset=\"1\"", "Offset=\"None\""),
        normal.replace("<DMXChannel", "<DMXChannel DMXBreak=\"2\""),
        channel(
            "Fog1",
            "Base",
            "1",
            &function(
                "Fog1",
                r#"ModeMaster="Base_Fog1" ModeFrom="0/1" ModeTo="255/1""#,
            ),
        ),
    ] {
        assert!(
            GdtfProfile::from_xml(&description(&invalid), "Control").is_err(),
            "{invalid}"
        );
    }
    assert!(GdtfProfile::from_xml(&description(&normal), "missing").is_err());
}

fn rig(channels: &str) -> FixtureRig {
    FixtureRig::from_wire(&wire::RigDefinition {
        fixtures: vec![wire::Fixture {
            fixture_id: "fixture".into(),
            profile_id: "test".into(),
            address: 1,
            ..Default::default()
        }],
        profiles: vec![wire::FixtureProfile {
            profile_id: "test".into(),
            gdtf_xml: description(channels),
            dmx_mode: "Control".into(),
        }],
        ..Default::default()
    })
    .unwrap()
}

fn engine(channels: &str, source: &str) -> Engine {
    let catalog = AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
        autoloop_id: "test".into(),
        bank: "test".into(),
        lua_source: source.into(),
    }])
    .unwrap();
    let mut engine = Engine::new(rig(channels), catalog).unwrap();
    engine
        .apply(
            0.0,
            &Command::Select {
                bank: "test".into(),
                autoloop: "test".into(),
                transition: Transition::Cut,
                palette: None,
            },
        )
        .unwrap();
    engine
}

#[test]
fn lua_controls_non_lighting_attributes_and_blackout_restores_defaults() {
    let channels = channel("Fog1", "Base", "1", &function("Fog1", ""));
    let mut engine = engine(
        &channels,
        "return {render=function(ctx) return sf.scene {sf.physical_attribute(sf.all(), 'Fog1', 50)} end}",
    );
    assert_eq!(
        engine.render_at(0.0).unwrap().dmx_frames_by_universe[&0][0],
        128
    );
    engine
        .apply(0.1, &Command::SetBlackout { enabled: true })
        .unwrap();
    assert_eq!(
        engine.render_at(0.1).unwrap().dmx_frames_by_universe[&0][0],
        0
    );
}

#[test]
fn explicit_dimmer_values_respect_master_and_blackout() {
    let channels = channel("Dimmer", "Base", "1", &function("Dimmer", ""));
    let mut engine = engine(
        &channels,
        "return {render=function(ctx) return sf.scene {sf.physical_attribute(sf.all(), 'Dimmer', 100)} end}",
    );
    engine
        .apply(
            0.0,
            &Command::SetMaster {
                intensity: 0.5,
                fade_seconds: 0.0,
            },
        )
        .unwrap();
    assert_eq!(
        engine.render_at(0.0).unwrap().dmx_frames_by_universe[&0][0],
        128
    );
    engine
        .apply(0.1, &Command::SetBlackout { enabled: true })
        .unwrap();
    assert_eq!(
        engine.render_at(0.1).unwrap().dmx_frames_by_universe[&0][0],
        0
    );
}

#[test]
fn white_only_emitters_follow_intensity_without_applying_colour_brightness_twice() {
    for with_dimmer in [false, true] {
        let mut channels = channel(
            "ColorAdd_W",
            "Base",
            "1",
            &function("ColorAdd_W", r#"Default="255/1""#),
        );
        if with_dimmer {
            channels += &channel("Dimmer", "Base", "2", &function("Dimmer", ""));
        }
        let profile = profile(&channels);
        for (intensity, expected) in [(0.0, 0), (0.5, 128), (1.0, 255)] {
            let output = encode(
                &profile,
                &FixtureState {
                    intensity,
                    ..Default::default()
                },
            );
            assert_eq!(
                output,
                if with_dimmer {
                    vec![255, expected]
                } else {
                    vec![expected]
                }
            );
        }
        let half_white = FixtureState {
            intensity: 0.5,
            colour: Rgb {
                red: 0.5,
                green: 0.5,
                blue: 0.5,
            },
            ..Default::default()
        };
        assert_eq!(
            encode(&profile, &half_white),
            if with_dimmer {
                vec![128, 128]
            } else {
                vec![64]
            }
        );
    }
}

#[test]
fn extra_emitters_ignore_lit_defaults_and_obey_master_blackout_idle_and_faults() {
    for (attribute, default, dark, dimmed) in [
        ("ColorAdd_A", "255/1", 0, 64),
        ("ColorSub_UV", "0/1", 255, 192),
    ] {
        let channels = channel(
            attribute,
            "Base",
            "1",
            &function(attribute, &format!(r#"Default="{default}""#)),
        );
        assert_eq!(
            encode(&profile(&channels), &FixtureState::default()),
            [dark]
        );
        let mut engine = engine(
            &channels,
            &format!(
                "return {{render=function(ctx) if ctx.time_seconds >= 1 then error('fixture fault') end; return {{sf.physical_attribute(sf.all(), '{attribute}', 50)}} end}}"
            ),
        );
        assert_eq!(
            engine.render_at(0.0).unwrap().dmx_frames_by_universe[&0][0],
            128
        );
        for (master, expected) in [(0.5, dimmed), (0.0, dark), (1.0, 128)] {
            engine
                .apply(
                    0.0,
                    &Command::SetMaster {
                        intensity: master,
                        fade_seconds: 0.0,
                    },
                )
                .unwrap();
            assert_eq!(
                engine.render_at(0.0).unwrap().dmx_frames_by_universe[&0][0],
                expected
            );
        }
        engine
            .apply(0.1, &Command::SetBlackout { enabled: true })
            .unwrap();
        assert_eq!(
            engine.render_at(0.1).unwrap().dmx_frames_by_universe[&0][0],
            dark
        );
        engine
            .apply(0.2, &Command::SetBlackout { enabled: false })
            .unwrap();
        assert_eq!(
            engine.render_at(0.2).unwrap().dmx_frames_by_universe[&0][0],
            128
        );
        engine.apply(0.3, &Command::Stop).unwrap();
        assert_eq!(
            engine.render_at(0.3).unwrap().dmx_frames_by_universe[&0][0],
            dark
        );
        engine
            .apply(
                0.4,
                &Command::Select {
                    bank: "test".into(),
                    autoloop: "test".into(),
                    transition: Transition::Cut,
                    palette: None,
                },
            )
            .unwrap();
        assert_eq!(
            engine.render_at(0.4).unwrap().dmx_frames_by_universe[&0][0],
            128
        );
        let failed = engine.render_at(1.0).unwrap();
        assert!(failed.script_error.unwrap().contains("fixture fault"));
        assert_eq!(failed.dmx_frames_by_universe[&0][0], dark);
    }
}

#[test]
fn encoding_faults_latch_darkness_keep_safe_movement_and_allow_a_healthy_autoloop() {
    for (zoom_condition, extra_effect, diagnostic) in [
        (
            r#"ModeMaster="Base_Mode" ModeFrom="128/1" ModeTo="255/1""#,
            "sf.physical_attribute(sf.all(), 'Zoom', 50)",
            "inactive",
        ),
        (
            "",
            "sf.physical_attribute(sf.all(), 'Zoom', 50), sf.physical_attribute(sf.all(), 'Base.Zoom', 75)",
            "conflicting",
        ),
    ] {
        let channels = channel("Dimmer", "Base", "1", &function("Dimmer", ""))
            + &channel("Pan", "Base", "2", &function("Pan", ""))
            + &channel("Mode", "Base", "3", &function("Mode", ""))
            + &channel("Zoom", "Base", "4", &function("Zoom", zoom_condition));
        let broken = format!(
            "return {{render=function(ctx) if ctx.time_seconds == 0 then return {{sf.dimmer(sf.all(), 0.5), sf.pan_tilt(sf.all(), 0.25, 0.5)}} end; return {{sf.dimmer(sf.all(), 1), sf.pan_tilt(sf.all(), 0.9, 0.5), {extra_effect}}} end}}"
        );
        let catalog = AutoloopCatalog::from_definitions(&[
            wire::AutoloopDefinition { bank: "test".into(), autoloop_id: "broken".into(), lua_source: broken },
            wire::AutoloopDefinition { bank: "test".into(), autoloop_id: "healthy".into(), lua_source: "return {render=function(ctx) return {sf.dimmer(sf.all(), 0.75), sf.pan_tilt(sf.all(), 0.75, 0.5)} end}".into() },
        ]).unwrap();
        let mut engine = Engine::new(rig(&channels), catalog).unwrap();
        engine
            .play_autoloop(0.0, "broken", Transition::Cut, "techno")
            .unwrap();
        assert_eq!(
            &engine.render_at(0.0).unwrap().dmx_frames_by_universe[&0][..2],
            &[128, 64]
        );
        let failed = engine.render_at(0.1).unwrap();
        assert!(failed.script_error.as_ref().unwrap().contains(diagnostic));
        assert_eq!(&failed.dmx_frames_by_universe[&0][..2], &[0, 64]);
        let latched = engine.render_at(0.2).unwrap();
        assert_eq!(latched.script_error, failed.script_error);
        assert_eq!(
            latched.dmx_frames_by_universe,
            failed.dmx_frames_by_universe
        );
        engine
            .play_autoloop(0.3, "healthy", Transition::Cut, "techno")
            .unwrap();
        let recovered = engine.render_at(0.3).unwrap();
        assert!(recovered.script_error.is_none());
        assert_eq!(&recovered.dmx_frames_by_universe[&0][..2], &[191, 191]);
    }
}

#[test]
fn unsupported_brightness_ranges_are_rejected_before_they_can_bypass_blackout() {
    let unsafe_dimmer = channel(
        "Dimmer",
        "Base",
        "1",
        r#"<ChannelFunction Name="Dimmer" Attribute="Dimmer" Default="255/1" PhysicalFrom="0" PhysicalTo="100"><ChannelSet Name="Full" DMXFrom="0/1"/></ChannelFunction>"#,
    );
    let conditional_dimmer = channel(
        "Dimmer",
        "Base",
        "1",
        &function(
            "Dimmer",
            r#"ModeMaster="Base_Mode" ModeFrom="128/1" ModeTo="255/1""#,
        ),
    ) + &channel("Mode", "Base", "2", &function("Mode", ""));
    for channels in [unsafe_dimmer, conditional_dimmer] {
        let error = GdtfProfile::from_xml(&description(&channels), "Control").unwrap_err();
        assert!(
            error.to_string().contains("brightness attribute"),
            "{error:#}"
        );
    }
}

#[test]
fn unrelated_cells_keep_independent_brightness_and_blackout() {
    let mut channels = channel("Dimmer", "Head1", "1", &function("Dimmer", ""));
    for (geometry, offset) in [("Head1", 2), ("Head2", 5)] {
        for (index, attribute) in ["ColorAdd_R", "ColorAdd_G", "ColorAdd_B"]
            .iter()
            .enumerate()
        {
            channels += &channel(
                attribute,
                geometry,
                &(offset + index).to_string(),
                &function(attribute, ""),
            );
        }
    }
    let profile = profile(&channels);
    let state = FixtureState {
        intensity: 0.5,
        ..Default::default()
    };
    assert_eq!(
        encode(&profile, &state),
        [128, 255, 255, 255, 128, 128, 128]
    );
    assert_eq!(
        encode(&profile, &FixtureState::default()),
        [0, 255, 255, 255, 0, 0, 0]
    );
}

#[test]
fn unreferenced_gdtf_files_in_the_archive_do_not_block_an_independent_rig() {
    let used = description(&channel("Fog1", "Base", "1", &function("Fog1", "")));
    let scene = r#"<GeneralSceneDescription verMajor="1" verMinor="6"><Scene><Layers>
        <Layer uuid="00000000-0000-4000-A000-000000000001"><ChildList>
        <Fixture name="fog" uuid="00000000-0000-4000-9000-000000000001">
        <GDTFSpec>used.gdtf</GDTFSpec><GDTFMode>Control</GDTFMode>
        <Addresses><Address>1</Address></Addresses></Fixture>
        </ChildList></Layer></Layers></Scene></GeneralSceneDescription>"#;
    let archive = support::zipped(&[
        ("GeneralSceneDescription.xml", scene.as_bytes()),
        (
            "used.gdtf",
            &support::zipped(&[("description.xml", used.as_bytes())]),
        ),
        (
            "unused.gdtf",
            &support::zipped(&[("description.xml", b"invalid until selected")]),
        ),
    ]);
    let parsed = FixtureRig::from_mvr(&archive).unwrap();
    assert_eq!(parsed.fixture_ids(), ["fog"]);
}

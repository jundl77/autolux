mod support;

use std::io::{Cursor, Read};

use prost::Message;
use starfire::engine::{Engine, MAX_SCRIPT_ERROR_BYTES};
use starfire::model::{
    Command, LightingOverride, OverrideLifetime, PropertyValues, Target, TimedCommand, Transition,
};
use starfire::protocol::{
    ClientMessage as Operation, MAX_ERROR_BYTES, MAX_LOGIN_BYTES, MAX_MESSAGE_BYTES, ServerMessage,
    apply_command, read_control, read_event, read_session_control, wire,
};
use starfire::replay::{Replay, render_replay};
use starfire::script::AutoloopCatalog;

fn encoded(mut command: Operation) -> Vec<u8> {
    command.set_command_id(1);
    let mut bytes = Vec::new();
    command.write(&mut bytes).unwrap();
    bytes
}

struct Fragmented(Cursor<Vec<u8>>);
impl Read for Fragmented {
    fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
        let length = bytes.len().min(1);
        self.0.read(&mut bytes[..length])
    }
}

fn play(bank: &str, autoloop: &str) -> Operation {
    Operation::PlayAutoloop(wire::PlayAutoloop {
        command_id: 1,
        autoloop_id: if bank.is_empty() && autoloop.is_empty() {
            String::new()
        } else {
            format!("{bank}/{autoloop}")
        },
        transition: None,
        palette: if autoloop.is_empty() {
            String::new()
        } else {
            "techno".into()
        },
    })
}

fn parameter(kind: wire::ParameterType, value: wire::parameter::Value) -> Operation {
    Operation::SetParameter(wire::SetParameter {
        command_id: 1,
        parameter: Some(wire::Parameter {
            r#type: kind as i32,
            value: Some(value),
            ..Default::default()
        }),
    })
}

fn faded(kind: wire::ParameterType, value: f64, fade_seconds: f64) -> Operation {
    Operation::SetParameter(wire::SetParameter {
        command_id: 1,
        parameter: Some(wire::Parameter {
            r#type: kind as i32,
            value: Some(wire::parameter::Value::Number(value)),
            fade_seconds,
            ..Default::default()
        }),
    })
}

fn engine() -> Engine {
    Engine::new(
        support::room(),
        support::show_catalog(),
        support::palettes(),
    )
    .unwrap()
}

#[test]
fn fragmented_coalesced_frames_use_standalone_generated_payloads() {
    let selection = play("atmospheric", "cold_breathe");
    let mut data = encoded(selection.clone());
    assert_eq!(data[0], wire::MessageType::PlayAutoloop as u8);
    assert_eq!(
        u32::from_be_bytes(data[1..5].try_into().unwrap()) as usize,
        data.len() - 5
    );
    let payload = wire::PlayAutoloop::decode(&data[5..]).unwrap();
    assert_eq!(payload.autoloop_id, "atmospheric/cold_breathe");
    assert_eq!(payload.command_id, 1);
    data.extend(encoded(play("", "")));
    let mut input = Fragmented(Cursor::new(data));
    assert_eq!(read_control(&mut input).unwrap(), Some(selection));
    assert_eq!(read_control(&mut input).unwrap(), Some(play("", "")));
    assert!(read_control(&mut input).unwrap().is_none());
    let heartbeat = Operation::Heartbeat(wire::Heartbeat {});
    assert_eq!(
        read_control(&mut Cursor::new(encoded(heartbeat.clone()))).unwrap(),
        Some(heartbeat)
    );
    for message in [
        ServerMessage::Heartbeat(wire::Heartbeat::default()),
        ServerMessage::Metrics(wire::Metrics::default()),
    ] {
        let mut data = Vec::new();
        message.write(&mut data).unwrap();
        assert_eq!(data.len(), 5);
        assert_eq!(
            read_event(&mut Fragmented(Cursor::new(data))).unwrap(),
            Some(message)
        );
    }
}

#[test]
fn discarding_delayed_audio_round_trips_and_leaves_lighting_untouched() {
    let discard = Operation::DiscardDelayedAudio(wire::DiscardDelayedAudio { command_id: 7 });
    let data = encoded(discard);
    assert_eq!(data[0], wire::MessageType::DiscardDelayedAudio as u8);
    let decoded = read_control(&mut Fragmented(Cursor::new(data)))
        .unwrap()
        .unwrap();
    assert_eq!(decoded.command_id(), Some(1));
    assert!(matches!(decoded, Operation::DiscardDelayedAudio(_)));

    let mut engine = engine();
    apply_command(&mut engine, 0.0, &play("atmospheric", "cold_breathe")).unwrap();
    let before = engine.render_at(0.5).unwrap();
    apply_command(&mut engine, 0.5, &decoded).unwrap();
    let after = engine.render_at(0.5).unwrap();
    assert_eq!(before.dmx_frames_by_universe, after.dmx_frames_by_universe);
    assert_eq!(after.active_autoloop.as_deref(), Some("cold_breathe"));
}

fn login_with_sources(count: usize, source_bytes: usize) -> Operation {
    Operation::Login(wire::Login {
        command_id: 1,
        autoloops: (0..count)
            .map(|index| wire::AutoloopDefinition {
                autoloop_id: format!("atmospheric/loop_{index}"),
                bank: "atmospheric".to_owned(),
                lua_source: "-".repeat(source_bytes),
            })
            .collect(),
        palettes: vec![wire::Palette {
            name: "techno".into(),
            primary: Some(wire::RgbColor {
                red: 1.0,
                green: 0.82,
                blue: 0.6,
            }),
            black: Some(wire::RgbColor::default()),
            ..Default::default()
        }],
        ..Default::default()
    })
}

fn header(kind: wire::MessageType, length: usize) -> Vec<u8> {
    let mut bytes = vec![kind as u8];
    bytes.extend((length as u32).to_be_bytes());
    bytes
}

#[test]
fn only_login_may_use_the_large_message_limit() {
    let login = login_with_sources(16, 64 * 1024);
    let bytes = encoded(login.clone());
    assert!(bytes.len() > 1024 * 1024);
    assert_eq!(read_control(&mut Cursor::new(bytes)).unwrap(), Some(login));

    let oversized = login_with_sources(66, 64 * 1024);
    let error = oversized.write(&mut Vec::new()).unwrap_err();
    assert!(error.to_string().starts_with("Login payload of"));
    let error = read_control(&mut Cursor::new(header(
        wire::MessageType::Login,
        MAX_LOGIN_BYTES + 100 * 1024,
    )))
    .unwrap_err();
    assert_eq!(
        error.to_string(),
        format!(
            "Login payload of {} bytes exceeds the {MAX_LOGIN_BYTES}-byte limit",
            MAX_LOGIN_BYTES + 100 * 1024
        )
    );

    let play = play("atmospheric", &"x".repeat(3 * 1024));
    let bytes = encoded(play.clone());
    assert!(bytes.len() > 3 * 1024);
    assert_eq!(read_control(&mut Cursor::new(bytes)).unwrap(), Some(play));

    let mut parameter = header(wire::MessageType::SetParameter, 5 * 1024);
    parameter.extend(vec![0; 5 * 1024]);
    let error = read_control(&mut Cursor::new(parameter)).unwrap_err();
    assert_eq!(
        error.to_string(),
        format!("SetParameter payload of 5120 bytes exceeds the {MAX_MESSAGE_BYTES}-byte limit")
    );
}

fn padded_heartbeat(payload_bytes: usize) -> Vec<u8> {
    let data = payload_bytes - 3;
    let mut frame = header(wire::MessageType::Heartbeat, payload_bytes);
    frame.extend([0x7a, (data % 128) as u8 | 0x80, (data / 128) as u8]);
    frame.extend(vec![b'-'; data]);
    frame
}

#[test]
fn realtime_limit_is_inclusive_at_exactly_4096_bytes() {
    assert_eq!(
        read_control(&mut Cursor::new(padded_heartbeat(MAX_MESSAGE_BYTES))).unwrap(),
        Some(Operation::Heartbeat(wire::Heartbeat {}))
    );
    let error =
        read_control(&mut Cursor::new(padded_heartbeat(MAX_MESSAGE_BYTES + 1))).unwrap_err();
    assert_eq!(
        error.to_string(),
        "Heartbeat payload of 4097 bytes exceeds the 4096-byte limit"
    );
}

#[test]
fn login_after_a_successful_login_gets_only_the_realtime_limit() {
    let login = header(wire::MessageType::Login, MAX_MESSAGE_BYTES + 1);
    assert!(
        read_session_control(&mut Cursor::new(login.clone()), false)
            .unwrap_err()
            .to_string()
            .contains("truncated")
    );
    assert_eq!(
        read_session_control(&mut Cursor::new(login), true)
            .unwrap_err()
            .to_string(),
        "Login payload of 4097 bytes exceeds the 4096-byte limit"
    );
}

#[test]
fn unknown_type_with_a_huge_length_is_rejected_from_the_header() {
    let error = read_control(&mut Cursor::new([200, 0xff, 0xff, 0xff, 0xff])).unwrap_err();
    assert_eq!(error.to_string(), "unknown message type");
}

#[test]
fn metrics_at_maximum_field_lengths_fit_the_realtime_limit() {
    let metrics = ServerMessage::Metrics(wire::Metrics {
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
    });
    let mut frame = Vec::new();
    metrics.write(&mut frame).unwrap();
    assert!(frame.len() - 5 <= MAX_MESSAGE_BYTES);
    assert_eq!(read_event(&mut Cursor::new(frame)).unwrap(), Some(metrics));
}

#[test]
fn truncated_oversized_unknown_and_wrong_direction_frames_are_rejected() {
    let message = encoded(play("atmospheric", "cold_breathe"));
    for length in 1..message.len() {
        assert!(read_control(&mut Cursor::new(&message[..length])).is_err());
    }
    let mut oversized = vec![wire::MessageType::Heartbeat as u8];
    oversized.extend(((MAX_MESSAGE_BYTES + 1) as u32).to_be_bytes());
    assert!(read_control(&mut Cursor::new(oversized)).is_err());
    for kind in [
        0,
        255,
        wire::MessageType::LoginReply as u8,
        wire::MessageType::Metrics as u8,
    ] {
        assert!(read_control(&mut Cursor::new([kind, 0, 0, 0, 0])).is_err());
    }
    assert!(
        read_control(&mut Cursor::new([
            wire::MessageType::PlayAutoloop as u8,
            0,
            0,
            0,
            0
        ]))
        .is_err()
    );
    assert!(read_event(&mut Cursor::new(encoded(play("test", "first")))).is_err());
}

#[test]
fn global_parameter_types_are_enforced_and_invalid_updates_preserve_output() {
    use wire::{ParameterType as Kind, parameter::Value};
    let mut engine = engine();
    apply_command(&mut engine, 0.0, &play("atmospheric", "cold_breathe")).unwrap();
    let expected = engine.render_at(0.0).unwrap();
    for invalid in [
        parameter(Kind::OutputBrightness, Value::Number(-1.0)),
        parameter(Kind::AnimationSpeed, Value::Number(f64::NAN)),
        parameter(Kind::OutputBlackout, Value::Number(0.0)),
        parameter(Kind::AudioRmsNormalized, Value::Boolean(false)),
        parameter(Kind::Unspecified, Value::Number(0.5)),
        faded(Kind::AnimationSpeed, 1.0, 1.0),
        faded(Kind::OutputBrightness, 0.5, -1.0),
        Operation::SetParameter(wire::SetParameter {
            command_id: 1,
            parameter: Some(wire::Parameter {
                r#type: 999,
                value: Some(Value::Number(0.5)),
                ..Default::default()
            }),
        }),
    ] {
        assert!(apply_command(&mut engine, 0.0, &invalid).is_err());
        assert_eq!(engine.render_at(0.0).unwrap(), expected);
    }
    for operation in [
        parameter(Kind::OutputBrightness, Value::Number(0.0)),
        parameter(Kind::OutputBlackout, Value::Boolean(false)),
    ] {
        assert_eq!(
            read_control(&mut Cursor::new(encoded(operation.clone()))).unwrap(),
            Some(operation.clone())
        );
        apply_command(&mut engine, 0.0, &operation).unwrap();
    }
    assert!(
        engine
            .render_at(0.0)
            .unwrap()
            .fixture_properties_before_strobe
            .values()
            .all(|state| state.intensity == 0.0)
    );
}

#[test]
fn playback_resolves_autoloop_ids_or_idle_and_validates_repeated_selection() {
    let mut engine = engine();
    apply_command(&mut engine, 0.0, &play("atmospheric", "cold_breathe")).unwrap();
    let expected = engine.render_at(0.0).unwrap();
    for operation in [
        play("atmospheric", ""),
        play("", "cold_breathe"),
        Operation::PlayAutoloop(wire::PlayAutoloop {
            transition: Some(wire::play_autoloop::Transition::default()),
            ..Default::default()
        }),
        Operation::PlayAutoloop(wire::PlayAutoloop {
            autoloop_id: "atmospheric/cold_breathe".into(),
            transition: Some(wire::play_autoloop::Transition {
                crossfade_beats: Some(f64::NAN),
            }),
            palette: "techno".into(),
            ..Default::default()
        }),
        Operation::PlayAutoloop(wire::PlayAutoloop {
            autoloop_id: "drop/kick".into(),
            ..Default::default()
        }),
        Operation::PlayAutoloop(wire::PlayAutoloop {
            autoloop_id: "drop/kick".into(),
            palette: "neon".into(),
            ..Default::default()
        }),
        Operation::PlayAutoloop(wire::PlayAutoloop {
            palette: "techno".into(),
            ..Default::default()
        }),
    ] {
        assert!(apply_command(&mut engine, 0.0, &operation).is_err());
        assert_eq!(engine.render_at(0.0).unwrap(), expected);
    }
    apply_command(&mut engine, 0.0, &play("", "")).unwrap();
    assert!(engine.render_at(0.0).unwrap().active_autoloop.is_none());
}

#[test]
fn optional_effect_properties_preserve_zero_and_invalid_replacements_preserve_expiry() {
    let mut engine = engine();
    apply_command(&mut engine, 0.0, &play("atmospheric", "cold_breathe")).unwrap();
    let effect = wire::SetActiveEffect {
        command_id: 1,
        effect_id: "accent".into(),
        intensity: Some(0.0),
        strobe_hz: Some(8.0),
        duration_seconds: Some(1.0),
        ..Default::default()
    };
    let operation = Operation::SetActiveEffect(effect.clone());
    assert_eq!(
        read_control(&mut Cursor::new(encoded(operation.clone()))).unwrap(),
        Some(operation.clone())
    );
    apply_command(&mut engine, 0.0, &operation).unwrap();
    for invalid in [
        wire::SetActiveEffect {
            duration_seconds: Some(0.0),
            ..effect.clone()
        },
        wire::SetActiveEffect {
            strobe_hz: Some(11.0),
            ..effect.clone()
        },
        wire::SetActiveEffect {
            target: Some(wire::set_active_effect::Target::Group("missing".into())),
            ..effect.clone()
        },
        wire::SetActiveEffect {
            effect_id: "accent".into(),
            ..Default::default()
        },
    ] {
        assert!(apply_command(&mut engine, 0.5, &Operation::SetActiveEffect(invalid)).is_err());
        assert!(
            engine
                .render_at(0.5)
                .unwrap()
                .fixture_properties_before_strobe
                .values()
                .all(|state| state.intensity == 0.0 && state.strobe_hz == 8.0)
        );
    }
    assert!(
        engine
            .render_at(1.0)
            .unwrap()
            .fixture_properties_before_strobe
            .values()
            .any(|state| state.intensity > 0.0 && state.strobe_hz == 0.0)
    );
}

#[test]
fn standalone_controls_and_recorded_commands_produce_identical_dmx() {
    use wire::{ParameterType as Kind, parameter::Value};
    let controls = vec![
        (
            0.0,
            Operation::UpdateBeat(wire::UpdateBeat {
                beat_index: 7,
                bar_phase: Some(2),
                beats_per_bar: 4,
                tempo_bpm: 140.0,
                ..Default::default()
            }),
            Command::Beat {
                beat_index: 7,
                bar_phase: Some(2),
                beats_per_bar: 4,
                bpm: 140.0,
            },
        ),
        (
            0.0,
            play("atmospheric", "cold_breathe"),
            Command::Select {
                bank: "atmospheric".into(),
                autoloop: "cold_breathe".into(),
                transition: Transition::Cut,
                palette: "techno".into(),
            },
        ),
        (
            0.25,
            parameter(Kind::AudioRmsNormalized, Value::Number(0.2)),
            Command::SetNormalizedRmsLevel { level: 0.2 },
        ),
        (
            0.5,
            parameter(Kind::OutputBrightness, Value::Number(0.6)),
            Command::SetMaster {
                intensity: 0.6,
                fade_seconds: 0.0,
            },
        ),
        (
            0.6,
            faded(Kind::OutputBrightness, 0.3, 0.5),
            Command::SetMaster {
                intensity: 0.3,
                fade_seconds: 0.5,
            },
        ),
        (
            0.75,
            Operation::PlayAutoloop(wire::PlayAutoloop {
                autoloop_id: "drop/kick".into(),
                transition: Some(wire::play_autoloop::Transition {
                    crossfade_beats: Some(2.0),
                }),
                palette: "house".into(),
                ..Default::default()
            }),
            Command::Select {
                bank: "drop".into(),
                autoloop: "kick".into(),
                transition: Transition::Crossfade {
                    duration_beats: 2.0,
                },
                palette: "house".into(),
            },
        ),
        (
            0.9,
            parameter(Kind::Palette, Value::Text("techno".into())),
            Command::SetPalette {
                name: "techno".into(),
            },
        ),
        (
            1.0,
            Operation::SetActiveEffect(wire::SetActiveEffect {
                effect_id: "arrival".into(),
                strobe_hz: Some(8.0),
                duration_seconds: Some(0.5),
                ..Default::default()
            }),
            Command::SetOverride {
                value: LightingOverride {
                    id: "arrival".into(),
                    target: Target::All,
                    values: PropertyValues {
                        strobe_hz: Some(8.0),
                        ..Default::default()
                    },
                    intensity_multiplier: None,
                    lifetime: OverrideLifetime::Seconds {
                        duration_seconds: 0.5,
                    },
                    priority: 0,
                    special_fx: None,
                },
            },
        ),
        (
            1.25,
            parameter(Kind::AnimationSpeed, Value::Number(0.5)),
            Command::SetSpeed { multiplier: 0.5 },
        ),
        (
            1.5,
            parameter(Kind::OutputBlackout, Value::Boolean(true)),
            Command::SetBlackout { enabled: true },
        ),
        (
            1.75,
            parameter(Kind::OutputBlackout, Value::Boolean(false)),
            Command::SetBlackout { enabled: false },
        ),
        (2.0, play("", ""), Command::Stop),
    ];
    let replay = Replay {
        schema_version: 1,
        duration_seconds: 2.0,
        frame_rate_hz: 40,
        palettes: support::palette_definitions(),
        events: controls
            .iter()
            .map(|(at, _, command)| TimedCommand {
                at_seconds: *at,
                command: command.clone(),
            })
            .collect(),
    };
    let rig = support::room();
    let catalog = support::show_catalog();
    let expected = render_replay(&replay, rig.clone(), catalog.clone()).unwrap();
    let mut engine = Engine::new(rig, catalog, support::palettes()).unwrap();
    let mut controls = controls.into_iter().peekable();
    for frame in expected {
        while controls
            .peek()
            .is_some_and(|entry| entry.0 <= frame.time_seconds)
        {
            let (at, command, _) = controls.next().unwrap();
            apply_command(
                &mut engine,
                at,
                &read_control(&mut Cursor::new(encoded(command)))
                    .unwrap()
                    .unwrap(),
            )
            .unwrap();
        }
        assert_eq!(engine.render_at(frame.time_seconds).unwrap(), frame);
    }
}

#[test]
fn renderer_setting_parameters_round_trip_and_reject_fades_and_wrong_types() {
    use wire::{ParameterType as Kind, parameter::Value};
    let mut engine = engine();
    for operation in [
        parameter(Kind::Paused, Value::Boolean(true)),
        parameter(Kind::Paused, Value::Boolean(false)),
        parameter(Kind::DefaultCrossfadeBeats, Value::Number(2.0)),
        parameter(Kind::StrobeMaxRateHz, Value::Number(6.0)),
        parameter(Kind::StrobeMaxBurstSeconds, Value::Number(3.0)),
        parameter(Kind::Palette, Value::Text("house".into())),
        parameter(Kind::Intensity, Value::Number(0.3)),
    ] {
        assert_eq!(
            read_control(&mut Cursor::new(encoded(operation.clone()))).unwrap(),
            Some(operation.clone())
        );
        apply_command(&mut engine, 0.0, &operation).unwrap();
    }
    for invalid in [
        parameter(Kind::Paused, Value::Number(1.0)),
        parameter(Kind::DefaultCrossfadeBeats, Value::Boolean(true)),
        parameter(Kind::Palette, Value::Number(1.0)),
        parameter(Kind::Palette, Value::Text("neon".into())),
        faded(Kind::StrobeMaxRateHz, 6.0, 1.0),
        faded(Kind::Intensity, 0.5, 1.0),
        parameter(Kind::Intensity, Value::Number(1.5)),
        parameter(Kind::Intensity, Value::Boolean(true)),
        Operation::SetParameter(wire::SetParameter {
            command_id: 1,
            parameter: Some(wire::Parameter {
                r#type: Kind::Paused as i32,
                value: Some(Value::Boolean(true)),
                fade_seconds: 1.0,
                ..Default::default()
            }),
        }),
    ] {
        assert!(apply_command(&mut engine, 0.0, &invalid).is_err());
    }
    assert_eq!(engine.active_palette(), "house");
}

const ATTRIBUTE_LOOK: &str = "drop/attribute_sweep";

fn attribute_engine() -> Engine {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../show/autoloops");
    let definition = |bank: &str, id: &str, lua_source: String| wire::AutoloopDefinition {
        autoloop_id: id.into(),
        bank: bank.into(),
        lua_source,
    };
    Engine::new(
        starfire::fixtures::FixtureRig::from_wire(&support::room_definition_as_loops()).unwrap(),
        AutoloopCatalog::from_definitions(&[
            definition(
                "atmospheric",
                "atmospheric/cold_breathe",
                std::fs::read_to_string(root.join("atmospheric/cold_breathe.lua")).unwrap(),
            ),
            definition(
                "drop",
                ATTRIBUTE_LOOK,
                r#"return { render = function()
                    return sf.scene {
                        sf.dimmer(sf.all(), 1),
                        sf.physical_attribute(sf.group("fx"), "Effects1PosRotate", 90),
                    }
                end }"#
                    .into(),
            ),
        ])
        .unwrap(),
        support::palettes(),
    )
    .unwrap()
}

#[test]
fn an_omitted_transition_fades_by_default_taking_target_attributes_at_once_and_an_empty_one_cuts() {
    use wire::{ParameterType as Kind, parameter::Value};
    let mut engine = attribute_engine();
    apply_command(&mut engine, 0.0, &play("atmospheric", "cold_breathe")).unwrap();
    apply_command(
        &mut engine,
        0.0,
        &parameter(Kind::DefaultCrossfadeBeats, Value::Number(4.0)),
    )
    .unwrap();
    let before = engine.render_at(0.0).unwrap();
    let cut = Operation::PlayAutoloop(wire::PlayAutoloop {
        command_id: 1,
        autoloop_id: ATTRIBUTE_LOOK.into(),
        transition: Some(wire::play_autoloop::Transition::default()),
        palette: "techno".into(),
    });
    let mut cutting = engine;
    apply_command(&mut cutting, 0.0, &cut).unwrap();
    let mut defaulted = attribute_engine();
    apply_command(&mut defaulted, 0.0, &play("atmospheric", "cold_breathe")).unwrap();
    apply_command(
        &mut defaulted,
        0.0,
        &parameter(Kind::DefaultCrossfadeBeats, Value::Number(4.0)),
    )
    .unwrap();
    apply_command(&mut defaulted, 0.0, &play("drop", "attribute_sweep")).unwrap();
    let faded = defaulted.render_at(0.0).unwrap();
    let cut = cutting.render_at(0.0).unwrap();
    let mut expected = before.fixture_properties_before_strobe.clone();
    for (id, state) in &mut expected {
        state
            .attributes
            .clone_from(&cut.fixture_properties_before_strobe[id].attributes);
    }
    assert!(expected.values().any(|state| !state.attributes.is_empty()));
    assert_eq!(faded.fixture_properties_before_strobe, expected);
    assert_ne!(
        cut.fixture_properties_before_strobe,
        before.fixture_properties_before_strobe
    );
}

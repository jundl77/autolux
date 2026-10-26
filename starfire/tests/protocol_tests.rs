use std::io::{Cursor, Read};

use prost::Message;
use starfire::engine::Engine;
use starfire::fixtures::FixtureRig;
use starfire::model::{
    Command, LightingOverride, OverrideLifetime, PropertyValues, Target, TimedCommand, Transition,
};
use starfire::protocol::{
    ClientMessage as Operation, MAX_MESSAGE_BYTES, ServerMessage, apply_command, read_control,
    read_event, wire,
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
        }),
    })
}

fn engine() -> Engine {
    Engine::new(
        FixtureRig::from_mvr(include_bytes!("../../rigs/room.mvr")).unwrap(),
        AutoloopCatalog::load(
            &std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../autoloops"),
        )
        .unwrap(),
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
        ..Default::default()
    })
}

// Regression: a ~40-script library outgrew the old 64 KiB frame at live start.
#[test]
fn login_carrying_a_large_autoloop_library_round_trips() {
    let login = login_with_sources(16, 64 * 1024);
    let bytes = encoded(login.clone());
    assert!(bytes.len() > 1024 * 1024);
    assert_eq!(read_control(&mut Cursor::new(bytes)).unwrap(), Some(login));
}

#[test]
fn login_over_the_message_limit_is_rejected_on_both_ends() {
    let oversized = login_with_sources(MAX_MESSAGE_BYTES / (64 * 1024) + 1, 64 * 1024);
    assert!(oversized.write(&mut Vec::new()).is_err());
    // The length prefix alone must fail before the payload buffer is allocated.
    let mut header = vec![wire::MessageType::Login as u8];
    header.extend(((MAX_MESSAGE_BYTES + 1) as u32).to_be_bytes());
    assert!(read_control(&mut Cursor::new(header)).is_err());
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
        // A fade on any parameter but brightness would be silently dropped, so reject it.
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
                position_beats: 0.0,
                tempo_bpm: 140.0,
                ..Default::default()
            }),
            Command::Beat {
                position_beats: 0.0,
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
                ..Default::default()
            }),
            Command::Select {
                bank: "drop".into(),
                autoloop: "kick".into(),
                transition: Transition::Crossfade {
                    duration_beats: 2.0,
                },
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
        events: controls
            .iter()
            .map(|(at, _, command)| TimedCommand {
                at_seconds: *at,
                command: command.clone(),
            })
            .collect(),
    };
    let rig = FixtureRig::from_mvr(include_bytes!("../../rigs/room.mvr")).unwrap();
    let catalog = AutoloopCatalog::load(
        &std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../autoloops"),
    )
    .unwrap();
    let expected = render_replay(&replay, rig.clone(), catalog.clone()).unwrap();
    let mut engine = Engine::new(rig, catalog).unwrap();
    let mut controls = controls.into_iter().peekable();
    for frame in expected.frames {
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
        faded(Kind::StrobeMaxRateHz, 6.0, 1.0),
        Operation::SetParameter(wire::SetParameter {
            command_id: 1,
            parameter: Some(wire::Parameter {
                r#type: Kind::Paused as i32,
                value: Some(Value::Boolean(true)),
                fade_seconds: 1.0,
            }),
        }),
    ] {
        assert!(apply_command(&mut engine, 0.0, &invalid).is_err());
    }
}

#[test]
fn an_omitted_transition_uses_the_default_and_an_empty_one_cuts() {
    use wire::{ParameterType as Kind, parameter::Value};
    let mut engine = engine();
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
        autoloop_id: "drop/kick".into(),
        transition: Some(wire::play_autoloop::Transition::default()),
    });
    let mut cutting = engine;
    apply_command(&mut cutting, 0.0, &cut).unwrap();
    let mut defaulted = self::engine();
    apply_command(&mut defaulted, 0.0, &play("atmospheric", "cold_breathe")).unwrap();
    apply_command(
        &mut defaulted,
        0.0,
        &parameter(Kind::DefaultCrossfadeBeats, Value::Number(4.0)),
    )
    .unwrap();
    apply_command(&mut defaulted, 0.0, &play("drop", "kick")).unwrap();
    // At the first instant a crossfade still shows the outgoing look; a cut does not.
    let faded = defaulted.render_at(0.0).unwrap();
    assert_eq!(
        faded.fixture_properties_before_strobe,
        before.fixture_properties_before_strobe
    );
    assert_ne!(
        cutting
            .render_at(0.0)
            .unwrap()
            .fixture_properties_before_strobe,
        before.fixture_properties_before_strobe
    );
}

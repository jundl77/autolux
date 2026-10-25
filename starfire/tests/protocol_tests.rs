use std::io::{Cursor, Read};

use prost::Message;
use starfire::engine::Engine;
use starfire::fixtures::FixtureRig;
use starfire::model::{Command, TimedCommand, Transition};
use starfire::parameters::number;
use starfire::protocol::{MAX_MESSAGE_BYTES, apply_command, read_control, wire};
use starfire::replay::{Replay, render_replay};
use starfire::script::SequenceCatalog;
use wire::renderer_command::Command as Operation;

fn encoded(command: Operation) -> Vec<u8> {
    let payload = wire::RendererCommand {
        command_id: 1,
        command: Some(command),
    }
    .encode_to_vec();
    let mut bytes = (payload.len() as u32).to_be_bytes().to_vec();
    bytes.extend(payload);
    bytes
}

struct Fragmented(Cursor<Vec<u8>>);
impl Read for Fragmented {
    fn read(&mut self, data: &mut [u8]) -> std::io::Result<usize> {
        let length = data.len().min(1);
        self.0.read(&mut data[..length])
    }
}

fn play(bank: &str, sequence: &str) -> Operation {
    Operation::PlaySequence(wire::PlaySequence {
        sequence: Some(wire::SequenceReference {
            bank_id: bank.into(),
            sequence_id: sequence.into(),
        }),
        transition: None,
    })
}

fn parameter(value: wire::set_parameter::Parameter) -> Operation {
    Operation::SetParameter(wire::SetParameter {
        parameter: Some(value),
    })
}

#[test]
fn fragmented_coalesced_commands_and_clean_eof_are_distinguished() {
    let beat = Operation::UpdateBeat(wire::UpdateBeat {
        position_beats: 0.0,
        tempo_bpm: 140.0,
    });
    let mut data = encoded(beat.clone());
    data.extend(encoded(Operation::PlaySequence(
        wire::PlaySequence::default(),
    )));
    let mut input = Fragmented(Cursor::new(data));
    assert_eq!(
        read_control(&mut input).unwrap().unwrap().command,
        Some(beat)
    );
    assert!(matches!(
        read_control(&mut input).unwrap().unwrap().command,
        Some(Operation::PlaySequence(_))
    ));
    assert!(read_control(&mut input).unwrap().is_none());
}

#[test]
fn truncated_oversized_empty_and_missing_operations_are_rejected() {
    let message = encoded(Operation::Heartbeat(wire::Heartbeat {}));
    for length in 1..message.len() {
        assert!(read_control(&mut Cursor::new(&message[..length])).is_err());
    }
    for length in [0_u32, (MAX_MESSAGE_BYTES + 1) as u32] {
        assert!(read_control(&mut Cursor::new(length.to_be_bytes())).is_err());
    }
    for command in [
        wire::RendererCommand {
            command_id: 0,
            command: Some(Operation::Heartbeat(wire::Heartbeat {})),
        },
        wire::RendererCommand {
            command_id: 1,
            command: None,
        },
    ] {
        let payload = command.encode_to_vec();
        let mut bytes = (payload.len() as u32).to_be_bytes().to_vec();
        bytes.extend(payload);
        assert!(read_control(&mut Cursor::new(bytes)).is_err());
    }
}

#[test]
fn zero_and_false_are_present_typed_parameter_values() {
    for value in [
        wire::set_parameter::Parameter::OutputBrightness(0.0),
        wire::set_parameter::Parameter::OutputBlackout(false),
    ] {
        let operation = parameter(value);
        let decoded = read_control(&mut Cursor::new(encoded(operation.clone())))
            .unwrap()
            .unwrap();
        assert_eq!(decoded.command, Some(operation));
    }
}

#[test]
fn protobuf_controls_and_recorded_commands_produce_identical_dmx() {
    use wire::set_parameter::Parameter;
    let mut crossfade = play("drop", "blue_impact");
    if let Operation::PlaySequence(value) = &mut crossfade {
        value.transition = Some(wire::Transition {
            policy: Some(wire::transition::Policy::CrossfadeBeats(2.0)),
        });
    }
    let controls = vec![
        (
            0.0,
            Operation::UpdateBeat(wire::UpdateBeat {
                position_beats: 0.0,
                tempo_bpm: 140.0,
            }),
            Command::Beat {
                position_beats: 0.0,
                bpm: 140.0,
            },
        ),
        (
            0.0,
            play("atmospheric", "blue_tide"),
            Command::Select {
                bank: "atmospheric".into(),
                sequence: "blue_tide".into(),
                transition: Transition::Cut,
            },
        ),
        (
            0.25,
            parameter(Parameter::AudioRmsNormalized(0.2)),
            Command::SetNormalizedRmsLevel { level: 0.2 },
        ),
        (
            0.5,
            parameter(Parameter::OutputBrightness(0.6)),
            Command::SetMaster { intensity: 0.6 },
        ),
        (
            0.75,
            crossfade,
            Command::Select {
                bank: "drop".into(),
                sequence: "blue_impact".into(),
                transition: Transition::Crossfade {
                    duration_beats: 2.0,
                },
            },
        ),
        (
            1.0,
            parameter(Parameter::AudioRmsNormalized(0.9)),
            Command::SetNormalizedRmsLevel { level: 0.9 },
        ),
        (
            1.25,
            parameter(Parameter::AnimationSpeed(0.5)),
            Command::SetSpeed { multiplier: 0.5 },
        ),
        (
            1.5,
            parameter(Parameter::OutputBlackout(true)),
            Command::SetBlackout { enabled: true },
        ),
        (
            1.75,
            parameter(Parameter::OutputBlackout(false)),
            Command::SetBlackout { enabled: false },
        ),
        (
            2.0,
            Operation::PlaySequence(wire::PlaySequence::default()),
            Command::Stop,
        ),
    ];
    let replay = Replay {
        schema_version: 1,
        duration_seconds: 2.0,
        frame_rate_hz: 40,
        events: controls
            .iter()
            .map(|(at_seconds, _, command)| TimedCommand {
                at_seconds: *at_seconds,
                command: command.clone(),
            })
            .collect(),
    };
    let rig = FixtureRig::from_json(include_str!("../rigs/room.json")).unwrap();
    let catalog =
        SequenceCatalog::load(&std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("sequences"))
            .unwrap();
    let expected = render_replay(&replay, rig.clone(), catalog.clone()).unwrap();
    let mut engine = Engine::new(rig, catalog).unwrap();
    let mut controls = controls.into_iter().peekable();
    for frame in expected.frames {
        while controls
            .peek()
            .is_some_and(|value| value.0 <= frame.time_seconds)
        {
            let (at_seconds, control, _) = controls.next().unwrap();
            let decoded = read_control(&mut Cursor::new(encoded(control)))
                .unwrap()
                .unwrap();
            apply_command(&mut engine, at_seconds, &decoded.command.unwrap()).unwrap();
        }
        assert_eq!(engine.render_at(frame.time_seconds).unwrap(), frame);
    }
}

const PARAMETER_SCRIPT: &str = r#"return {
    parameters = { level = {value_type='number', default_value=0.5, minimum=0, maximum=1},
                   enabled = {value_type='boolean', default_value=true},
                   count = {value_type='integer', default_value=9007199254740993, minimum=9007199254740992, maximum=9007199254740994},
                   mode = {value_type='text', default_value='red', allowed_values={'red','blue'}},
                   tint = {value_type='rgb', default_value={red=1, green=0, blue=0}} },
    render = function(ctx)
        assert(ctx.parameters.count == 9007199254740993)
        return sf.scene {sf.dimmer(sf.all(), ctx.parameters.enabled and ctx.parameters.level or 0),
            sf.colour(sf.all(), sf.rgb(ctx.parameters.tint.red, ctx.parameters.tint.green, ctx.parameters.tint.blue))}
    end
}"#;

fn test_engine() -> Engine {
    let directory = tempfile::tempdir().unwrap();
    std::fs::create_dir(directory.path().join("test")).unwrap();
    for name in ["first", "second"] {
        std::fs::write(
            directory.path().join(format!("test/{name}.lua")),
            PARAMETER_SCRIPT,
        )
        .unwrap();
    }
    let rig = FixtureRig::from_json(r#"{"schema_version":1,"fixtures":[{"id":"lamp","profile":"generic_rgb","universe":0,"address":1}]}"#).unwrap();
    Engine::new(rig, SequenceCatalog::load(directory.path()).unwrap()).unwrap()
}

fn sequence_parameter(sequence: &str, name: &str, value: wire::ParameterValue) -> Operation {
    parameter(wire::set_parameter::Parameter::SequenceParameter(
        wire::SequenceParameter {
            sequence: Some(wire::SequenceReference {
                bank_id: "test".into(),
                sequence_id: sequence.into(),
            }),
            name: name.into(),
            value: Some(value),
        },
    ))
}

fn level(engine: &mut Engine, at: f64) -> f64 {
    let frame = engine.render_at(at).unwrap();
    assert!(frame.script_error.is_none(), "{:?}", frame.script_error);
    frame.fixture_properties_before_strobe["lamp"].intensity
}

#[test]
fn sequence_parameters_validate_atomically_and_reselection_preserves_them() {
    let mut engine = test_engine();
    apply_command(&mut engine, 0.0, &play("test", "first")).unwrap();
    assert_eq!(level(&mut engine, 0.0), 0.5);
    apply_command(
        &mut engine,
        0.0,
        &sequence_parameter("first", "level", number(0.8)),
    )
    .unwrap();
    apply_command(&mut engine, 0.0, &play("test", "first")).unwrap();
    assert_eq!(level(&mut engine, 0.0), 0.8);
    for (name, value) in [
        ("level", number(-0.1)),
        ("level", number(f64::NAN)),
        ("missing", number(0.3)),
        ("enabled", number(1.0)),
        (
            "count",
            wire::ParameterValue {
                value: Some(wire::parameter_value::Value::Integer(9007199254740995)),
            },
        ),
        (
            "mode",
            wire::ParameterValue {
                value: Some(wire::parameter_value::Value::Text("green".into())),
            },
        ),
    ] {
        assert!(
            apply_command(&mut engine, 0.0, &sequence_parameter("first", name, value)).is_err()
        );
        assert_eq!(level(&mut engine, 0.0), 0.8);
    }
    apply_command(&mut engine, 0.0, &play("test", "second")).unwrap();
    assert!(
        apply_command(
            &mut engine,
            0.0,
            &sequence_parameter("first", "level", number(0.1))
        )
        .is_err()
    );
    assert_eq!(level(&mut engine, 0.0), 0.5);
    apply_command(
        &mut engine,
        0.0,
        &parameter(wire::set_parameter::Parameter::OutputBrightness(0.5)),
    )
    .unwrap();
    apply_command(
        &mut engine,
        0.0,
        &Operation::PlaySequence(wire::PlaySequence::default()),
    )
    .unwrap();
    apply_command(&mut engine, 0.0, &play("test", "first")).unwrap();
    assert_eq!(level(&mut engine, 0.0), 0.25);
}

#[test]
fn invalid_reselection_and_engine_values_preserve_output() {
    let mut engine = test_engine();
    apply_command(&mut engine, 0.0, &play("test", "first")).unwrap();
    for duration in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        let mut select = play("test", "first");
        if let Operation::PlaySequence(value) = &mut select {
            value.transition = Some(wire::Transition {
                policy: Some(wire::transition::Policy::CrossfadeBeats(duration)),
            });
        }
        assert!(apply_command(&mut engine, 0.0, &select).is_err());
    }
    for invalid in [
        wire::set_parameter::Parameter::OutputBrightness(-1.0),
        wire::set_parameter::Parameter::AnimationSpeed(f64::NAN),
        wire::set_parameter::Parameter::AudioRmsNormalized(2.0),
    ] {
        assert!(apply_command(&mut engine, 0.0, &parameter(invalid)).is_err());
        assert_eq!(level(&mut engine, 0.0), 0.5);
    }
}

#[test]
fn only_absent_sequence_without_a_transition_enters_idle() {
    let mut engine = test_engine();
    apply_command(&mut engine, 0.0, &play("test", "first")).unwrap();
    for invalid in [
        wire::PlaySequence {
            sequence: Some(wire::SequenceReference::default()),
            transition: None,
        },
        wire::PlaySequence {
            sequence: None,
            transition: Some(wire::Transition {
                policy: Some(wire::transition::Policy::Cut(wire::Empty {})),
            }),
        },
    ] {
        assert!(apply_command(&mut engine, 0.0, &Operation::PlaySequence(invalid)).is_err());
        assert_eq!(level(&mut engine, 0.0), 0.5);
    }
    apply_command(
        &mut engine,
        0.0,
        &Operation::PlaySequence(wire::PlaySequence::default()),
    )
    .unwrap();
    assert_eq!(level(&mut engine, 0.0), 0.0);
}

fn effect(
    id: &str,
    priority: i32,
    operation: wire::BlendOperation,
    value: f64,
) -> wire::SetActiveEffect {
    wire::SetActiveEffect {
        effect_id: id.into(),
        fixtures: Some(wire::FixtureSelection {
            selection: Some(wire::fixture_selection::Selection::All(wire::Empty {})),
        }),
        attributes: vec![wire::AttributeAssignment {
            attribute: wire::FixtureAttribute::Intensity as i32,
            value: Some(number(value)),
            operation: operation as i32,
        }],
        priority,
        lifetime: Some(wire::EffectLifetime {
            expiration: Some(wire::effect_lifetime::Expiration::UntilRemoved(
                wire::Empty {},
            )),
        }),
    }
}

#[test]
fn effects_compose_by_priority_then_order_with_per_effect_clamping() {
    let mut engine = test_engine();
    apply_command(&mut engine, 0.0, &play("test", "first")).unwrap();
    engine
        .set_active_effect(
            0.0,
            &effect("multiply", 0, wire::BlendOperation::Multiply, 8.0),
        )
        .unwrap();
    engine
        .set_active_effect(
            0.0,
            &effect("replace", 1, wire::BlendOperation::Replace, 0.6),
        )
        .unwrap();
    assert_eq!(level(&mut engine, 0.0), 0.6);
    engine
        .set_active_effect(
            0.0,
            &effect("attenuate", 2, wire::BlendOperation::Multiply, 0.5),
        )
        .unwrap();
    assert_eq!(level(&mut engine, 0.0), 0.3);
    engine
        .set_active_effect(
            0.0,
            &effect("replace", 0, wire::BlendOperation::Replace, 0.8),
        )
        .unwrap();
    assert_eq!(level(&mut engine, 0.0), 0.4);
    for invalid in [
        wire::SetActiveEffect {
            attributes: vec![],
            ..effect("replace", 0, wire::BlendOperation::Replace, 0.1)
        },
        wire::SetActiveEffect {
            lifetime: None,
            ..effect("replace", 0, wire::BlendOperation::Replace, 0.1)
        },
        effect("replace", 0, wire::BlendOperation::Multiply, 9.0),
    ] {
        assert!(engine.set_active_effect(0.0, &invalid).is_err());
        assert_eq!(level(&mut engine, 0.0), 0.4);
    }
    apply_command(
        &mut engine,
        0.0,
        &parameter(wire::set_parameter::Parameter::OutputBlackout(true)),
    )
    .unwrap();
    assert_eq!(level(&mut engine, 0.0), 0.0);
}

#[test]
fn effects_reject_duplicate_unknown_and_incompatible_attributes_without_replacement() {
    let mut engine = test_engine();
    apply_command(&mut engine, 0.0, &play("test", "first")).unwrap();
    let good = effect("hold", 0, wire::BlendOperation::Replace, 0.7);
    engine.set_active_effect(0.0, &good).unwrap();
    for attribute in [
        wire::AttributeAssignment {
            attribute: 999,
            ..good.attributes[0].clone()
        },
        wire::AttributeAssignment {
            operation: 999,
            ..good.attributes[0].clone()
        },
        wire::AttributeAssignment {
            value: Some(wire::ParameterValue {
                value: Some(wire::parameter_value::Value::Boolean(true)),
            }),
            ..good.attributes[0].clone()
        },
        wire::AttributeAssignment {
            attribute: wire::FixtureAttribute::Colour as i32,
            operation: wire::BlendOperation::Multiply as i32,
            value: Some(wire::ParameterValue {
                value: Some(wire::parameter_value::Value::Rgb(wire::RgbColor {
                    red: 1.0,
                    green: 0.0,
                    blue: 0.0,
                })),
            }),
        },
    ] {
        assert!(
            engine
                .set_active_effect(
                    0.0,
                    &wire::SetActiveEffect {
                        attributes: vec![attribute],
                        ..good.clone()
                    }
                )
                .is_err()
        );
        assert_eq!(level(&mut engine, 0.0), 0.7);
    }
    assert!(
        engine
            .set_active_effect(
                0.0,
                &wire::SetActiveEffect {
                    attributes: vec![good.attributes[0].clone(); 2],
                    ..good
                }
            )
            .is_err()
    );
    assert_eq!(level(&mut engine, 0.0), 0.7);
}

#[test]
fn beat_effect_expiry_ignores_phase_jumps_and_animation_speed_but_tracks_tempo() {
    let mut engine = test_engine();
    apply_command(&mut engine, 0.0, &play("test", "first")).unwrap();
    apply_command(
        &mut engine,
        0.0,
        &Operation::UpdateBeat(wire::UpdateBeat {
            position_beats: 0.0,
            tempo_bpm: 120.0,
        }),
    )
    .unwrap();
    let mut timed = effect("accent", 0, wire::BlendOperation::Replace, 1.0);
    timed.lifetime = Some(wire::EffectLifetime {
        expiration: Some(wire::effect_lifetime::Expiration::DurationBeats(4.0)),
    });
    engine.set_active_effect(0.0, &timed).unwrap();
    apply_command(
        &mut engine,
        0.5,
        &parameter(wire::set_parameter::Parameter::AnimationSpeed(0.0)),
    )
    .unwrap();
    apply_command(
        &mut engine,
        1.0,
        &Operation::UpdateBeat(wire::UpdateBeat {
            position_beats: 100.0,
            tempo_bpm: 60.0,
        }),
    )
    .unwrap();
    assert_eq!(level(&mut engine, 2.9), 1.0);
    assert_eq!(level(&mut engine, 3.0), 0.5);
}

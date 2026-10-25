use std::io::{Cursor, Read};

use prost::Message;
use starfire::model::{Command, OverrideLifetime, Target};
use starfire::protocol::{MAX_MESSAGE_BYTES, SCHEMA_VERSION, musical_command, read_control, wire};

fn encoded(command: wire::control::Command) -> Vec<u8> {
    let payload = wire::Control {
        schema_version: SCHEMA_VERSION,
        command: Some(command),
    }
    .encode_to_vec();
    let mut bytes = (payload.len() as u32).to_be_bytes().to_vec();
    bytes.extend(payload);
    bytes
}

struct Fragmented(Cursor<Vec<u8>>);

impl Read for Fragmented {
    fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
        let count = bytes.len().min(1);
        self.0.read(&mut bytes[..count])
    }
}

#[test]
fn fragmented_pipe_reads_preserve_message_boundaries_and_clean_eof() {
    let beat = wire::control::Command::Beat(wire::Beat {
        position_beats: 12.5,
        bpm: 140.0,
    });
    let mut data = encoded(beat.clone());
    data.extend(encoded(wire::control::Command::Stop(wire::Empty {})));
    let mut input = Fragmented(Cursor::new(data));
    assert_eq!(
        read_control(&mut input).unwrap().unwrap().command,
        Some(beat)
    );
    assert!(matches!(
        read_control(&mut input).unwrap().unwrap().command,
        Some(wire::control::Command::Stop(_))
    ));
    assert!(read_control(&mut input).unwrap().is_none());
}

#[test]
fn truncated_oversized_empty_unknown_version_and_missing_commands_are_rejected() {
    let message = encoded(wire::control::Command::Heartbeat(wire::Empty {}));
    for length in 1..message.len() {
        assert!(read_control(&mut Cursor::new(&message[..length])).is_err());
    }
    for length in [0_u32, (MAX_MESSAGE_BYTES + 1) as u32] {
        assert!(read_control(&mut Cursor::new(length.to_be_bytes())).is_err());
    }
    for control in [
        wire::Control {
            schema_version: SCHEMA_VERSION + 1,
            command: Some(wire::control::Command::Stop(wire::Empty {})),
        },
        wire::Control {
            schema_version: SCHEMA_VERSION,
            command: None,
        },
    ] {
        let payload = control.encode_to_vec();
        let mut bytes = (payload.len() as u32).to_be_bytes().to_vec();
        bytes.extend(payload);
        assert!(read_control(&mut Cursor::new(bytes)).is_err());
    }
}

#[test]
fn optional_zero_values_and_override_lifetime_survive_the_wire() {
    let control = wire::control::Command::SetOverride(wire::LightingOverride {
        id: "held_red".into(),
        target: Some(wire::Target {
            selection: Some(wire::target::Selection::Fixture("left".into())),
        }),
        values: Some(wire::Properties {
            intensity: Some(0.0),
            pan: Some(0.0),
            ..Default::default()
        }),
        lifetime: Some(wire::Lifetime {
            duration: Some(wire::lifetime::Duration::Beats(4.0)),
        }),
        priority: 7,
        ..Default::default()
    });
    let decoded = read_control(&mut Cursor::new(encoded(control)))
        .unwrap()
        .unwrap();
    let Some(Command::SetOverride { value }) = musical_command(decoded.command.unwrap()).unwrap()
    else {
        panic!("expected override");
    };
    assert_eq!(value.target, Target::Fixture("left".into()));
    assert_eq!(value.values.intensity, Some(0.0));
    assert_eq!(value.values.pan, Some(0.0));
    assert_eq!(value.values.tilt, None);
    assert_eq!(
        value.lifetime,
        OverrideLifetime::Beats {
            duration_beats: 4.0
        }
    );
    assert_eq!(value.priority, 7);
}

#[test]
fn ambiguous_targets_and_empty_overrides_are_rejected() {
    let mut value = wire::LightingOverride {
        id: "bad".into(),
        ..Default::default()
    };
    assert!(musical_command(wire::control::Command::SetOverride(value.clone())).is_err());
    value.target = Some(wire::Target {
        selection: Some(wire::target::Selection::All(wire::Empty {})),
    });
    assert!(musical_command(wire::control::Command::SetOverride(value)).is_err());
}

#[test]
fn protobuf_controls_and_recorded_commands_produce_identical_dmx() {
    use starfire::engine::Engine;
    use starfire::fixtures::FixtureRig;
    use starfire::model::{TimedCommand, Transition};
    use starfire::replay::{Replay, render_replay};
    use starfire::script::SequenceCatalog;
    let mut controls = vec![
        (
            0.0,
            wire::control::Command::Beat(wire::Beat {
                position_beats: 0.0,
                bpm: 140.0,
            }),
        ),
        (
            0.0,
            wire::control::Command::Select(wire::Select {
                bank: "atmospheric".into(),
                sequence: "blue_tide".into(),
                transition: None,
            }),
        ),
        (
            0.25,
            wire::control::Command::SetNormalizedRmsLevel(wire::SetNormalizedRmsLevel {
                level: 0.2,
            }),
        ),
        (
            0.5,
            wire::control::Command::SetMaster(wire::SetMaster { intensity: 0.6 }),
        ),
        (
            0.75,
            wire::control::Command::Select(wire::Select {
                bank: "drop".into(),
                sequence: "blue_impact".into(),
                transition: Some(wire::Transition {
                    policy: Some(wire::transition::Policy::CrossfadeBeats(2.0)),
                }),
            }),
        ),
        (
            1.0,
            wire::control::Command::SetNormalizedRmsLevel(wire::SetNormalizedRmsLevel {
                level: 0.9,
            }),
        ),
        (
            1.25,
            wire::control::Command::SetSpeed(wire::SetSpeed { multiplier: 0.5 }),
        ),
        (
            1.5,
            wire::control::Command::SetBlackout(wire::SetBlackout { enabled: true }),
        ),
        (
            1.75,
            wire::control::Command::SetBlackout(wire::SetBlackout { enabled: false }),
        ),
        (2.0, wire::control::Command::Stop(wire::Empty {})),
    ];
    let replay = Replay {
        schema_version: 1,
        duration_seconds: 2.0,
        frame_rate_hz: 40,
        events: vec![
            TimedCommand {
                at_seconds: 0.0,
                command: Command::Beat {
                    position_beats: 0.0,
                    bpm: 140.0,
                },
            },
            TimedCommand {
                at_seconds: 0.0,
                command: Command::Select {
                    bank: "atmospheric".into(),
                    sequence: "blue_tide".into(),
                    transition: Transition::Cut,
                },
            },
            TimedCommand {
                at_seconds: 0.25,
                command: Command::SetNormalizedRmsLevel { level: 0.2 },
            },
            TimedCommand {
                at_seconds: 0.5,
                command: Command::SetMaster { intensity: 0.6 },
            },
            TimedCommand {
                at_seconds: 0.75,
                command: Command::Select {
                    bank: "drop".into(),
                    sequence: "blue_impact".into(),
                    transition: Transition::Crossfade {
                        duration_beats: 2.0,
                    },
                },
            },
            TimedCommand {
                at_seconds: 1.0,
                command: Command::SetNormalizedRmsLevel { level: 0.9 },
            },
            TimedCommand {
                at_seconds: 1.25,
                command: Command::SetSpeed { multiplier: 0.5 },
            },
            TimedCommand {
                at_seconds: 1.5,
                command: Command::SetBlackout { enabled: true },
            },
            TimedCommand {
                at_seconds: 1.75,
                command: Command::SetBlackout { enabled: false },
            },
            TimedCommand {
                at_seconds: 2.0,
                command: Command::Stop,
            },
        ],
    };
    let rig = FixtureRig::from_json(include_str!("../rigs/room.json")).unwrap();
    let catalog =
        SequenceCatalog::load(&std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("sequences"))
            .unwrap();
    let expected = render_replay(&replay, rig.clone(), catalog.clone()).unwrap();
    let mut engine = Engine::new(rig, catalog).unwrap();
    for frame in expected.frames {
        while !controls.is_empty() && controls[0].0 <= frame.time_seconds {
            let (at_seconds, control) = controls.remove(0);
            let decoded = read_control(&mut Cursor::new(encoded(control)))
                .unwrap()
                .unwrap();
            engine
                .apply(
                    at_seconds,
                    &musical_command(decoded.command.unwrap()).unwrap().unwrap(),
                )
                .unwrap();
        }
        assert_eq!(engine.render_at(frame.time_seconds).unwrap(), frame);
    }
}

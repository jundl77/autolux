mod support;

use starfire::fixtures::FixtureRig;
use starfire::model::{FixtureState, Rgb};
use starfire::protocol::wire;
use starfire::special::capabilities;

#[test]
fn probe() {
    for source in [
        "corpus/robe_bmfl_spot#Mode 2 - Reduced 8 bit",
        "corpus/virtual_channel#Default",
        "corpus/WrongDmxValue#mode 4",
        "curved_dimmer#8 bit",
    ] {
        let profile = support::compiled_profile(source, "p");
        for (index, channel) in profile.mode.as_ref().unwrap().channels.iter().enumerate() {
            println!(
                "{source} ch{index} {:?} off {:?} def {} fns {:?}",
                channel.geometry_path,
                channel.offsets,
                channel.default,
                channel
                    .functions
                    .iter()
                    .map(|f| format!(
                        "{}@{}{}",
                        f.attribute,
                        f.dmx_from,
                        if f.mode_master.is_some() { "M" } else { "" }
                    ))
                    .collect::<Vec<_>>()
            );
        }
        let rig = FixtureRig::from_wire(&wire::RigDefinition {
            fixtures: vec![wire::Fixture {
                fixture_id: "f".into(),
                profile_id: "p".into(),
                address: 1,
                ..Default::default()
            }],
            groups: vec![wire::FixtureGroup {
                group_id: "g".into(),
                fixture_ids: vec!["f".into()],
            }],
            profiles: vec![profile],
        })
        .unwrap();
        println!("cells {:?} caps {:?}", rig.cell_ids(), capabilities(&rig));
        let mut states = rig.empty_states();
        for state in states.values_mut() {
            *state = FixtureState {
                intensity: 1.0,
                colour: Rgb {
                    red: 1.0,
                    green: 0.0,
                    blue: 0.0,
                },
                ..Default::default()
            };
        }
        let frame = &rig.encode(&states).unwrap()[&0];
        println!("red full {:?}", &frame[..40]);
    }
}

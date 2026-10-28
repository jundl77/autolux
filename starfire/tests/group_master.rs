mod support;

use starfire::engine::{Engine, perceptual_to_linear_master as curved};
use starfire::fixtures::FixtureRig;
use starfire::model::{Command, Transition};
use starfire::protocol::wire;
use starfire::script::AutoloopCatalog;

const LIT: &str = "return {render=function(ctx) return sf.scene {sf.dimmer(sf.all(), 1), sf.colour(sf.all(), sf.rgb(1, 0, 0))} end}";

fn group_master(group: &str, level: f64) -> wire::Parameter {
    wire::Parameter {
        r#type: wire::ParameterType::GroupMaster as i32,
        value: Some(wire::parameter::Value::Number(level)),
        group: group.into(),
        ..Default::default()
    }
}

fn lit_engine(definition: &wire::RigDefinition) -> Engine {
    let catalog = AutoloopCatalog::from_definitions(&[wire::AutoloopDefinition {
        autoloop_id: "lit".into(),
        bank: "test".into(),
        lua_source: LIT.into(),
    }])
    .unwrap();
    let rig = FixtureRig::from_wire(definition).unwrap();
    let mut engine = Engine::new(rig, catalog, support::palettes()).unwrap();
    for command in [
        Command::Beat {
            beat_index: 0,
            bar_phase: None,
            beats_per_bar: 4,
            bpm: 120.0,
        },
        Command::Select {
            bank: "test".into(),
            autoloop: "lit".into(),
            transition: Transition::Cut,
            palette: "techno".into(),
        },
    ] {
        engine.apply(0.0, &command).unwrap();
    }
    engine
}

/// The Mini Kinta has no Dimmer: Color1 is a wheel with a Black slot and Shutter1 only opens or
/// strobes, so the group master must reach its light through the wheel.
#[test]
fn a_group_master_on_the_dimmerless_kinta_keeps_its_colour_and_at_zero_selects_black() {
    let definition = support::room_definition_as_loops();
    let member = support::room().groups()["fx"][0].clone();
    let fixture = definition
        .fixtures
        .iter()
        .find(|fixture| fixture.fixture_uuid == member)
        .unwrap();
    let (universe, colour, shutter) = (
        u16::try_from(fixture.universe).unwrap(),
        fixture.address as usize - 1,
        fixture.address as usize,
    );
    let mut engine = lit_engine(&definition);
    let full = engine.render_at(0.0).unwrap();
    let full_dmx = &full.dmx_frames_by_universe[&universe];
    assert_ne!(full_dmx[colour], 0, "a lit Kinta sits on a coloured slot");

    engine.set_parameter(0.0, &group_master("fx", 0.5)).unwrap();
    let half = engine.render_at(0.0).unwrap();
    let half_dmx = &half.dmx_frames_by_universe[&universe];
    assert!((half.fixture_properties_before_strobe[&member].intensity - curved(0.5)).abs() < 1e-9);
    assert_eq!(half_dmx[colour], full_dmx[colour]);
    assert_eq!(half_dmx[shutter], full_dmx[shutter]);

    engine.set_parameter(0.0, &group_master("fx", 0.0)).unwrap();
    let dark = engine.render_at(0.0).unwrap();
    let dark_dmx = &dark.dmx_frames_by_universe[&universe];
    assert_eq!(
        dark.fixture_properties_before_strobe[&member].intensity,
        0.0
    );
    assert_eq!(dark_dmx[colour], 0, "Color1 Black slot");
    assert!(
        dark_dmx[shutter] < 6,
        "Shutter1 at rest, below its strobe range"
    );
}

#[test]
fn a_cell_in_two_capped_groups_takes_the_product_of_their_masters() {
    let mut definition = support::room_definition_as_loops();
    let wash = support::room().groups()["wash"].clone();
    let fx = support::room().groups()["fx"][0].clone();
    let (shared, wash_only) = (&wash[0], &wash[1]);
    definition.groups.push(wire::FixtureGroup {
        group_id: "house".into(),
        fixture_uuids: vec![shared.clone(), fx.clone()],
        ..Default::default()
    });
    let mut engine = lit_engine(&definition);
    let before = engine
        .render_at(0.0)
        .unwrap()
        .fixture_properties_before_strobe;
    engine
        .set_parameter(0.0, &group_master("wash", 0.5))
        .unwrap();
    engine
        .set_parameter(0.0, &group_master("house", 0.4))
        .unwrap();
    let capped = engine
        .render_at(0.0)
        .unwrap()
        .fixture_properties_before_strobe;
    let (wash_master, house_master) = (curved(0.5), curved(0.4));
    for (cell, factor) in [
        (shared, wash_master * house_master),
        (wash_only, wash_master),
        (&fx, house_master),
    ] {
        let expected = before[cell].intensity * factor;
        assert!(
            (capped[cell].intensity - expected).abs() < 1e-9,
            "{cell}: {} != {expected}",
            capped[cell].intensity
        );
        assert!(
            (capped[cell].attribute_brightness - factor).abs() < 1e-9,
            "{cell}"
        );
    }
}

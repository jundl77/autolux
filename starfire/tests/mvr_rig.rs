mod support;

use starfire::fixtures::FixtureRig;
use starfire::model::Target;
use starfire::mvr::rig_definition;

const SPOT: &str = "chauvet_intimidator_spot_110_12ch.gdtf";

fn scene(children: &str) -> String {
    format!(
        r#"<GeneralSceneDescription verMajor="1" verMinor="6"><Scene>
        <AUXData><Class uuid="00000000-0000-4000-8000-000000000001" name="movers"/></AUXData>
        <Layers><Layer uuid="00000000-0000-4000-A000-000000000001">
        <Matrix>{{1,0,0}}{{0,1,0}}{{0,0,1}}{{0,0,3000}}</Matrix><ChildList>{children}</ChildList>
        </Layer></Layers></Scene></GeneralSceneDescription>"#
    )
}

fn spot(name: &str, extra: &str, address: &str) -> String {
    format!(
        r#"<Fixture uuid="00000000-0000-4000-9000-{:012X}" name="{name}">{extra}
        <GDTFSpec>{SPOT}</GDTFSpec><GDTFMode>12ch</GDTFMode><FixtureID>1</FixtureID>
        <FixtureIDNumeric>1</FixtureIDNumeric><UnitNumber>0</UnitNumber>
        <Addresses>{address}</Addresses></Fixture>"#,
        name.len()
    )
}

fn pan_dmx(rig: &FixtureRig, fixture: &str, start: usize) -> [u8; 2] {
    let mut states = rig.empty_states();
    states.get_mut(fixture).unwrap().pan = 0.5;
    let frame = &rig.encode(&states).unwrap()[&0];
    [frame[start], frame[start + 1]]
}

#[test]
fn fixtures_nested_in_groups_take_their_parents_rotation() {
    let nested = scene(&format!(
        r#"<GroupObject uuid="00000000-0000-4000-B000-000000000001" name="truss">
        <Matrix>{{-1,0,0}}{{0,-1,0}}{{0,0,1}}{{0,2000,0}}</Matrix><ChildList>{}</ChildList></GroupObject>
        {}"#,
        spot(
            "inner",
            "<Classing>00000000-0000-4000-8000-000000000001</Classing>",
            "<Address>1</Address>"
        ),
        spot(
            "direct",
            "<Matrix>{-1,0,0}{0,-1,0}{0,0,1}{0,0,0}</Matrix>",
            "<Address break=\"0\">13</Address>"
        ),
    ));
    let rig = FixtureRig::from_mvr(&support::archive(&nested)).unwrap();
    assert_eq!(rig.fixture_ids(), ["inner", "direct"]);
    assert_eq!(
        rig.resolve(&Target::Group("movers".into())).unwrap(),
        ["inner"]
    );
    assert_eq!(pan_dmx(&rig, "inner", 0), pan_dmx(&rig, "direct", 12));
    let definition = rig_definition(&support::archive(&nested)).unwrap();
    assert_eq!(&definition.fixtures[0].matrix[9..], &[0.0, 2000.0, 3000.0]);
}

#[test]
fn unpatched_planning_fixtures_are_skipped() {
    let planning = scene(&format!(
        "{}{}",
        spot("patched", "", "<Address>1</Address>"),
        spot("planned", "", ""),
    ));
    let rig = FixtureRig::from_mvr(&support::archive(&planning)).unwrap();
    assert_eq!(rig.fixture_ids(), ["patched"]);
}

#[test]
fn unsupported_or_unresolved_patches_fail_with_the_fixture_name() {
    for (children, message) in [
        (
            spot(
                "split",
                "",
                "<Address>1</Address><Address break=\"1\">600</Address>",
            ),
            "one Address on break 0",
        ),
        (
            spot("far", "", "<Address>1.513</Address>"),
            "outside universes",
        ),
        (
            spot(
                "lost",
                "<Classing>00000000-0000-4000-8000-0000000000FF</Classing>",
                "<Address>1</Address>",
            ),
            "does not resolve to a Class",
        ),
        (
            spot(
                "bent",
                "<Matrix>{1,0,0}{1,0,0}{0,0,1}{0,0,0}</Matrix>",
                "<Address>1</Address>",
            ),
            "orthogonal",
        ),
        (
            spot("ghost", "", "<Address>1</Address>").replace(SPOT, "missing.gdtf"),
            "has no missing.gdtf",
        ),
    ] {
        let error = format!(
            "{:#}",
            FixtureRig::from_mvr(&support::archive(&scene(&children))).unwrap_err()
        );
        assert!(error.contains(message), "{error}");
    }
}

#[test]
fn file_and_login_paths_build_the_same_rig() {
    let archive = std::fs::read(support::rigs().join("room.mvr")).unwrap();
    let from_file = FixtureRig::from_mvr(&archive).unwrap();
    let from_login = FixtureRig::from_wire(&rig_definition(&archive).unwrap()).unwrap();
    let mut states = from_file.empty_states();
    for state in states.values_mut() {
        state.intensity = 0.5;
        state.pan = 0.3;
    }
    assert_eq!(
        from_file.encode(&states).unwrap(),
        from_login.encode(&states).unwrap()
    );
}

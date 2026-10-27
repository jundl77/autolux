mod support;

use std::fs;

use starfire::fixtures::FixtureRig;
use starfire::model::{RenderContext, Target};
use starfire::palette::PaletteSlots;
use starfire::script::{AutoloopCatalog, LuaAutoloop};

fn techno() -> PaletteSlots {
    support::palettes().slots("techno").unwrap()
}

fn rig() -> FixtureRig {
    support::rig(
        serde_json::from_str(
            r#"{"schema_version":1,"fixtures":[
          {"id":"left","profile":"generic_rgb","universe":0,"address":1},
          {"id":"right","profile":"generic_rgb","universe":0,"address":4}],
          "groups":{"wash":["right","left"]}}"#,
        )
        .unwrap(),
    )
}

fn catalog(source: &str) -> AutoloopCatalog {
    let directory = tempfile::tempdir().unwrap();
    fs::create_dir(directory.path().join("test")).unwrap();
    fs::write(directory.path().join("test/autoloop.lua"), source).unwrap();
    AutoloopCatalog::load(directory.path()).unwrap()
}

fn autoloop(source: &str) -> LuaAutoloop {
    catalog(source)
        .instantiate("test", "autoloop", &rig(), &support::palettes())
        .unwrap()
}

fn snapshot(beats: f64) -> RenderContext {
    RenderContext {
        time_seconds: beats / 2.0,
        beat_position: beats,
        autoloop_elapsed_beats: beats,
        autoloop_speed_scaled_elapsed_beats: beats,
        normalized_rms_level: Some(1.0),
        ..Default::default()
    }
}

#[test]
fn ctx_intensity_reaches_lua_and_softens_the_shared_beat_envelope() {
    let mut autoloop = autoloop(
        r#"return {render = function(ctx)
          return {sf.dimmer(sf.fixture("left"), ctx.intensity),
                  sf.dimmer(sf.fixture("right"), sf.on_beat(ctx.beat_position, 0.5))}
        end}"#,
    );
    let mut levels = |beats: f64, intensity: f64| {
        let effects = autoloop
            .render(
                &RenderContext {
                    intensity,
                    ..snapshot(beats)
                },
                &techno(),
            )
            .unwrap();
        (
            effects[0].values.intensity.unwrap(),
            effects[1].values.intensity.unwrap(),
        )
    };
    assert_eq!(levels(0.0, 1.0), (1.0, 1.0));
    assert_eq!(levels(0.25, 1.0), (1.0, 0.25));
    assert_eq!(levels(0.6, 1.0), (1.0, 0.0));
    let (exposed, soft_peak) = levels(0.0, 0.3);
    assert_eq!(exposed, 0.3);
    assert!((soft_peak - starfire::intensity::PULSE_DEPTH_CALM).abs() < 1e-12);
    assert!(levels(0.6, 0.3).1 > 0.0);
    assert!(
        autoloop
            .render(
                &RenderContext {
                    intensity: 1.5,
                    ..snapshot(0.0)
                },
                &techno(),
            )
            .is_err()
    );
}

#[test]
fn grid_helpers_given_ctx_use_the_authoritative_bar_fields() {
    let mut autoloop = autoloop(
        r#"return {render = function(ctx)
          -- A 3/4 bar: the bare-number form would assume four beats per bar.
          assert(sf.beat_in_bar(ctx) == 1.5 and sf.beat_in_bar(ctx.beat_position) == 3.5)
          assert(sf.bar(ctx) == 2 and sf.on_beat(ctx, 1) == 0.25)
          return {sf.dimmer(sf.group("wash"), sf.phrase_phase(ctx, 2))}
        end}"#,
    );
    let context = RenderContext {
        beat_position: 7.5,
        bar_phase: 1.5,
        bar_index: 2,
        beats_per_bar: 3,
        ..snapshot(7.5)
    };
    let effects = autoloop.render(&context, &techno()).unwrap();
    assert_eq!(effects[0].values.intensity, Some(0.25));
}

#[test]
fn composed_effects_resolve_named_groups_in_the_authored_order() {
    let mut autoloop = autoloop(
        r#"return {render = function(ctx)
          return sf.scene {
            sf.dimmer(sf.group("wash"), sf.osc.sine(ctx.autoloop_speed_scaled_elapsed_beats, 4, 0.2, 0.8)),
            sf.each(sf.group("wash"), function(fixture, index, count)
              return {sf.pan_tilt(fixture, index / count, 0.5),
                      sf.colour(fixture, sf.rgb(0, 0, 1))}
            end)
          }
        end}"#,
    );
    let effects = autoloop.render(&snapshot(2.0), &techno()).unwrap();
    assert_eq!(effects.len(), 5);
    assert!((effects[0].values.intensity.unwrap() - 0.8).abs() < 1e-12);
    assert_eq!(effects[1].target, Target::Fixture("right".into()));
    assert_eq!(effects[1].values.pan, Some(0.5));
    assert_eq!(effects[3].target, Target::Fixture("left".into()));
    assert_eq!(effects[3].values.pan, Some(1.0));
    let colour = effects[2].values.colour.unwrap();
    assert_eq!((colour.red, colour.green, colour.blue), (0.0, 0.0, 1.0));
}

#[test]
fn mirror_pairs_and_sides_follow_the_groups_left_to_right_order() {
    let rig = support::rig(
        serde_json::from_str(
            r#"{"schema_version":1,"fixtures":[
          {"id":"a","profile":"generic_rgb","universe":0,"address":1},
          {"id":"b","profile":"generic_rgb","universe":0,"address":4},
          {"id":"c","profile":"generic_rgb","universe":0,"address":7}],
          "groups":{"row":["c","a","b"]}}"#,
        )
        .unwrap(),
    );
    let mut autoloop = catalog(
        r#"return {render = function(ctx)
          local effects = {}
          for rank, pair in ipairs(sf.mirror(sf.group("row"))) do
            effects[#effects + 1] = sf.dimmer(pair[1], rank / 10)
            effects[#effects + 1] = sf.dimmer(pair[2], rank / 10)
          end
          return sf.scene {
            effects,
            sf.each(sf.group("row"), function(fixture, index, count)
              return sf.pan_tilt(fixture, 0.5 + sf.side(index, count) / 4, 0.5)
            end)
          }
        end}"#,
    )
    .instantiate("test", "autoloop", &rig, &support::palettes())
    .unwrap();
    let effects = autoloop.render(&snapshot(0.0), &techno()).unwrap();
    let mirrored: Vec<_> = effects[..4]
        .iter()
        .map(|effect| (effect.target.clone(), effect.values.intensity.unwrap()))
        .collect();
    assert_eq!(
        mirrored,
        [
            (Target::Fixture("c".into()), 0.1),
            (Target::Fixture("b".into()), 0.1),
            (Target::Fixture("a".into()), 0.2),
            (Target::Fixture("a".into()), 0.2),
        ]
    );
    let pans: Vec<_> = effects[4..]
        .iter()
        .map(|effect| effect.values.pan.unwrap())
        .collect();
    assert_eq!(pans, [0.25, 0.5, 0.75]);
}

fn at(x: f64, y: f64) -> String {
    format!("[1,0,0,0,1,0,0,0,1,{x},{y},0]")
}

fn placed(id: &str, address: u32, x: f64, y: f64) -> String {
    format!(
        r#"{{"id":"{id}","profile":"generic_rgb","universe":0,"address":{address},"matrix":{}}}"#,
        at(x, y)
    )
}

/// `area` is a StageArea name without its prefix, as Autolux would derive it.
fn placed_in(id: &str, address: u32, x: f64, y: f64, area: &str) -> String {
    let fixture = placed(id, address, x, y);
    format!(r#"{},"area":"{area}"}}"#, &fixture[..fixture.len() - 1])
}

/// Uneven spacing: the house-left members sit 100 mm off the house-right ones, the middle
/// 40 mm off the centreline, and two rows of different depth interleave in X, so rank would
/// pair across rows.
/// Areas are the thirds of the box X -1000..1000, Y -2000..0.
fn uneven_rig() -> FixtureRig {
    support::rig(
        serde_json::from_str(&format!(
            r#"{{"schema_version":1,"fixtures":[{},{},{},{},{}],
              "groups":{{"row":["a","b","m","c","d"]}}}}"#,
            placed_in("a", 1, -1000.0, 0.0, "UP_LEFT"),
            placed_in("b", 4, -900.0, -2000.0, "DOWN_LEFT"),
            placed_in("m", 7, 40.0, -1000.0, "CENTRE"),
            placed_in("c", 10, 900.0, 0.0, "UP_RIGHT"),
            placed_in("d", 13, 1000.0, -2000.0, "DOWN_RIGHT"),
        ))
        .unwrap(),
    )
}

#[test]
fn mirror_pairs_by_reflection_about_the_centreline_not_by_rank() {
    let rig = uneven_rig();
    assert!(rig.spatial().warnings().is_empty());
    let mut autoloop = catalog(
        r#"return {render = function(ctx)
          local effects = {}
          for rank, pair in ipairs(sf.mirror(sf.group("row"))) do
            effects[#effects + 1] = sf.dimmer(pair[1], rank / 10)
            effects[#effects + 1] = sf.dimmer(pair[2], rank / 10)
          end
          return effects
        end}"#,
    )
    .instantiate("test", "autoloop", &rig, &support::palettes())
    .unwrap();
    let pairs: Vec<_> = autoloop
        .render(&snapshot(0.0), &techno())
        .unwrap()
        .iter()
        .map(|effect| (effect.target.clone(), effect.values.intensity.unwrap()))
        .collect();
    let fixture = |id: &str| Target::Fixture(id.into());
    // Outermost first: a↔c and b↔d both reach 1000 mm, broken by the left member's rank.
    assert_eq!(
        pairs,
        [
            (fixture("a"), 0.1),
            (fixture("c"), 0.1),
            (fixture("b"), 0.2),
            (fixture("d"), 0.2),
            (fixture("m"), 0.3),
            (fixture("m"), 0.3),
        ]
    );
}

#[test]
fn lua_reads_zone_position_and_distance_in_metres() {
    let rig = uneven_rig();
    let mut autoloop = catalog(
        r#"return {render = function(ctx)
          return sf.each(sf.group("row"), function(fixture)
            local zone, depth, area = sf.zone(fixture)
            local x, y, z = sf.position(fixture)
            local level = (zone == "house_left" and 0.1 or zone == "centre" and 0.2 or 0.3)
              + (depth == "upstage" and 0.01 or depth == "centre" and 0.02 or 0)
              + (area == "centre" and 0.4 or 0) + z
            return {sf.dimmer(fixture, level),
                    sf.pan_tilt(fixture, x / 10 + 0.5, sf.distance(fixture, 0, -1) / 10)}
          end)
        end}"#,
    )
    .instantiate("test", "autoloop", &rig, &support::palettes())
    .unwrap();
    let effects = autoloop.render(&snapshot(0.0), &techno()).unwrap();
    let levels: Vec<_> = effects
        .iter()
        .filter_map(|effect| effect.values.intensity)
        .map(|level| (level * 100.0).round() / 100.0)
        .collect();
    assert_eq!(levels, [0.11, 0.1, 0.62, 0.31, 0.3]);
    let a = effects.iter().find_map(|effect| effect.values.pan).unwrap();
    assert!((a - 0.4).abs() < 1e-12, "{a}");
    let tilt_of_b = effects
        .iter()
        .filter_map(|effect| effect.values.tilt)
        .nth(1)
        .unwrap();
    assert!((tilt_of_b - 0.9f64.hypot(1.0) / 10.0).abs() < 1e-12);
}

#[test]
fn a_rig_with_a_fixture_outside_every_stage_area_is_refused_at_load() {
    let error = support::try_rig(
        serde_json::from_str(&format!(
            r#"{{"schema_version":1,"fixtures":[{}],"groups":{{"row":["a"]}}}}"#,
            placed_in("a", 1, 0.0, 0.0, "UNSPECIFIED"),
        ))
        .unwrap(),
    )
    .unwrap_err();
    assert!(
        format!("{error:#}").contains("fixture a has no stage area"),
        "{error:#}"
    );
}

#[test]
fn an_asymmetric_group_falls_back_to_rank_and_says_so() {
    let rig = support::rig(
        serde_json::from_str(&format!(
            r#"{{"schema_version":1,"fixtures":[{},{}],"groups":{{"row":["a","b"]}}}}"#,
            placed("a", 1, -2000.0, 0.0),
            placed("b", 4, 500.0, 0.0),
        ))
        .unwrap(),
    );
    assert_eq!(
        rig.spatial().mirror("row").unwrap(),
        [("a".to_owned(), "b".to_owned())]
    );
    let warnings = rig.spatial().warnings();
    assert!(warnings[0].contains("group row"), "{warnings:?}");
}

#[test]
fn oscillators_are_periodic_and_use_supplied_musical_time() {
    let mut autoloop = autoloop(
        r#"return {render = function(ctx)
          return sf.scene {
            sf.dimmer(sf.all(), sf.osc.triangle(ctx.autoloop_speed_scaled_elapsed_beats, 4, 0.2, 0.8)),
            sf.pan_tilt(sf.all(), sf.osc.pulse(ctx.autoloop_speed_scaled_elapsed_beats, 4, 0.3, 0.7, 0.25), 0.5)
          }
        end}"#,
    );
    let start = autoloop.render(&snapshot(0.0), &techno()).unwrap();
    let quarter = autoloop.render(&snapshot(1.0), &techno()).unwrap();
    let middle = autoloop.render(&snapshot(2.0), &techno()).unwrap();
    let repeated = autoloop.render(&snapshot(4.0), &techno()).unwrap();
    assert_eq!(start, repeated);
    assert_eq!(start[1].values.pan, Some(0.7));
    assert_eq!(quarter[1].values.pan, Some(0.3));
    assert!((middle[0].values.intensity.unwrap() - 0.8).abs() < 1e-12);
}

#[test]
fn infinite_loops_are_bounded_during_load_and_render() {
    let error = catalog("while true do end")
        .instantiate("test", "autoloop", &rig(), &support::palettes())
        .err()
        .unwrap();
    assert!(format!("{error:#}").contains("instruction budget exhausted"));

    let mut autoloop = autoloop("return {render = function(ctx) while true do end end}");
    let error = autoloop.render(&snapshot(0.0), &techno()).unwrap_err();
    assert!(format!("{error:#}").contains("instruction budget exhausted"));
}

#[test]
fn excessive_allocation_is_bounded_during_load_and_render() {
    let error = catalog("local large = string.rep('x', 32 * 1024 * 1024)")
        .instantiate("test", "autoloop", &rig(), &support::palettes())
        .err()
        .unwrap();
    assert!(format!("{error:#}").contains("memory"));

    let mut autoloop = autoloop(
        "return {render = function(ctx) local large = string.rep('x', 32 * 1024 * 1024); return {} end}",
    );
    let error = autoloop.render(&snapshot(0.0), &techno()).unwrap_err();
    assert!(format!("{error:#}").contains("memory"));
}

#[test]
fn bytecode_and_recursive_scene_output_are_rejected() {
    assert!(
        catalog("\u{1b}Lua\u{54}\0")
            .instantiate("test", "autoloop", &rig(), &support::palettes())
            .is_err()
    );
    let mut autoloop = autoloop(
        "return {render = function(ctx) local recursive = {}; recursive[1] = recursive; return sf.scene(recursive) end}",
    );
    assert!(autoloop.render(&snapshot(0.0), &techno()).is_err());
}

#[test]
fn sandbox_exposes_no_ambient_inputs_or_budget_escape() {
    let mut autoloop = autoloop(
        r#"local forbidden = {"os", "io", "debug", "package", "require", "load", "loadfile",
          "dofile", "coroutine", "pcall", "xpcall", "collectgarbage", "pairs", "next",
          "setmetatable", "getmetatable", "rawset", "print", "warn", "tostring"}
        for _, name in ipairs(forbidden) do assert(_G[name] == nil, name) end
        assert(math.random == nil and math.randomseed == nil and string.dump == nil and string.format == nil)
        return {render = function(ctx) return {sf.dimmer(sf.all(), 0.5)} end}"#,
    );
    assert_eq!(autoloop.render(&snapshot(0.0), &techno()).unwrap().len(), 1);
}

#[test]
fn pointer_identity_cannot_be_reflected_into_authored_effects() {
    for expression in [
        "tostring({})",
        "tostring(function() end)",
        "string.format('%p', {})",
        "string.format('%s', {})",
        "('%p'):format({})",
    ] {
        let source = format!(
            "return {{render = function(ctx) local address = {expression}; return {{sf.dimmer(sf.all(), 0.5)}} end}}"
        );
        let error = autoloop(&source)
            .render(&snapshot(0.0), &techno())
            .unwrap_err();
        assert!(
            format!("{error:#}").contains("nil value"),
            "{expression}: {error:#}"
        );
    }
}

#[test]
fn renderer_context_is_read_only() {
    let mut autoloop =
        autoloop("return {render = function(ctx) ctx.beat_position = 100; return {} end}");
    let error = autoloop.render(&snapshot(0.0), &techno()).unwrap_err();
    assert!(format!("{error:#}").contains("read-only"));
}

#[test]
fn nonfinite_input_is_rejected_before_lua_runs() {
    let mut autoloop = autoloop("return {render = function(ctx) error('Lua ran') end}");
    for value in [f64::NAN, f64::INFINITY, -1.0] {
        let error = autoloop.render(&snapshot(value), &techno()).unwrap_err();
        assert!(error.to_string().contains("finite nonnegative"));
    }
}

#[test]
fn invalid_properties_and_unknown_targets_are_rejected() {
    for expression in [
        "sf.dimmer(sf.all(), 0/0)",
        "sf.dimmer(sf.all(), math.huge)",
        "sf.dimmer(sf.all(), 1.1)",
        "sf.pan_tilt(sf.all(), -0.1, 0.5)",
        "sf.colour(sf.all(), sf.rgb(0, 0, 2))",
        "sf.strobe(sf.all(), 100)",
        "sf.dimmer(sf.fixture('missing'), 0.5)",
        "{target = sf.all(), values = {}}",
        "{target = sf.all(), values = {dmx = 255}}",
    ] {
        let source = format!("return {{render = function(ctx) return {{{expression}}} end}}");
        assert!(
            autoloop(&source).render(&snapshot(0.0), &techno()).is_err(),
            "accepted invalid effect: {expression}"
        );
    }
}

#[test]
fn output_must_be_a_bounded_dense_effect_array() {
    for body in [
        "local result = {}; for index = 1, 513 do result[index] = sf.dimmer(sf.all(), 0.5) end; return result",
        "return {[1] = sf.dimmer(sf.all(), 0.5), [3] = sf.dimmer(sf.all(), 0.5)}",
        "return {unexpected = sf.dimmer(sf.all(), 0.5)}",
        "return sf.dimmer(sf.all(), 0.5)",
    ] {
        let source = format!("return {{render = function(ctx) {body} end}}");
        assert!(autoloop(&source).render(&snapshot(0.0), &techno()).is_err());
    }
}

#[test]
fn selection_cannot_access_paths_outside_the_loaded_catalogue() {
    let catalog = catalog("return {render = function(ctx) return {} end}");
    assert!(
        catalog
            .instantiate("../test", "autoloop", &rig(), &support::palettes())
            .is_err()
    );
    assert!(
        catalog
            .instantiate("test", "../autoloop", &rig(), &support::palettes())
            .is_err()
    );
    assert!(
        catalog
            .instantiate("test", "autoloop.lua", &rig(), &support::palettes())
            .is_err()
    );
    assert_eq!(catalog.list()[0].bank, "test");
    assert_eq!(catalog.list()[0].autoloop, "autoloop");
}

#[test]
fn oversized_sources_are_rejected_before_compilation() {
    let directory = tempfile::tempdir().unwrap();
    fs::create_dir(directory.path().join("test")).unwrap();
    fs::write(
        directory.path().join("test/autoloop.lua"),
        " ".repeat(64 * 1024 + 1),
    )
    .unwrap();
    let error = AutoloopCatalog::load(directory.path()).unwrap_err();
    assert!(error.to_string().contains("source limit"));
}

#[test]
fn a_group_absent_from_the_rig_renders_nothing_without_error() {
    let mut autoloop = autoloop(
        r#"return {render = function(ctx)
          return sf.scene({
            sf.dimmer(sf.group("fx"), 1),
            sf.each(sf.group("fx"), function(fixture) return sf.dimmer(fixture, 1) end),
          })
        end}"#,
    );
    assert!(
        autoloop
            .render(&snapshot(0.0), &techno())
            .unwrap()
            .is_empty()
    );
}

#[test]
fn an_unknown_fixture_is_still_an_error() {
    let mut autoloop = autoloop(
        r#"return {render = function(ctx) return {sf.dimmer(sf.fixture("ghost"), 1)} end}"#,
    );
    let error = format!(
        "{:#}",
        autoloop.render(&snapshot(0.0), &techno()).unwrap_err()
    );
    assert!(error.contains("unknown fixture ghost"), "{error}");
}

#[test]
fn missing_groups_lists_exactly_the_referenced_groups_the_rig_lacks() {
    let catalog = catalog(
        r#"return {render = function(ctx)
          return {sf.dimmer(sf.group("wash"), 1), sf.dimmer(sf.group('fx'), 1),
            sf.dimmer(sf.group( "strip" ), 1), sf.dimmer(sf.group("fx"), 1)}
        end}"#,
    );
    let missing = catalog.groups_missing_from(&rig());
    assert_eq!(
        missing.iter().map(String::as_str).collect::<Vec<_>>(),
        ["fx", "strip"]
    );
    assert_eq!(
        starfire::script::missing_groups_warning(&missing).as_deref(),
        Some("groups not in this rig: fx, strip")
    );
    assert_eq!(
        starfire::script::missing_groups_warning(&Default::default()),
        None
    );
}

#[test]
fn a_loop_naming_an_accent_group_is_refused_and_the_shipped_loops_name_none() {
    let mut definition = support::room_definition();
    let fx = definition
        .groups
        .iter()
        .find(|group| group.group_id == "fx")
        .unwrap();
    assert_eq!(fx.role(), starfire::protocol::wire::GroupRole::Accent);
    let room = FixtureRig::from_wire(&definition).unwrap();
    support::show_catalog()
        .require_no_accent_groups(&room)
        .unwrap();
    let named_fx =
        catalog(r#"return {render = function(ctx) return {sf.dimmer(sf.group("fx"), 1)} end}"#);
    assert_eq!(
        named_fx
            .require_no_accent_groups(&room)
            .unwrap_err()
            .to_string(),
        "autoloops may not run on accent groups, which only play specials: test/autoloop names fx"
    );
    for group in &mut definition.groups {
        group.role = 0;
    }
    named_fx
        .require_no_accent_groups(&FixtureRig::from_wire(&definition).unwrap())
        .unwrap();
}

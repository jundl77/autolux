use std::fs;

use starfire::fixtures::FixtureRig;
use starfire::model::{RenderContext, Target};
use starfire::script::{AutoloopCatalog, LuaAutoloop};

fn rig() -> FixtureRig {
    FixtureRig::from_json(
        r#"{"schema_version":1,"fixtures":[
          {"id":"left","profile":"generic_rgb","universe":0,"address":1},
          {"id":"right","profile":"generic_rgb","universe":0,"address":4}],
          "groups":{"wash":["right","left"]}}"#,
    )
    .unwrap()
}

fn catalog(source: &str) -> AutoloopCatalog {
    let directory = tempfile::tempdir().unwrap();
    fs::create_dir(directory.path().join("test")).unwrap();
    fs::write(directory.path().join("test/autoloop.lua"), source).unwrap();
    AutoloopCatalog::load(directory.path()).unwrap()
}

fn autoloop(source: &str) -> LuaAutoloop {
    catalog(source)
        .instantiate("test", "autoloop", &rig())
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
    let effects = autoloop.render(&context).unwrap();
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
                      sf.colour(fixture, sf.hsv(240, 1, 1))}
            end)
          }
        end}"#,
    );
    let effects = autoloop.render(&snapshot(2.0)).unwrap();
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
fn oscillators_are_periodic_and_use_supplied_musical_time() {
    let mut autoloop = autoloop(
        r#"return {render = function(ctx)
          return sf.scene {
            sf.dimmer(sf.all(), sf.osc.triangle(ctx.autoloop_speed_scaled_elapsed_beats, 4, 0.2, 0.8)),
            sf.pan_tilt(sf.all(), sf.osc.pulse(ctx.autoloop_speed_scaled_elapsed_beats, 4, 0.3, 0.7, 0.25), 0.5)
          }
        end}"#,
    );
    let start = autoloop.render(&snapshot(0.0)).unwrap();
    let quarter = autoloop.render(&snapshot(1.0)).unwrap();
    let middle = autoloop.render(&snapshot(2.0)).unwrap();
    let repeated = autoloop.render(&snapshot(4.0)).unwrap();
    assert_eq!(start, repeated);
    assert_eq!(start[1].values.pan, Some(0.7));
    assert_eq!(quarter[1].values.pan, Some(0.3));
    assert!((middle[0].values.intensity.unwrap() - 0.8).abs() < 1e-12);
}

#[test]
fn infinite_loops_are_bounded_during_load_and_render() {
    let error = catalog("while true do end")
        .instantiate("test", "autoloop", &rig())
        .err()
        .unwrap();
    assert!(format!("{error:#}").contains("instruction budget exhausted"));

    let mut autoloop = autoloop("return {render = function(ctx) while true do end end}");
    let error = autoloop.render(&snapshot(0.0)).unwrap_err();
    assert!(format!("{error:#}").contains("instruction budget exhausted"));
}

#[test]
fn excessive_allocation_is_bounded_during_load_and_render() {
    let error = catalog("local large = string.rep('x', 32 * 1024 * 1024)")
        .instantiate("test", "autoloop", &rig())
        .err()
        .unwrap();
    assert!(format!("{error:#}").contains("memory"));

    let mut autoloop = autoloop(
        "return {render = function(ctx) local large = string.rep('x', 32 * 1024 * 1024); return {} end}",
    );
    let error = autoloop.render(&snapshot(0.0)).unwrap_err();
    assert!(format!("{error:#}").contains("memory"));
}

#[test]
fn bytecode_and_recursive_scene_output_are_rejected() {
    assert!(
        catalog("\u{1b}Lua\u{54}\0")
            .instantiate("test", "autoloop", &rig())
            .is_err()
    );
    let mut autoloop = autoloop(
        "return {render = function(ctx) local recursive = {}; recursive[1] = recursive; return sf.scene(recursive) end}",
    );
    assert!(autoloop.render(&snapshot(0.0)).is_err());
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
    assert_eq!(autoloop.render(&snapshot(0.0)).unwrap().len(), 1);
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
        let error = autoloop(&source).render(&snapshot(0.0)).unwrap_err();
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
    let error = autoloop.render(&snapshot(0.0)).unwrap_err();
    assert!(format!("{error:#}").contains("read-only"));
}

#[test]
fn nonfinite_input_is_rejected_before_lua_runs() {
    let mut autoloop = autoloop("return {render = function(ctx) error('Lua ran') end}");
    for value in [f64::NAN, f64::INFINITY, -1.0] {
        let error = autoloop.render(&snapshot(value)).unwrap_err();
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
        "sf.dimmer(sf.group('missing'), 0.5)",
        "sf.dimmer(sf.fixture('missing'), 0.5)",
        "{target = sf.all(), values = {}}",
        "{target = sf.all(), values = {dmx = 255}}",
    ] {
        let source = format!("return {{render = function(ctx) return {{{expression}}} end}}");
        assert!(
            autoloop(&source).render(&snapshot(0.0)).is_err(),
            "accepted invalid effect: {expression}"
        );
    }
}

#[test]
fn unknown_group_fails_inside_dynamic_fixture_spread() {
    let mut autoloop = autoloop(
        "return {render = function(ctx) return sf.each(sf.group('missing'), function(f) return sf.dimmer(f, 1) end) end}",
    );
    let error = autoloop.render(&snapshot(0.0)).unwrap_err();
    assert!(format!("{error:#}").contains("missing"), "{error:#}");
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
        assert!(autoloop(&source).render(&snapshot(0.0)).is_err());
    }
}

#[test]
fn selection_cannot_access_paths_outside_the_loaded_catalogue() {
    let catalog = catalog("return {render = function(ctx) return {} end}");
    assert!(catalog.instantiate("../test", "autoloop", &rig()).is_err());
    assert!(catalog.instantiate("test", "../autoloop", &rig()).is_err());
    assert!(catalog.instantiate("test", "autoloop.lua", &rig()).is_err());
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

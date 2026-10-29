//! Bounded Lua autoloops operating on musical time and fixture properties.

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::Read;
use std::path::Path;
use std::rc::Rc;
use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

use anyhow::{Context, Result, bail, ensure};
use mlua::chunk::ChunkMode;
use mlua::serde::SerializeOptions;
use mlua::{Function, HookTriggers, Lua, LuaOptions, LuaSerdeExt, StdLib, Table, Value, VmState};
use serde::Serialize;

use crate::fixtures::{FixtureRig, MAX_CELLS};
use crate::gdtf::GOBO_ATTRIBUTE;
use crate::intensity;
use crate::model::{Effect, MAX_STROBE_HZ, PropertyValues, RenderContext, Rgb, Target};
use crate::palette::{PaletteSlots, PaletteTable, WHITE, WHITE_SLOT};
use crate::protocol::wire;
use crate::spatial::MM_PER_METRE;

const MAX_SOURCE_BYTES: usize = 64 * 1024;
const MAX_CATALOGUE_BYTES: usize = 4 * 1024 * 1024;
const MAX_AUTOLOOPS: usize = 256;
const MAX_IDENTIFIER_BYTES: usize = 64;
const MAX_AUTOLOOP_ID_BYTES: usize = 128;
const LUA_MEMORY_LIMIT_BYTES: usize = 16 * 1024 * 1024;
const LUA_INSTRUCTION_LIMIT: u32 = 100_000;
const HOOK_INSTRUCTION_INTERVAL: u32 = 100;
const MAX_EFFECTS_PER_FRAME: usize = 512;
const LIGHTING_API: &str = include_str!("lua/lighting.lua");

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct AutoloopInfo {
    pub bank: String,
    pub autoloop: String,
}

#[derive(Clone, Debug)]
pub struct AutoloopCatalog {
    sources: BTreeMap<(String, String), String>,
    identities: BTreeMap<String, (String, String)>,
    /// Loops whose intensity composes HTP as an overlay (Login metadata).
    intensity_htp: BTreeSet<(String, String)>,
}

/// Banks whose loops play as cue overlays; their header must say how they compose.
const OVERLAY_BANKS: [&str; 5] = ["hit", "fill", "riser", "dip", "accent"];
/// Autolux's OVERLAY_META_VALUES: the keys every overlay header declares, with their values.
const OVERLAY_HEADER: [(&str, &[&str]); 4] = [
    ("strobe", &["yes", "no"]),
    ("intensity", &["htp", "ltp"]),
    ("movement", &["still", "sweep", "snap"]),
    ("texture", &["wash", "beam", "pixel", "mixed"]),
];

/// An overlay's header, read as Autolux's read_overlay_meta reads it for Login: the
/// `-- key: value` block right after the first line, keys in any order, each once, all required.
fn overlay_htp(source: &str) -> Result<bool> {
    let mut values: BTreeMap<&str, &str> = BTreeMap::new();
    for line in source.split('\n').skip(1) {
        let line = line.strip_suffix('\r').unwrap_or(line);
        let Some((key, value)) = line
            .strip_prefix("-- ")
            .and_then(|entry| entry.split_once(": "))
        else {
            break;
        };
        let Some((_, allowed)) = OVERLAY_HEADER.iter().find(|(name, _)| *name == key) else {
            break;
        };
        ensure!(
            allowed.contains(&value),
            "{key} {value:?} is not one of {}",
            allowed.join(", ")
        );
        ensure!(values.insert(key, value).is_none(), "{key} declared twice");
    }
    for (key, _) in OVERLAY_HEADER {
        ensure!(
            values.contains_key(key),
            "missing {key} in the header block after the first line"
        );
    }
    Ok(values["intensity"] == "htp")
}

/// Every catalogue script compiled once at Login; cues and selections only look identities up,
/// so nothing compiles on the render thread. One VM serves every layer naming the loop, which
/// Lua purity allows: a render is a function of its ctx alone.
pub struct CompiledScripts {
    scripts: BTreeMap<(String, String), Compiled>,
}

/// A shared VM, or the compile error a command naming the loop reports.
type Compiled = std::result::Result<Rc<RefCell<LuaAutoloop>>, String>;

impl CompiledScripts {
    pub fn get(&self, bank: &str, autoloop: &str) -> Result<Rc<RefCell<LuaAutoloop>>> {
        match self.scripts.get(&(bank.to_owned(), autoloop.to_owned())) {
            Some(Ok(script)) => Ok(Rc::clone(script)),
            Some(Err(error)) => bail!("{error}"),
            None => bail!("unknown autoloop {bank}/{autoloop}"),
        }
    }

    /// Renders every script twice on one frame: one VM serves the base and overlay layers, so
    /// state kept in chunk-level upvalues (which the frozen globals cannot stop) would leak
    /// between them. A loop whose second render differs is refused. A render that fails is
    /// left to the frame probe, which reports it in context.
    pub fn check_pure(&self, context: &RenderContext, palette: &PaletteSlots) -> Result<()> {
        for ((bank, autoloop), script) in &self.scripts {
            let Ok(script) = script else {
                continue;
            };
            let mut script = script.borrow_mut();
            let (Ok(first), Ok(second)) = (
                script.render(context, palette),
                script.render(context, palette),
            ) else {
                continue;
            };
            ensure!(
                first == second,
                "autoloop {bank}/{autoloop} renders one frame two ways: it keeps state between \
                 renders, which a shared VM forbids"
            );
        }
        Ok(())
    }
}

impl AutoloopCatalog {
    pub fn load(root: &Path) -> Result<Self> {
        let root = root
            .canonicalize()
            .context("cannot open autoloop directory")?;
        let mut sources = BTreeMap::new();
        let mut source_bytes = 0;
        for entry in fs::read_dir(&root)? {
            let entry = entry?;
            if !entry.file_type()?.is_dir() {
                continue;
            }
            let bank = entry
                .file_name()
                .into_string()
                .map_err(|_| anyhow::anyhow!("bank name is not UTF-8"))?;
            validate_identifier(&bank)?;
            for autoloop in fs::read_dir(entry.path())? {
                let autoloop = autoloop?;
                let path = autoloop.path();
                if path.extension().and_then(|value| value.to_str()) != Some("lua") {
                    continue;
                }
                ensure!(
                    autoloop.file_type()?.is_file(),
                    "autoloop must be a regular file"
                );
                ensure!(
                    path.canonicalize()?.starts_with(&root),
                    "autoloop escapes catalogue directory"
                );
                let name = path
                    .file_stem()
                    .and_then(|value| value.to_str())
                    .context("autoloop name is not UTF-8")?
                    .to_owned();
                validate_identifier(&name)?;
                let mut source = String::new();
                File::open(&path)?
                    .take((MAX_SOURCE_BYTES + 1) as u64)
                    .read_to_string(&mut source)?;
                ensure!(
                    source.len() <= MAX_SOURCE_BYTES,
                    "autoloop {bank}/{name} exceeds source limit"
                );
                source_bytes += source.len();
                ensure!(
                    source_bytes <= MAX_CATALOGUE_BYTES,
                    "autoloop catalogue exceeds source limit"
                );
                ensure!(
                    sources.len() < MAX_AUTOLOOPS,
                    "too many autoloops in catalogue"
                );
                ensure!(
                    sources.insert((bank.clone(), name), source).is_none(),
                    "duplicate autoloop identity"
                );
            }
        }
        ensure!(
            !sources.is_empty(),
            "autoloop catalogue contains no Lua autoloops"
        );
        let identities = sources
            .keys()
            .map(|(bank, autoloop)| {
                (
                    format!("{bank}/{autoloop}"),
                    (bank.clone(), autoloop.clone()),
                )
            })
            .collect();
        let mut intensity_htp = BTreeSet::new();
        for ((bank, autoloop), source) in &sources {
            // Base loops never compose as overlays, so only overlay banks declare it.
            let htp = OVERLAY_BANKS.contains(&bank.as_str())
                && overlay_htp(source)
                    .with_context(|| format!("invalid overlay header in {bank}/{autoloop}.lua"))?;
            if htp {
                intensity_htp.insert((bank.clone(), autoloop.clone()));
            }
        }
        Ok(Self {
            sources,
            identities,
            intensity_htp,
        })
    }

    pub fn from_definitions(scripts: &[wire::AutoloopDefinition]) -> Result<Self> {
        ensure!(
            !scripts.is_empty() && scripts.len() <= MAX_AUTOLOOPS,
            "catalogue must contain 1..{MAX_AUTOLOOPS} scripts"
        );
        let mut sources = BTreeMap::new();
        let mut identities = BTreeMap::new();
        let mut intensity_htp = BTreeSet::new();
        let mut source_bytes = 0;
        for script in scripts {
            validate_identifier(&script.bank)?;
            ensure!(
                !script.autoloop_id.is_empty()
                    && script.autoloop_id.len() <= MAX_AUTOLOOP_ID_BYTES
                    && script.autoloop_id.trim() == script.autoloop_id
                    && !script.autoloop_id.chars().any(char::is_control),
                "autoloop ID must contain 1..{MAX_AUTOLOOP_ID_BYTES} non-control bytes without surrounding whitespace"
            );
            ensure!(
                script.lua_source.len() <= MAX_SOURCE_BYTES,
                "autoloop {} exceeds source limit",
                script.autoloop_id
            );
            source_bytes += script.lua_source.len();
            ensure!(
                source_bytes <= MAX_CATALOGUE_BYTES,
                "autoloop catalogue exceeds source limit"
            );
            let identity = (script.bank.clone(), script.autoloop_id.clone());
            ensure!(
                identities
                    .insert(script.autoloop_id.clone(), identity.clone())
                    .is_none(),
                "duplicate autoloop ID {}",
                script.autoloop_id
            );
            if script.intensity_htp {
                intensity_htp.insert(identity.clone());
            }
            sources.insert(identity, script.lua_source.clone());
        }
        Ok(Self {
            sources,
            identities,
            intensity_htp,
        })
    }

    pub fn intensity_htp(&self, bank: &str, autoloop: &str) -> bool {
        self.intensity_htp
            .contains(&(bank.to_owned(), autoloop.to_owned()))
    }

    /// A script that fails to compile is kept as its error, reported when a command names it.
    pub fn compile(&self, rig: &FixtureRig, palettes: &PaletteTable) -> CompiledScripts {
        CompiledScripts {
            scripts: self
                .sources
                .iter()
                .map(|((bank, autoloop), source)| {
                    let script = LuaAutoloop::new(bank, autoloop, source, rig, palettes)
                        .map(|script| Rc::new(RefCell::new(script)))
                        .map_err(|error| format!("{error:#}"));
                    ((bank.clone(), autoloop.clone()), script)
                })
                .collect(),
        }
    }

    pub fn resolve(&self, autoloop_id: &str) -> Result<&(String, String)> {
        self.identities
            .get(autoloop_id)
            .with_context(|| format!("unknown autoloop ID {autoloop_id}"))
    }

    pub fn list(&self) -> Vec<AutoloopInfo> {
        self.sources
            .keys()
            .map(|(bank, autoloop)| AutoloopInfo {
                bank: bank.clone(),
                autoloop: autoloop.clone(),
            })
            .collect()
    }

    pub fn groups_missing_from(&self, rig: &FixtureRig) -> BTreeSet<String> {
        self.sources
            .values()
            .flat_map(|source| referenced_groups(source))
            .filter(|group| !rig.groups().contains_key(group))
            .collect()
    }

    pub fn instantiate(
        &self,
        bank: &str,
        autoloop: &str,
        rig: &FixtureRig,
        palettes: &PaletteTable,
    ) -> Result<LuaAutoloop> {
        let source = self
            .sources
            .get(&(bank.to_owned(), autoloop.to_owned()))
            .with_context(|| format!("unknown autoloop {bank}/{autoloop}"))?;
        LuaAutoloop::new(bank, autoloop, source, rig, palettes)
    }
}

pub fn missing_groups_warning(missing: &BTreeSet<String>) -> Option<String> {
    (!missing.is_empty()).then(|| {
        format!(
            "groups not in this rig: {}",
            missing.iter().cloned().collect::<Vec<_>>().join(", ")
        )
    })
}

fn referenced_groups(source: &str) -> impl Iterator<Item = String> + '_ {
    source.split("sf.group(").skip(1).filter_map(|call| {
        let call = call.trim_start();
        let quote = call.chars().next().filter(|c| matches!(c, '"' | '\''))?;
        let name = &call[1..];
        Some(name[..name.find(quote)?].to_owned())
    })
}

fn validate_identifier(value: &str) -> Result<()> {
    ensure!(
        !value.is_empty() && value.len() <= MAX_IDENTIFIER_BYTES,
        "autoloop and bank names must contain 1..{MAX_IDENTIFIER_BYTES} characters"
    );
    ensure!(
        value.bytes().all(|byte| byte.is_ascii_lowercase()
            || byte.is_ascii_digit()
            || byte == b'_'
            || byte == b'-'),
        "autoloop and bank names use lowercase letters, digits, underscores or hyphens"
    );
    Ok(())
}

pub struct LuaAutoloop {
    lua: Lua,
    render: Function,
    use_render_intensity: Function,
    instruction_budget: Arc<AtomicU32>,
    rig: Rc<FixtureRig>,
    identity: String,
    active_palette_slots: Vec<Table>,
}

impl LuaAutoloop {
    fn new(
        bank: &str,
        autoloop: &str,
        source: &str,
        rig: &FixtureRig,
        palettes: &PaletteTable,
    ) -> Result<Self> {
        ensure!(
            rig.cell_ids().len() <= MAX_CELLS,
            "Lua cell target limit exceeded"
        );
        let lua = Lua::new_with(
            StdLib::MATH | StdLib::TABLE | StdLib::STRING,
            LuaOptions::default(),
        )?;
        lua.set_memory_limit(LUA_MEMORY_LIMIT_BYTES)?;
        let instruction_budget = Arc::new(AtomicU32::new(LUA_INSTRUCTION_LIMIT));
        let budget = instruction_budget.clone();
        lua.set_hook(
            HookTriggers::new().every_nth_instruction(HOOK_INSTRUCTION_INTERVAL),
            move |_, _| {
                let remaining = budget.try_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
                    value.checked_sub(HOOK_INSTRUCTION_INTERVAL)
                });
                if remaining.is_err() {
                    return Err(mlua::Error::RuntimeError(
                        "autoloop instruction budget exhausted".to_owned(),
                    ));
                }
                Ok(VmState::Continue)
            },
        )?;

        let globals = lua.globals();
        for name in [
            "collectgarbage",
            "dofile",
            "load",
            "loadfile",
            "require",
            "print",
            "tostring",
            "warn",
            "rawset",
            "rawget",
            "rawequal",
            "getmetatable",
            "setmetatable",
            "pairs",
            "next",
            "pcall",
            "xpcall",
            "io",
            "os",
            "debug",
            "package",
            "coroutine",
        ] {
            globals.set(name, Value::Nil)?;
        }
        let math: Table = globals.get("math")?;
        math.set("random", Value::Nil)?;
        math.set("randomseed", Value::Nil)?;
        let string: Table = globals.get("string")?;
        // Native pattern backtracking cannot be interrupted by Lua instruction hooks.
        for name in ["dump", "format", "find", "match", "gmatch", "gsub"] {
            string.set(name, Value::Nil)?;
        }

        let (api, named_palettes, use_active_palette, use_render_intensity): (
            Table,
            Table,
            Function,
            Function,
        ) = lua
            .load(LIGHTING_API)
            .set_name("starfire lighting API")
            .set_mode(ChunkMode::Text)
            .eval()?;
        let colour_table = |colour: &Rgb| -> Result<Table> {
            lua.to_value(colour)?
                .as_table()
                .cloned()
                .context("palette slot is a table")
        };
        // One white table for every palette, reset each frame like the active palette's slots.
        let white = colour_table(&WHITE)?;
        for (name, slots) in palettes.iter() {
            let table = colour_table_of(&lua, slots)?;
            table.set(WHITE_SLOT, &white)?;
            named_palettes.set(
                name,
                read_only(&lua, Value::Table(table), "palettes are read-only")?,
            )?;
        }
        let initial_slots = palettes.slots(palettes.initial())?;
        let active_palette = lua.create_table()?;
        let mut active_palette_slots = Vec::new();
        for (slot, colour) in initial_slots.named_slots() {
            let table = colour_table(&colour)?;
            active_palette.set(slot, &table)?;
            active_palette_slots.push(table);
        }
        active_palette.set(WHITE_SLOT, &white)?;
        active_palette_slots.push(white);
        use_active_palette.call::<()>(read_only(
            &lua,
            Value::Table(active_palette),
            "palettes are read-only",
        )?)?;
        // The closures outlive this call and the rig is large: they and the autoloop share one copy.
        let shared = Rc::new(rig.clone());
        let targets = Rc::clone(&shared);
        api.set(
            "members",
            lua.create_function(move |lua, value: Value| {
                let target = targets.by_label(lua.from_value(value)?);
                if matches!(&target, Target::Group(group) if !targets.groups().contains_key(group))
                {
                    return lua.create_table();
                }
                let members = targets
                    .resolve_for_loop(&target)
                    .map_err(mlua::Error::external)?;
                if members.len() > MAX_CELLS {
                    return Err(mlua::Error::RuntimeError(
                        "cell target limit exceeded".to_owned(),
                    ));
                }
                lua.create_sequence_from(members)
            })?,
        )?;
        let spatial = Rc::clone(&shared);
        api.set(
            "mirror",
            lua.create_function(move |lua, value: Value| {
                let target = spatial.by_label(lua.from_value(value)?);
                let named: Vec<(String, String)> = match &target {
                    Target::Group(group) => {
                        spatial
                            .refuse_accent_group(group)
                            .map_err(mlua::Error::external)?;
                        spatial
                            .spatial()
                            .mirror(group)
                            .map(<[_]>::to_vec)
                            .unwrap_or_default()
                    }
                    // A fixture's cells share its position, so only their order can pair them.
                    _ => {
                        let members = spatial
                            .resolve_for_loop(&target)
                            .map_err(mlua::Error::external)?;
                        crate::spatial::rank_pairs(members.len())
                            .into_iter()
                            .map(|(left, right)| (members[left].clone(), members[right].clone()))
                            .collect()
                    }
                };
                let fixture = |id: &str| -> mlua::Result<Table> {
                    let table = lua.create_table()?;
                    table.set("fixture", id)?;
                    Ok(table)
                };
                lua.create_sequence_from(
                    named
                        .iter()
                        .map(|(left, right)| {
                            lua.create_sequence_from([fixture(left)?, fixture(right)?])
                        })
                        .collect::<mlua::Result<Vec<_>>>()?,
                )
            })?,
        )?;
        // MVR millimetres become metres here, at the Lua boundary only.
        let located = |rig: Rc<FixtureRig>| {
            move |value: &Value| -> mlua::Result<[f64; 3]> {
                let position = cell_fact(&rig, value, |id| rig.spatial().position(id))?;
                Ok(position.map(|mm| mm / MM_PER_METRE))
            }
        };
        let position = located(Rc::clone(&shared));
        api.set(
            "position",
            lua.create_function(move |_, cell: Value| {
                let [x, y, z] = position(&cell)?;
                Ok((x, y, z))
            })?,
        )?;
        let position = located(Rc::clone(&shared));
        api.set(
            "distance",
            lua.create_function(move |_, (cell, x, y): (Value, f64, f64)| {
                let [cell_x, cell_y, _] = position(&cell)?;
                Ok((cell_x - x).hypot(cell_y - y))
            })?,
        )?;
        let areas = Rc::clone(&shared);
        api.set(
            "zone",
            lua.create_function(move |_, cell: Value| {
                // Rig load refuses an unspecified area, so every known cell has a placement.
                cell_fact(&areas, &cell, |id| areas.spatial().area(id)?.placement())
            })?,
        )?;
        let fixtures = Rc::clone(&shared);
        api.set(
            "fixture_of",
            lua.create_function(move |_, cell: Value| {
                cell_fact(&fixtures, &cell, |id| {
                    fixtures.uuid_of_cell(id).map(str::to_owned)
                })
            })?,
        )?;
        let aiming = Rc::clone(&shared);
        api.set(
            "aim",
            lua.create_function(move |_, (cell, x, y, z): (Value, f64, f64, f64)| {
                if ![x, y, z].iter().all(|value| value.is_finite()) {
                    return Err(mlua::Error::RuntimeError(
                        "aim point must be finite metres".to_owned(),
                    ));
                }
                let point = [x, y, z].map(|metres| metres * MM_PER_METRE);
                // Only cells aim, and one without a pan range aims nowhere: nil, nil, not an error.
                let aim = cell_fact(&aiming, &cell, |id| {
                    aiming.profile(id).map(|_| aiming.aim_at(id, point))
                })?;
                Ok(aim.unzip())
            })?,
        )?;
        let focusing = Rc::clone(&shared);
        api.set(
            "focus",
            lua.create_function(move |_, cell: Value| {
                let [x, y, z] = cell_fact(&focusing, &cell, |id| focusing.focus(id))?
                    .map(|mm| mm / MM_PER_METRE);
                Ok((x, y, z))
            })?,
        )?;
        let gobos = Rc::clone(&shared);
        api.set(
            "gobo",
            lua.create_function(move |lua, (value, slot): (Value, i64)| {
                let slot = u64::try_from(slot).map_err(|_| {
                    mlua::Error::RuntimeError("gobo slot must be 0 or more".to_owned())
                })?;
                let target = gobos.by_label(lua.from_value(value)?);
                if matches!(&target, Target::Group(group) if !gobos.groups().contains_key(group)) {
                    return lua.create_table();
                }
                let mut effects = Vec::new();
                for id in gobos
                    .resolve_for_loop(&target)
                    .map_err(mlua::Error::external)?
                {
                    let Some(name) = gobos
                        .profile(&id)
                        .and_then(|profile| profile.gobo_name(slot))
                    else {
                        continue;
                    };
                    let attributes = lua.create_table()?;
                    attributes.set(GOBO_ATTRIBUTE, name)?;
                    let values = lua.create_table()?;
                    values.set("attributes", attributes)?;
                    let member = lua.create_table()?;
                    member.set("fixture", id)?;
                    let effect = lua.create_table()?;
                    effect.set("target", member)?;
                    effect.set("values", values)?;
                    effects.push(effect);
                }
                lua.create_sequence_from(effects)
            })?,
        )?;
        globals.set("sf", api)?;
        let identity = format!("{bank}/{autoloop}");
        instruction_budget.store(LUA_INSTRUCTION_LIMIT, Ordering::Relaxed);
        let definition: Table = lua
            .load(source)
            .set_name(&identity)
            .set_mode(ChunkMode::Text)
            .eval()
            .with_context(|| format!("cannot load autoloop {identity}"))?;
        let render: Function = definition
            .get("render")
            .context("autoloop must return a table with a render function")?;
        freeze_globals(&lua)?;
        Ok(Self {
            lua,
            render,
            use_render_intensity,
            instruction_budget,
            rig: shared,
            identity,
            active_palette_slots,
        })
    }

    pub fn render(
        &mut self,
        context: &RenderContext,
        palette: &PaletteSlots,
    ) -> Result<Vec<Effect>> {
        ensure!(
            context
                .normalized_rms_level
                .is_none_or(|value| value.is_finite() && (0.0..=1.0).contains(&value)),
            "normalized RMS level must be finite and between zero and one"
        );
        ensure!(
            context.intensity.is_finite() && (0.0..=1.0).contains(&context.intensity),
            "intensity must be finite and between zero and one"
        );
        for value in [
            context.time_seconds,
            context.beat_position,
            context.bar_phase,
            context.autoloop_elapsed_beats,
            context.autoloop_speed_scaled_elapsed_beats,
            context.bpm,
        ]
        .into_iter()
        .chain(context.section_elapsed_beats)
        .chain(context.beats_until_next)
        .chain(context.beats_until_dip)
        .chain(context.next_known_at)
        .chain(context.overlay_length_beats)
        {
            ensure!(
                value.is_finite() && value >= 0.0,
                "render context must contain finite nonnegative time"
            );
        }
        for value in [
            context.drop_strength,
            context.next_drop_strength,
            context.onset,
            context.bass,
            context.mid,
            context.high,
        ]
        .into_iter()
        .flatten()
        {
            ensure!(
                value.is_finite() && (0.0..=1.0).contains(&value),
                "render context levels must be finite and between zero and one"
            );
        }
        let colours = palette.named_slots().map(|(_, colour)| colour);
        for (table, colour) in self
            .active_palette_slots
            .iter()
            .zip(colours.iter().chain([&WHITE]))
        {
            table.set("red", colour.red)?;
            table.set("green", colour.green)?;
            table.set("blue", colour.blue)?;
        }
        self.instruction_budget
            .store(LUA_INSTRUCTION_LIMIT, Ordering::Relaxed);
        self.use_render_intensity
            .call::<()>((context.intensity, intensity::pulse_depth(context.intensity)))
            .context("set render intensity")?;
        let values = self.lua.to_value_with(
            context,
            SerializeOptions::new().serialize_none_to_null(false),
        )?;
        let snapshot = read_only(&self.lua, values, "render context is read-only")?;

        let result: Table = self
            .render
            .call(snapshot)
            .with_context(|| format!("autoloop {} failed", self.identity))?;
        let length = result.raw_len();
        ensure!(
            length <= MAX_EFFECTS_PER_FRAME,
            "autoloop returned more than {MAX_EFFECTS_PER_FRAME} effects"
        );
        let mut entries = 0;
        for entry in result.clone().pairs::<Value, Value>() {
            let (key, _) = entry?;
            ensure!(
                matches!(key, Value::Integer(index) if index >= 1 && index <= length as i64),
                "autoloop must return a dense effect array"
            );
            entries += 1;
            ensure!(
                entries <= MAX_EFFECTS_PER_FRAME,
                "autoloop effect limit exceeded"
            );
        }
        ensure!(
            entries == length,
            "autoloop must return a dense effect array"
        );
        let mut effects: Vec<Effect> = self.lua.from_value(Value::Table(result))?;
        for effect in &mut effects {
            effect.target = self
                .rig
                .by_label(std::mem::replace(&mut effect.target, Target::All));
        }
        effects.retain(|effect| match &effect.target {
            Target::Group(group) => self.rig.groups().contains_key(group),
            _ => true,
        });
        for effect in &effects {
            let cells = self
                .rig
                .resolve_for_loop(&effect.target)
                .with_context(|| format!("autoloop {} failed", self.identity))?;
            for id in cells {
                self.rig
                    .validate_attributes(&id, &effect.values.attributes)?;
            }
            validate_properties(&effect.values)?;
        }
        Ok(effects)
    }
}

/// A cell (or fixture) name, given bare or as the `sf.fixture` target `sf.each` hands out.
fn cell_name(value: &Value) -> mlua::Result<String> {
    match value {
        Value::String(id) => Ok(id.to_str()?.to_owned()),
        Value::Table(target) => target.get::<String>("fixture"),
        _ => Err(mlua::Error::RuntimeError(
            "expected a cell name or sf.fixture target".to_owned(),
        )),
    }
}

/// What the rig knows about the cell Lua names (by ID, or a fixture by label), which must exist.
fn cell_fact<T>(
    rig: &FixtureRig,
    value: &Value,
    fact: impl FnOnce(&str) -> Option<T>,
) -> mlua::Result<T> {
    let name = cell_name(value)?;
    let id = rig.uuid_of_label(&name).unwrap_or(&name);
    fact(id).ok_or_else(|| mlua::Error::RuntimeError(format!("unknown cell {name}")))
}

/// One VM serves every layer naming the loop and outlives reselection, so a render must not
/// keep state in globals: they move behind the read-only metatable once the chunk has loaded.
fn freeze_globals(lua: &Lua) -> mlua::Result<()> {
    let globals = lua.globals();
    let backing = lua.create_table()?;
    let mut names = Vec::new();
    for pair in globals.pairs::<Value, Value>() {
        let (name, value) = pair?;
        backing.raw_set(name.clone(), value)?;
        names.push(name);
    }
    for name in names {
        globals.raw_set(name, Value::Nil)?;
    }
    let frozen = read_only(
        lua,
        Value::Table(backing),
        "globals are read-only after load",
    )?;
    globals.set_metatable(frozen.metatable())?;
    Ok(())
}

fn colour_table_of(lua: &Lua, slots: &PaletteSlots) -> Result<Table> {
    lua.to_value(slots)?
        .as_table()
        .cloned()
        .context("palette is a table")
}

fn read_only(lua: &Lua, value: Value, error: &'static str) -> mlua::Result<Table> {
    let metatable = lua.create_table()?;
    metatable.set("__index", value)?;
    metatable.set("__metatable", false)?;
    metatable.set(
        "__newindex",
        lua.create_function(move |_, _: (Value, Value, Value)| -> mlua::Result<()> {
            Err(mlua::Error::RuntimeError(error.to_owned()))
        })?,
    )?;
    let proxy = lua.create_table()?;
    proxy.set_metatable(Some(metatable))?;
    Ok(proxy)
}

fn validate_properties(values: &PropertyValues) -> Result<()> {
    for (name, value) in [
        ("intensity", values.intensity),
        ("pan", values.pan),
        ("tilt", values.tilt),
    ] {
        if let Some(value) = value {
            ensure!(
                value.is_finite() && (0.0..=1.0).contains(&value),
                "{name} must be finite and within 0..1"
            );
        }
    }
    if let Some(colour) = values.colour {
        for value in [colour.red, colour.green, colour.blue] {
            ensure!(
                value.is_finite() && (0.0..=1.0).contains(&value),
                "colour components must be finite and within 0..1"
            );
        }
    }
    if let Some(rate) = values.strobe_hz {
        ensure!(
            rate.is_finite() && (0.0..=MAX_STROBE_HZ).contains(&rate),
            "strobe rate must be finite and within 0..{MAX_STROBE_HZ} Hz"
        );
    }
    if values == &PropertyValues::default() {
        bail!("effect must set at least one property");
    }
    Ok(())
}

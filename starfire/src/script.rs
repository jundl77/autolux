//! Bounded Lua autoloops operating on musical time and fixture properties.

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
use crate::intensity;
use crate::model::{Effect, MAX_STROBE_HZ, PropertyValues, RenderContext, Target};
use crate::palette::{PaletteSlots, PaletteTable};
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
        Ok(Self {
            sources,
            identities,
        })
    }

    pub fn from_definitions(scripts: &[wire::AutoloopDefinition]) -> Result<Self> {
        ensure!(
            !scripts.is_empty() && scripts.len() <= MAX_AUTOLOOPS,
            "catalogue must contain 1..{MAX_AUTOLOOPS} scripts"
        );
        let mut sources = BTreeMap::new();
        let mut identities = BTreeMap::new();
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
            sources.insert(identity, script.lua_source.clone());
        }
        Ok(Self {
            sources,
            identities,
        })
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

/// Checked wherever a loop resolves a target, so no way of naming an accent group escapes it.
fn refuse_accent(rig: &FixtureRig, target: &Target) -> Result<()> {
    match target {
        Target::Group(group) if rig.is_accent(group) => {
            bail!("accent group {group} plays specials only, never autoloops")
        }
        _ => Ok(()),
    }
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
    rig: FixtureRig,
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
        for (name, slots) in palettes.iter() {
            named_palettes.set(
                name,
                read_only(&lua, lua.to_value(slots)?, "palettes are read-only")?,
            )?;
        }
        let initial_slots = palettes.slots(palettes.initial())?;
        let active_palette = lua.create_table()?;
        let mut active_palette_slots = Vec::new();
        for (slot, colour) in initial_slots.named_slots() {
            let table: Table = lua
                .to_value(&colour)?
                .as_table()
                .cloned()
                .context("palette slot is a table")?;
            active_palette.set(slot, &table)?;
            active_palette_slots.push(table);
        }
        use_active_palette.call::<()>(read_only(
            &lua,
            Value::Table(active_palette),
            "palettes are read-only",
        )?)?;
        // The closures outlive this call and the rig is large: share one copy, not one each.
        let shared = Rc::new(rig.clone());
        let targets = Rc::clone(&shared);
        api.set(
            "members",
            lua.create_function(move |lua, value: Value| {
                let target: Target = lua.from_value(value)?;
                refuse_accent(&targets, &target).map_err(mlua::Error::external)?;
                if matches!(&target, Target::Group(group) if !targets.groups().contains_key(group))
                {
                    return lua.create_table();
                }
                let members = targets.resolve(&target).map_err(mlua::Error::external)?;
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
                let target: Target = lua.from_value(value)?;
                refuse_accent(&spatial, &target).map_err(mlua::Error::external)?;
                let named: Vec<(String, String)> = match &target {
                    Target::Group(group) => spatial
                        .spatial()
                        .mirror(group)
                        .map(<[_]>::to_vec)
                        .unwrap_or_default(),
                    // A fixture's cells share its position, so only their order can pair them.
                    _ => {
                        let members = spatial.resolve(&target).map_err(mlua::Error::external)?;
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
                let id = cell_name(value)?;
                let position = rig
                    .spatial()
                    .position(&id)
                    .ok_or_else(|| mlua::Error::RuntimeError(format!("unknown cell {id}")))?;
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
        let areas = shared;
        api.set(
            "zone",
            lua.create_function(move |_, cell: Value| {
                let id = cell_name(&cell)?;
                let area = areas
                    .spatial()
                    .area(&id)
                    .ok_or_else(|| mlua::Error::RuntimeError(format!("unknown cell {id}")))?;
                area.placement().ok_or_else(|| {
                    mlua::Error::RuntimeError(format!("cell {id} has no stage area"))
                })
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
        Ok(Self {
            lua,
            render,
            use_render_intensity,
            instruction_budget,
            rig: rig.clone(),
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
        ] {
            ensure!(
                value.is_finite() && value >= 0.0,
                "render context must contain finite nonnegative time"
            );
        }
        for (table, (_, colour)) in self.active_palette_slots.iter().zip(palette.named_slots()) {
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
        effects.retain(|effect| match &effect.target {
            Target::Group(group) => self.rig.groups().contains_key(group),
            _ => true,
        });
        for effect in &effects {
            refuse_accent(&self.rig, &effect.target)
                .with_context(|| format!("autoloop {} failed", self.identity))?;
            for id in self.rig.resolve(&effect.target)? {
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

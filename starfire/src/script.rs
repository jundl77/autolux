//! Bounded Lua autoloops operating on musical time and fixture properties.

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::Read;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

use anyhow::{Context, Result, bail, ensure};
use mlua::chunk::ChunkMode;
use mlua::serde::SerializeOptions;
use mlua::{Function, HookTriggers, Lua, LuaOptions, LuaSerdeExt, StdLib, Table, Value, VmState};
use serde::Serialize;

use crate::fixtures::{FixtureRig, MAX_FIXTURES};
use crate::model::{Effect, MAX_STROBE_HZ, PropertyValues, RenderContext, Target};
use crate::protocol::wire;

const MAX_SOURCE_BYTES: usize = 128 * 1024;
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

    pub fn instantiate(&self, bank: &str, autoloop: &str, rig: &FixtureRig) -> Result<LuaAutoloop> {
        let source = self
            .sources
            .get(&(bank.to_owned(), autoloop.to_owned()))
            .with_context(|| format!("unknown autoloop {bank}/{autoloop}"))?;
        LuaAutoloop::new(bank, autoloop, source, rig)
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
    instruction_budget: Arc<AtomicU32>,
    rig: FixtureRig,
    identity: String,
}

impl LuaAutoloop {
    fn new(bank: &str, autoloop: &str, source: &str, rig: &FixtureRig) -> Result<Self> {
        ensure!(
            rig.fixture_ids().len() <= MAX_FIXTURES,
            "Lua fixture target limit exceeded"
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
        string.set("dump", Value::Nil)?;
        string.set("format", Value::Nil)?;

        let api: Table = lua
            .load(LIGHTING_API)
            .set_name("starfire lighting API")
            .set_mode(ChunkMode::Text)
            .eval()?;
        let targets = rig.clone();
        api.set(
            "members",
            lua.create_function(move |lua, value: Value| {
                let target: Target = lua.from_value(value)?;
                let members = targets.resolve(&target).map_err(mlua::Error::external)?;
                if members.len() > MAX_FIXTURES {
                    return Err(mlua::Error::RuntimeError(
                        "fixture target limit exceeded".to_owned(),
                    ));
                }
                lua.create_sequence_from(members)
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
            instruction_budget,
            rig: rig.clone(),
            identity,
        })
    }

    pub fn render(&mut self, context: &RenderContext) -> Result<Vec<Effect>> {
        ensure!(
            context
                .normalized_rms_level
                .is_none_or(|value| value.is_finite() && (0.0..=1.0).contains(&value)),
            "normalized RMS level must be finite and between zero and one"
        );
        for value in [
            context.time_seconds,
            context.beat_position,
            context.autoloop_elapsed_beats,
            context.autoloop_speed_scaled_elapsed_beats,
        ] {
            ensure!(
                value.is_finite() && value >= 0.0,
                "render context must contain finite nonnegative time"
            );
        }
        self.instruction_budget
            .store(LUA_INSTRUCTION_LIMIT, Ordering::Relaxed);
        let values = self.lua.to_value_with(
            context,
            SerializeOptions::new().serialize_none_to_null(false),
        )?;
        let metatable = self.lua.create_table()?;
        metatable.set("__index", values)?;
        metatable.set("__metatable", false)?;
        metatable.set(
            "__newindex",
            self.lua
                .create_function(|_, _: (Value, Value, Value)| -> mlua::Result<()> {
                    Err(mlua::Error::RuntimeError(
                        "render context is read-only".to_owned(),
                    ))
                })?,
        )?;
        let snapshot = self.lua.create_table()?;
        snapshot.set_metatable(Some(metatable))?;

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
        let effects: Vec<Effect> = self.lua.from_value(Value::Table(result))?;
        for effect in &effects {
            for id in self.rig.resolve(&effect.target)? {
                self.rig
                    .validate_attributes(&id, &effect.values.attributes)?;
            }
            validate_properties(&effect.values)?;
        }
        Ok(effects)
    }
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

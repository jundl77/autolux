//! Bounded Lua sequences operating on musical time and fixture properties.

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::Read;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

use anyhow::{Context, Result, bail, ensure};
use mlua::chunk::ChunkMode;
use mlua::{Function, HookTriggers, Lua, LuaOptions, LuaSerdeExt, StdLib, Table, Value, VmState};
use serde::Serialize;

use crate::fixtures::{FixtureRig, MAX_FIXTURES};
use crate::model::{Effect, MAX_STROBE_HZ, PropertyValues, RenderContext, Target};

const MAX_SOURCE_BYTES: usize = 128 * 1024;
const MAX_CATALOGUE_BYTES: usize = 4 * 1024 * 1024;
const MAX_SEQUENCES: usize = 256;
const MAX_IDENTIFIER_BYTES: usize = 64;
const LUA_MEMORY_LIMIT_BYTES: usize = 16 * 1024 * 1024;
const LUA_INSTRUCTION_LIMIT: u32 = 100_000;
const HOOK_INSTRUCTION_INTERVAL: u32 = 100;
const MAX_EFFECTS_PER_FRAME: usize = 512;
const LIGHTING_API: &str = include_str!("lua/lighting.lua");

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct SequenceInfo {
    pub bank: String,
    pub sequence: String,
}

#[derive(Clone, Debug)]
pub struct SequenceCatalog {
    sources: BTreeMap<(String, String), String>,
}

impl SequenceCatalog {
    pub fn load(root: &Path) -> Result<Self> {
        let root = root
            .canonicalize()
            .context("cannot open sequence directory")?;
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
            for sequence in fs::read_dir(entry.path())? {
                let sequence = sequence?;
                let path = sequence.path();
                if path.extension().and_then(|value| value.to_str()) != Some("lua") {
                    continue;
                }
                ensure!(
                    sequence.file_type()?.is_file(),
                    "sequence must be a regular file"
                );
                ensure!(
                    path.canonicalize()?.starts_with(&root),
                    "sequence escapes catalogue directory"
                );
                let name = path
                    .file_stem()
                    .and_then(|value| value.to_str())
                    .context("sequence name is not UTF-8")?
                    .to_owned();
                validate_identifier(&name)?;
                let mut source = String::new();
                File::open(&path)?
                    .take((MAX_SOURCE_BYTES + 1) as u64)
                    .read_to_string(&mut source)?;
                ensure!(
                    source.len() <= MAX_SOURCE_BYTES,
                    "sequence {bank}/{name} exceeds source limit"
                );
                source_bytes += source.len();
                ensure!(
                    source_bytes <= MAX_CATALOGUE_BYTES,
                    "sequence catalogue exceeds source limit"
                );
                ensure!(
                    sources.len() < MAX_SEQUENCES,
                    "too many sequences in catalogue"
                );
                ensure!(
                    sources.insert((bank.clone(), name), source).is_none(),
                    "duplicate sequence identity"
                );
            }
        }
        ensure!(
            !sources.is_empty(),
            "sequence catalogue contains no Lua sequences"
        );
        Ok(Self { sources })
    }

    pub fn list(&self) -> Vec<SequenceInfo> {
        self.sources
            .keys()
            .map(|(bank, sequence)| SequenceInfo {
                bank: bank.clone(),
                sequence: sequence.clone(),
            })
            .collect()
    }

    pub fn instantiate(&self, bank: &str, sequence: &str, rig: &FixtureRig) -> Result<LuaSequence> {
        let source = self
            .sources
            .get(&(bank.to_owned(), sequence.to_owned()))
            .with_context(|| format!("unknown sequence {bank}/{sequence}"))?;
        LuaSequence::new(bank, sequence, source, rig)
    }
}

fn validate_identifier(value: &str) -> Result<()> {
    ensure!(
        !value.is_empty() && value.len() <= MAX_IDENTIFIER_BYTES,
        "sequence and bank names must contain 1..{MAX_IDENTIFIER_BYTES} characters"
    );
    ensure!(
        value.bytes().all(|byte| byte.is_ascii_lowercase()
            || byte.is_ascii_digit()
            || byte == b'_'
            || byte == b'-'),
        "sequence and bank names use lowercase letters, digits, underscores or hyphens"
    );
    Ok(())
}

pub struct LuaSequence {
    lua: Lua,
    render: Function,
    instruction_budget: Arc<AtomicU32>,
    rig: FixtureRig,
    identity: String,
}

impl LuaSequence {
    fn new(bank: &str, sequence: &str, source: &str, rig: &FixtureRig) -> Result<Self> {
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
                        "sequence instruction budget exhausted".to_owned(),
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
        let identity = format!("{bank}/{sequence}");
        instruction_budget.store(LUA_INSTRUCTION_LIMIT, Ordering::Relaxed);
        let definition: Table = lua
            .load(source)
            .set_name(&identity)
            .set_mode(ChunkMode::Text)
            .eval()
            .with_context(|| format!("cannot load sequence {identity}"))?;
        let render: Function = definition
            .get("render")
            .context("sequence must return a table with a render function")?;
        Ok(Self {
            lua,
            render,
            instruction_budget,
            rig: rig.clone(),
            identity,
        })
    }

    pub fn render(&mut self, context: &RenderContext) -> Result<Vec<Effect>> {
        for value in [
            context.time_seconds,
            context.beat_position,
            context.sequence_elapsed_beats,
            context.sequence_speed_scaled_elapsed_beats,
        ] {
            ensure!(
                value.is_finite() && value >= 0.0,
                "render context must contain finite nonnegative time"
            );
        }
        self.instruction_budget
            .store(LUA_INSTRUCTION_LIMIT, Ordering::Relaxed);
        let values = self.lua.to_value(context)?;
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
            .with_context(|| format!("sequence {} failed", self.identity))?;
        let length = result.raw_len();
        ensure!(
            length <= MAX_EFFECTS_PER_FRAME,
            "sequence returned more than {MAX_EFFECTS_PER_FRAME} effects"
        );
        let mut entries = 0;
        for entry in result.clone().pairs::<Value, Value>() {
            let (key, _) = entry?;
            ensure!(
                matches!(key, Value::Integer(index) if index >= 1 && index <= length as i64),
                "sequence must return a dense effect array"
            );
            entries += 1;
            ensure!(
                entries <= MAX_EFFECTS_PER_FRAME,
                "sequence effect limit exceeded"
            );
        }
        ensure!(
            entries == length,
            "sequence must return a dense effect array"
        );
        let effects: Vec<Effect> = self.lua.from_value(Value::Table(result))?;
        for effect in &effects {
            self.rig.resolve(&effect.target)?;
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

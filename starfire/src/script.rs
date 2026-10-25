//! Bounded Lua sequences operating on musical time and fixture properties.

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
use prost::Message;
use serde::Serialize;

use crate::fixtures::{FixtureRig, MAX_FIXTURES};
use crate::model::{Effect, MAX_STROBE_HZ, PropertyValues, RenderContext, Target};
use crate::parameters::{MAX_ALLOWED_VALUES, MAX_PARAMETERS, validate_definition, validate_value};
use crate::protocol::{MAX_ERROR_BYTES, MAX_MESSAGE_BYTES, wire};

const MAX_SOURCE_BYTES: usize = 128 * 1024;
const MAX_CATALOGUE_BYTES: usize = 4 * 1024 * 1024;
const MAX_SEQUENCES: usize = 256;
const MAX_IDENTIFIER_BYTES: usize = 64;
const LUA_MEMORY_LIMIT_BYTES: usize = 16 * 1024 * 1024;
const LUA_INSTRUCTION_LIMIT: u32 = 100_000;
const HOOK_INSTRUCTION_INTERVAL: u32 = 100;
const MAX_EFFECTS_PER_FRAME: usize = 512;
const LIGHTING_API: &str = include_str!("lua/lighting.lua");
const ERROR_FIELD_OVERHEAD_BYTES: usize = 8;

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
    parameter_definitions: Vec<wire::ParameterDefinition>,
    parameter_values: BTreeMap<String, wire::ParameterValue>,
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
        let parameter_definitions = read_parameters(definition.get("parameters")?)?;
        let catalog = wire::RendererEvent {
            command_id: u64::MAX,
            event: Some(wire::renderer_event::Event::PlaySequenceReply(
                wire::PlaySequenceReply {
                    sequence: Some(wire::SequenceReference {
                        bank_id: bank.into(),
                        sequence_id: sequence.into(),
                    }),
                    parameters: parameter_definitions.clone(),
                    error: String::new(),
                },
            )),
        };
        ensure!(
            catalog.encoded_len() + MAX_ERROR_BYTES + ERROR_FIELD_OVERHEAD_BYTES
                <= MAX_MESSAGE_BYTES,
            "sequence parameter catalog exceeds message limit"
        );
        let parameter_values = parameter_definitions
            .iter()
            .filter_map(|definition| {
                definition
                    .default_value
                    .clone()
                    .map(|value| (definition.name.clone(), value))
            })
            .collect();
        Ok(Self {
            lua,
            render,
            instruction_budget,
            rig: rig.clone(),
            identity,
            parameter_definitions,
            parameter_values,
        })
    }

    pub fn parameter_definitions(&self) -> &[wire::ParameterDefinition] {
        &self.parameter_definitions
    }

    pub fn set_parameter(&mut self, name: &str, value: &wire::ParameterValue) -> Result<()> {
        let definition = self
            .parameter_definitions
            .iter()
            .find(|definition| definition.name == name)
            .with_context(|| format!("unknown sequence parameter {name}"))?;
        validate_value(definition, value)?;
        self.parameter_values.insert(name.into(), value.clone());
        Ok(())
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
        let values = self.lua.to_value_with(
            context,
            SerializeOptions::new().serialize_none_to_null(false),
        )?;
        let parameters = self.lua.create_table()?;
        for (key, value) in &self.parameter_values {
            parameters.set(key.as_str(), parameter_to_lua(&self.lua, value)?)?;
        }
        values
            .as_table()
            .context("context must be a table")?
            .set("parameters", read_only_table(&self.lua, parameters)?)?;
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

fn read_parameters(parameters: Option<Table>) -> Result<Vec<wire::ParameterDefinition>> {
    let mut definitions = BTreeMap::new();
    if let Some(parameters) = parameters {
        for parameter in parameters.pairs::<String, Table>() {
            ensure!(
                definitions.len() < MAX_PARAMETERS,
                "too many sequence parameters"
            );
            let (key, declaration) = parameter?;
            for field in declaration.clone().pairs::<String, Value>() {
                let (field, _) = field?;
                ensure!(
                    matches!(
                        field.as_str(),
                        "value_type"
                            | "default_value"
                            | "unit"
                            | "minimum"
                            | "maximum"
                            | "allowed_values"
                    ),
                    "unknown parameter declaration field {field}"
                );
            }
            let value_type = match declaration.get::<String>("value_type")?.as_str() {
                "number" => wire::ValueType::Number,
                "integer" => wire::ValueType::Integer,
                "boolean" => wire::ValueType::Boolean,
                "text" => wire::ValueType::Text,
                "rgb" => wire::ValueType::Rgb,
                _ => bail!("unknown sequence parameter type"),
            };
            let mut definition = wire::ParameterDefinition {
                name: key.clone(),
                value_type: value_type as i32,
                default_value: Some(parameter_from_lua(
                    value_type,
                    declaration.get("default_value")?,
                )?),
                unit: declaration
                    .get::<Option<String>>("unit")?
                    .unwrap_or_default(),
                ..Default::default()
            };
            for (field, destination) in [
                ("minimum", &mut definition.minimum),
                ("maximum", &mut definition.maximum),
            ] {
                let value = declaration.get::<Value>(field)?;
                if value != Value::Nil {
                    *destination = Some(parameter_from_lua(value_type, value)?);
                }
            }
            if let Some(allowed) = declaration.get::<Option<Table>>("allowed_values")? {
                let length = allowed.raw_len();
                ensure!(
                    length <= MAX_ALLOWED_VALUES,
                    "too many allowed parameter values"
                );
                let mut entries = 0;
                for entry in allowed.clone().pairs::<Value, Value>() {
                    let (key, _) = entry?;
                    ensure!(
                        matches!(key, Value::Integer(index) if index >= 1 && index <= length as i64),
                        "allowed_values must be a dense array"
                    );
                    entries += 1;
                    ensure!(
                        entries <= MAX_ALLOWED_VALUES,
                        "too many allowed parameter values"
                    );
                }
                ensure!(entries == length, "allowed_values must be a dense array");
                for value in allowed.sequence_values::<Value>() {
                    ensure!(
                        definition.allowed_values.len() < MAX_ALLOWED_VALUES,
                        "too many allowed parameter values"
                    );
                    definition
                        .allowed_values
                        .push(parameter_from_lua(value_type, value?)?);
                }
            }
            validate_definition(&definition)?;
            definitions.insert(key, definition);
        }
    }
    Ok(definitions.into_values().collect())
}

fn parameter_from_lua(value_type: wire::ValueType, value: Value) -> Result<wire::ParameterValue> {
    use wire::parameter_value::Value as WireValue;
    let value = match (value_type, value) {
        (wire::ValueType::Number, Value::Number(value)) => WireValue::Number(value),
        (wire::ValueType::Number, Value::Integer(value)) => WireValue::Number(value as f64),
        (wire::ValueType::Integer, Value::Integer(value)) => WireValue::Integer(value),
        (wire::ValueType::Boolean, Value::Boolean(value)) => WireValue::Boolean(value),
        (wire::ValueType::Text, Value::String(value)) => {
            WireValue::Text(value.to_str()?.to_owned())
        }
        (wire::ValueType::Rgb, Value::Table(value)) => WireValue::Rgb(wire::RgbColor {
            red: value.get("red")?,
            green: value.get("green")?,
            blue: value.get("blue")?,
        }),
        _ => bail!("sequence parameter declaration has the wrong value type"),
    };
    Ok(wire::ParameterValue { value: Some(value) })
}

fn parameter_to_lua(lua: &Lua, value: &wire::ParameterValue) -> Result<Value> {
    use wire::parameter_value::Value as WireValue;
    Ok(
        match value
            .value
            .as_ref()
            .context("parameter value is required")?
        {
            WireValue::Number(value) => Value::Number(*value),
            WireValue::Integer(value) => Value::Integer(*value),
            WireValue::Boolean(value) => Value::Boolean(*value),
            WireValue::Text(value) => Value::String(lua.create_string(value)?),
            WireValue::Rgb(value) => {
                let table = lua.create_table()?;
                table.set("red", value.red)?;
                table.set("green", value.green)?;
                table.set("blue", value.blue)?;
                Value::Table(read_only_table(lua, table)?)
            }
        },
    )
}

fn read_only_table(lua: &Lua, values: Table) -> Result<Table> {
    let metatable = lua.create_table()?;
    metatable.set("__index", values)?;
    metatable.set("__metatable", false)?;
    metatable.set(
        "__newindex",
        lua.create_function(|_, _: (Value, Value, Value)| -> mlua::Result<()> {
            Err(mlua::Error::RuntimeError(
                "render parameters are read-only".into(),
            ))
        })?,
    )?;
    let snapshot = lua.create_table()?;
    snapshot.set_metatable(Some(metatable))?;
    Ok(snapshot)
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

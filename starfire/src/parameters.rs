//! Registered typed controls shared by engine settings and Lua sequences.

use anyhow::{Context, Result, bail, ensure};

use crate::protocol::wire::{self, parameter_value::Value};

pub const MAX_PARAMETERS: usize = 64;
pub const MAX_PARAMETER_NAME_BYTES: usize = 128;
pub const MAX_TEXT_BYTES: usize = 256;
pub const MAX_ALLOWED_VALUES: usize = 32;

pub fn number(value: f64) -> wire::ParameterValue {
    wire::ParameterValue {
        value: Some(Value::Number(value)),
    }
}

pub fn number_definition(
    name: &str,
    default: Option<f64>,
    minimum: f64,
    maximum: f64,
) -> wire::ParameterDefinition {
    wire::ParameterDefinition {
        name: name.into(),
        value_type: wire::ValueType::Number as i32,
        default_value: default.map(number),
        minimum: Some(number(minimum)),
        maximum: Some(number(maximum)),
        ..Default::default()
    }
}

pub fn validate_definition(definition: &wire::ParameterDefinition) -> Result<()> {
    ensure!(
        !definition.name.trim().is_empty() && definition.name.len() <= MAX_PARAMETER_NAME_BYTES,
        "invalid parameter name"
    );
    ensure!(
        definition.unit.len() <= MAX_TEXT_BYTES,
        "parameter unit exceeds byte limit"
    );
    ensure!(
        definition.allowed_values.len() <= MAX_ALLOWED_VALUES,
        "too many allowed parameter values"
    );
    let value_type =
        wire::ValueType::try_from(definition.value_type).context("unknown parameter type")?;
    ensure!(
        value_type != wire::ValueType::Unspecified,
        "parameter type is required"
    );
    for bound in [&definition.minimum, &definition.maximum]
        .into_iter()
        .flatten()
    {
        validate_type(value_type, bound)?;
        ensure!(
            matches!(bound.value, Some(Value::Number(_) | Value::Integer(_))),
            "only numeric parameters have bounds"
        );
    }
    if let (Some(minimum), Some(maximum)) = (&definition.minimum, &definition.maximum) {
        ensure!(
            numeric_le(minimum, maximum),
            "parameter minimum exceeds maximum"
        );
    }
    for value in &definition.allowed_values {
        validate_value(definition, value)?;
    }
    if let Some(value) = &definition.default_value {
        validate_value(definition, value)?;
    }
    Ok(())
}

pub fn validate_value(
    definition: &wire::ParameterDefinition,
    value: &wire::ParameterValue,
) -> Result<()> {
    validate_type(
        wire::ValueType::try_from(definition.value_type).context("unknown parameter type")?,
        value,
    )?;
    if let Some(minimum) = &definition.minimum {
        ensure!(numeric_le(minimum, value), "parameter is below its minimum");
    }
    if let Some(maximum) = &definition.maximum {
        ensure!(numeric_le(value, maximum), "parameter is above its maximum");
    }
    ensure!(
        definition.allowed_values.is_empty() || definition.allowed_values.contains(value),
        "parameter is not an allowed value"
    );
    Ok(())
}

fn validate_type(value_type: wire::ValueType, value: &wire::ParameterValue) -> Result<()> {
    match (
        value_type,
        value
            .value
            .as_ref()
            .context("parameter value type is required")?,
    ) {
        (wire::ValueType::Number, Value::Number(value)) => {
            ensure!(value.is_finite(), "number must be finite")
        }
        (wire::ValueType::Integer, Value::Integer(_))
        | (wire::ValueType::Boolean, Value::Boolean(_)) => {}
        (wire::ValueType::Text, Value::Text(value)) => ensure!(
            value.len() <= MAX_TEXT_BYTES,
            "parameter text exceeds byte limit"
        ),
        (wire::ValueType::Rgb, Value::Rgb(value)) => ensure!(
            [value.red, value.green, value.blue]
                .into_iter()
                .all(|v| v.is_finite() && (0.0..=1.0).contains(&v)),
            "RGB components must be finite and in 0..1"
        ),
        _ => bail!("parameter value has the wrong type"),
    }
    Ok(())
}

fn numeric_le(left: &wire::ParameterValue, right: &wire::ParameterValue) -> bool {
    match (&left.value, &right.value) {
        (Some(Value::Number(left)), Some(Value::Number(right))) => left <= right,
        (Some(Value::Integer(left)), Some(Value::Integer(right))) => left <= right,
        _ => false,
    }
}

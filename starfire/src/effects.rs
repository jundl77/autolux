//! Typed fixture attributes composed one effect at a time in priority order.

use anyhow::{Context, Result, bail, ensure};

use crate::model::{FixtureState, LightingOverride, MAX_STROBE_HZ, OverrideLifetime, Rgb, Target};
use crate::parameters::{number, number_definition, validate_value};
use crate::protocol::wire::{self, parameter_value::Value};

pub const MAX_MULTIPLIER: f64 = 8.0;
pub const MAX_ATTRIBUTES: usize = 5;

pub fn fixture_target(selection: &wire::FixtureSelection) -> Result<Target> {
    Ok(
        match selection
            .selection
            .as_ref()
            .context("fixture selection is required")?
        {
            wire::fixture_selection::Selection::All(_) => Target::All,
            wire::fixture_selection::Selection::GroupId(id) => Target::Group(id.clone()),
            wire::fixture_selection::Selection::FixtureId(id) => Target::Fixture(id.clone()),
        },
    )
}

pub fn validate_attribute(attribute: &wire::AttributeAssignment) -> Result<()> {
    let maximum = match wire::FixtureAttribute::try_from(attribute.attribute) {
        Ok(
            wire::FixtureAttribute::Intensity
            | wire::FixtureAttribute::Pan
            | wire::FixtureAttribute::Tilt,
        ) => 1.0,
        Ok(wire::FixtureAttribute::StrobeHz) => MAX_STROBE_HZ,
        Ok(wire::FixtureAttribute::Colour) => {
            ensure!(
                attribute.operation == wire::BlendOperation::Replace as i32,
                "colour supports replacement only"
            );
            return validate_value(
                &wire::ParameterDefinition {
                    value_type: wire::ValueType::Rgb as i32,
                    ..Default::default()
                },
                attribute
                    .value
                    .as_ref()
                    .context("attribute value is required")?,
            );
        }
        _ => bail!("unknown fixture attribute {}", attribute.attribute),
    };
    let limit = match wire::BlendOperation::try_from(attribute.operation) {
        Ok(wire::BlendOperation::Replace) => maximum,
        Ok(wire::BlendOperation::Multiply) => MAX_MULTIPLIER,
        _ => bail!("attribute blend operation is required"),
    };
    validate_value(
        &number_definition("fixture attribute", None, 0.0, limit),
        attribute
            .value
            .as_ref()
            .context("attribute value is required")?,
    )
}

pub fn apply_attribute(
    state: &mut FixtureState,
    attribute: &wire::AttributeAssignment,
) -> Result<()> {
    let value = attribute
        .value
        .as_ref()
        .and_then(|value| value.value.as_ref())
        .context("validated attribute has no value")?;
    if let Value::Rgb(value) = value {
        state.colour = Rgb {
            red: value.red,
            green: value.green,
            blue: value.blue,
        };
        return Ok(());
    }
    let Value::Number(value) = value else {
        bail!("validated attribute has the wrong type");
    };
    let (current, maximum) = match wire::FixtureAttribute::try_from(attribute.attribute) {
        Ok(wire::FixtureAttribute::Intensity) => (&mut state.intensity, 1.0),
        Ok(wire::FixtureAttribute::Pan) => (&mut state.pan, 1.0),
        Ok(wire::FixtureAttribute::Tilt) => (&mut state.tilt, 1.0),
        Ok(wire::FixtureAttribute::StrobeHz) => (&mut state.strobe_hz, MAX_STROBE_HZ),
        _ => bail!("validated numeric attribute is unknown"),
    };
    let result = if attribute.operation == wire::BlendOperation::Multiply as i32 {
        *current * value
    } else {
        *value
    };
    ensure!(result.is_finite(), "effect composition overflow");
    *current = result.clamp(0.0, maximum);
    Ok(())
}

/// Recorded semantic overrides use the same composer as live effect commands.
pub fn from_override(value: &LightingOverride) -> wire::SetActiveEffect {
    let selection = match &value.target {
        Target::All => wire::fixture_selection::Selection::All(wire::Empty {}),
        Target::Group(id) => wire::fixture_selection::Selection::GroupId(id.clone()),
        Target::Fixture(id) => wire::fixture_selection::Selection::FixtureId(id.clone()),
    };
    let expiration = match value.lifetime {
        OverrideLifetime::UntilCleared => {
            wire::effect_lifetime::Expiration::UntilRemoved(wire::Empty {})
        }
        OverrideLifetime::Beats { duration_beats } => {
            wire::effect_lifetime::Expiration::DurationBeats(duration_beats)
        }
        OverrideLifetime::Seconds { duration_seconds } => {
            wire::effect_lifetime::Expiration::DurationSeconds(duration_seconds)
        }
    };
    let mut attributes = Vec::new();
    let intensity = value
        .values
        .intensity
        .map(|intensity| (intensity * value.intensity_multiplier.unwrap_or(1.0)).clamp(0.0, 1.0));
    for (attribute, number_value) in [
        (wire::FixtureAttribute::Intensity, intensity),
        (wire::FixtureAttribute::Pan, value.values.pan),
        (wire::FixtureAttribute::Tilt, value.values.tilt),
        (wire::FixtureAttribute::StrobeHz, value.values.strobe_hz),
    ] {
        if let Some(number_value) = number_value {
            attributes.push(wire::AttributeAssignment {
                attribute: attribute as i32,
                value: Some(number(number_value)),
                operation: wire::BlendOperation::Replace as i32,
            });
        }
    }
    if intensity.is_none()
        && let Some(multiplier) = value.intensity_multiplier
    {
        attributes.push(wire::AttributeAssignment {
            attribute: wire::FixtureAttribute::Intensity as i32,
            value: Some(number(multiplier)),
            operation: wire::BlendOperation::Multiply as i32,
        });
    }
    if let Some(colour) = value.values.colour {
        attributes.push(wire::AttributeAssignment {
            attribute: wire::FixtureAttribute::Colour as i32,
            value: Some(wire::ParameterValue {
                value: Some(Value::Rgb(wire::RgbColor {
                    red: colour.red,
                    green: colour.green,
                    blue: colour.blue,
                })),
            }),
            operation: wire::BlendOperation::Replace as i32,
        });
    }
    wire::SetActiveEffect {
        effect_id: value.id.clone(),
        fixtures: Some(wire::FixtureSelection {
            selection: Some(selection),
        }),
        attributes,
        priority: value.priority,
        lifetime: Some(wire::EffectLifetime {
            expiration: Some(expiration),
        }),
    }
}

use std::collections::BTreeMap;

use anyhow::{Context, Result, ensure};
use serde::Serialize;

use crate::model::Rgb;
use crate::protocol::wire;

const MAX_PALETTES: usize = 64;
const MAX_PALETTE_NAME_BYTES: usize = 64;

#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct PaletteSlots {
    pub primary: Rgb,
    pub secondary: Rgb,
    pub accent: Rgb,
    pub cold: Rgb,
    pub black: Rgb,
}

impl PaletteSlots {
    pub fn named_slots(&self) -> [(&'static str, Rgb); 5] {
        [
            ("primary", self.primary),
            ("secondary", self.secondary),
            ("accent", self.accent),
            ("cold", self.cold),
            ("black", self.black),
        ]
    }

    pub fn blend(&self, to: &Self, amount: f64) -> Self {
        let mix = |from: Rgb, to: Rgb| Rgb {
            red: from.red + (to.red - from.red) * amount,
            green: from.green + (to.green - from.green) * amount,
            blue: from.blue + (to.blue - from.blue) * amount,
        };
        Self {
            primary: mix(self.primary, to.primary),
            secondary: mix(self.secondary, to.secondary),
            accent: mix(self.accent, to.accent),
            cold: mix(self.cold, to.cold),
            black: mix(self.black, to.black),
        }
    }
}

const fn rgb(red: f64, green: f64, blue: f64) -> Rgb {
    Rgb { red, green, blue }
}

const BLACK: Rgb = rgb(0.0, 0.0, 0.0);

#[derive(Clone, Debug)]
pub struct PaletteTable {
    slots_by_name: BTreeMap<String, PaletteSlots>,
    initial: String,
}

impl PaletteTable {
    pub fn builtin() -> Self {
        let techno = PaletteSlots {
            primary: rgb(1.0, 0.82, 0.6),
            secondary: rgb(1.0, 0.55, 0.1),
            accent: rgb(0.75, 0.0, 0.02),
            cold: rgb(0.75, 0.85, 1.0),
            black: BLACK,
        };
        let house = PaletteSlots {
            primary: rgb(1.0, 0.82, 0.6),
            secondary: rgb(1.0, 0.0, 0.7),
            accent: rgb(1.0, 0.4, 0.0),
            cold: rgb(0.0, 0.85, 1.0),
            black: BLACK,
        };
        Self {
            slots_by_name: BTreeMap::from([("techno".into(), techno), ("house".into(), house)]),
            initial: "techno".into(),
        }
    }

    pub fn from_definitions(palettes: &[wire::Palette]) -> Result<Self> {
        ensure!(
            (1..=MAX_PALETTES).contains(&palettes.len()),
            "login must supply 1..{MAX_PALETTES} palettes; the first is active until changed"
        );
        let mut slots_by_name = BTreeMap::new();
        for palette in palettes {
            validate_palette_name(&palette.name)?;
            let slot = |colour: &Option<wire::RgbColor>, slot: &str| -> Result<Rgb> {
                let colour = colour
                    .as_ref()
                    .with_context(|| format!("palette {} lacks its {slot} slot", palette.name))?;
                for channel in [colour.red, colour.green, colour.blue] {
                    ensure!(
                        channel.is_finite() && (0.0..=1.0).contains(&channel),
                        "palette {} {slot} channels must be finite and within 0..1",
                        palette.name
                    );
                }
                Ok(rgb(colour.red, colour.green, colour.blue))
            };
            let slots = PaletteSlots {
                primary: slot(&palette.primary, "primary")?,
                secondary: slot(&palette.secondary, "secondary")?,
                accent: slot(&palette.accent, "accent")?,
                cold: slot(&palette.cold, "cold")?,
                black: slot(&palette.black, "black")?,
            };
            ensure!(
                slots_by_name.insert(palette.name.clone(), slots).is_none(),
                "duplicate palette {}",
                palette.name
            );
        }
        Ok(Self {
            slots_by_name,
            initial: palettes[0].name.clone(),
        })
    }

    pub fn slots(&self, name: &str) -> Result<PaletteSlots> {
        self.slots_by_name.get(name).copied().with_context(|| {
            let known: Vec<_> = self.slots_by_name.keys().map(String::as_str).collect();
            format!(
                "unknown palette {name:?}; this session has {}",
                known.join(", ")
            )
        })
    }

    pub fn initial(&self) -> &str {
        &self.initial
    }

    pub fn iter(&self) -> impl Iterator<Item = (&str, &PaletteSlots)> {
        self.slots_by_name
            .iter()
            .map(|(name, slots)| (name.as_str(), slots))
    }
}

fn validate_palette_name(name: &str) -> Result<()> {
    ensure!(
        !name.is_empty()
            && name.len() <= MAX_PALETTE_NAME_BYTES
            && name
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_'),
        "palette name {name:?} must be 1..{MAX_PALETTE_NAME_BYTES} lowercase letters, digits or underscores"
    );
    Ok(())
}

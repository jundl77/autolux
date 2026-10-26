use std::collections::BTreeMap;
use std::fs::File;
use std::io::Read;
use std::path::Path;

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};

use crate::model::Rgb;
use crate::protocol::wire;

const MAX_PALETTES: usize = 64;
const MAX_PALETTE_NAME_BYTES: usize = 64;
const MAX_PALETTE_FILE_BYTES: u64 = 64 * 1024;

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

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaletteDefinition {
    pub name: String,
    pub primary: Rgb,
    pub secondary: Rgb,
    pub accent: Rgb,
    pub cold: Rgb,
    pub black: Rgb,
}

impl PaletteDefinition {
    fn from_wire(palette: &wire::Palette) -> Result<Self> {
        let slot = |colour: &Option<wire::RgbColor>, slot: &str| -> Result<Rgb> {
            let colour = colour
                .as_ref()
                .with_context(|| format!("palette {} lacks its {slot} slot", palette.name))?;
            Ok(Rgb {
                red: colour.red,
                green: colour.green,
                blue: colour.blue,
            })
        };
        Ok(Self {
            name: palette.name.clone(),
            primary: slot(&palette.primary, "primary")?,
            secondary: slot(&palette.secondary, "secondary")?,
            accent: slot(&palette.accent, "accent")?,
            cold: slot(&palette.cold, "cold")?,
            black: slot(&palette.black, "black")?,
        })
    }

    fn slots(&self) -> Result<PaletteSlots> {
        let slots = PaletteSlots {
            primary: self.primary,
            secondary: self.secondary,
            accent: self.accent,
            cold: self.cold,
            black: self.black,
        };
        for (slot, colour) in slots.named_slots() {
            for channel in [colour.red, colour.green, colour.blue] {
                ensure!(
                    channel.is_finite() && (0.0..=1.0).contains(&channel),
                    "palette {} {slot} channels must be finite and within 0..1",
                    self.name
                );
            }
        }
        Ok(slots)
    }
}

#[derive(Clone, Debug)]
pub struct PaletteTable {
    slots_by_name: BTreeMap<String, PaletteSlots>,
    initial: String,
}

impl PaletteTable {
    pub fn new(palettes: &[PaletteDefinition]) -> Result<Self> {
        ensure!(
            (1..=MAX_PALETTES).contains(&palettes.len()),
            "a session needs 1..{MAX_PALETTES} palettes; the first is active until changed"
        );
        let mut slots_by_name = BTreeMap::new();
        for palette in palettes {
            validate_palette_name(&palette.name)?;
            ensure!(
                slots_by_name
                    .insert(palette.name.clone(), palette.slots()?)
                    .is_none(),
                "duplicate palette {}",
                palette.name
            );
        }
        Ok(Self {
            slots_by_name,
            initial: palettes[0].name.clone(),
        })
    }

    pub fn from_wire(palettes: &[wire::Palette]) -> Result<Self> {
        Self::new(
            &palettes
                .iter()
                .map(PaletteDefinition::from_wire)
                .collect::<Result<Vec<_>>>()?,
        )
    }

    pub fn load(path: &Path) -> Result<Self> {
        let mut bytes = Vec::new();
        File::open(path)
            .with_context(|| format!("open palettes {}", path.display()))?
            .take(MAX_PALETTE_FILE_BYTES + 1)
            .read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() as u64 <= MAX_PALETTE_FILE_BYTES,
            "palette file exceeds {MAX_PALETTE_FILE_BYTES} bytes"
        );
        let palettes: Vec<PaletteDefinition> =
            serde_json::from_slice(&bytes).context("decode palettes")?;
        Self::new(&palettes)
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

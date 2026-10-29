//! Section cues on the render clock: pending queue, Autolux beat index to grid beat, sticky
//! activation, the rotation clock and the overlay instance schedule.
//!
//! Cues are kept in Autolux beat-index space and mapped through the newest received beat on
//! every frame, so a bar-phase correction moves a pending cue with the grid. `beat_position` is
//! not monotonic (a late beat re-anchors it), so every state here only moves forward: cues go
//! pending → active → gone, instances pending → running → releasing → done, rotation steps up.
//! Envelope times are elapsed musical beats, which never jump; the grid only schedules.

use anyhow::{Result, ensure};

use crate::model::{OverlayCue, SectionCue, SectionIntent, Transition};

pub const MAX_ROTATION: usize = 8;
pub const MAX_OVERLAYS: usize = 8;
pub const MAX_PENDING_CUES: usize = 4;
pub const MAX_ACTIVE_OVERLAYS: usize = 8;
pub const MAX_PREROLL_BEATS: f64 = 64.0;
pub const MAX_OVERLAY_ERROR_BYTES: usize = 256;
/// The overlay bank whose pre-roll row is the blackout before the section: beats_until_dip and
/// move in black key on this name (the proto Overlay comment states the contract).
pub const DIP_BANK: &str = "dip";

/// What the cue timeline reads from the musical clock at one instant.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CueSample {
    pub beat_position: f64,
    pub elapsed_musical_beats: f64,
    pub beats_per_bar: u32,
    /// The newest received beat as (beat index, grid beat); `None` before the first beat.
    pub anchor: Option<(u64, u64)>,
}

impl CueSample {
    /// i128 because a cue may address a beat before the session's first (grid below zero) and
    /// must still resolve.
    fn grid_of(&self, beat_index: i128) -> Option<f64> {
        let (index, grid) = self.anchor?;
        Some((i128::from(grid) + beat_index - i128::from(index)) as f64)
    }

    fn index_of_grid(&self, grid: i128) -> Option<i128> {
        let (index, anchor_grid) = self.anchor?;
        Some(grid - i128::from(anchor_grid) + i128::from(index))
    }

    fn bar_beats(&self) -> f64 {
        f64::from(self.beats_per_bar)
    }
}

/// The section fields of the render context.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct SectionContext {
    pub section_intent: Option<SectionIntent>,
    pub section_elapsed_beats: Option<f64>,
    pub next_intent: Option<SectionIntent>,
    pub beats_until_next: Option<f64>,
    pub beats_until_dip: Option<f64>,
    /// Elapsed musical beats when the next cue was received.
    pub next_received_at: Option<f64>,
    pub drop_strength: Option<f64>,
    pub next_drop_strength: Option<f64>,
    pub seed: u32,
}

/// A base loop the active cue's rotation wants playing from `begin_beat` (a grid beat, the
/// entry's begin counted from the true section start). The engine starts it unless it is
/// already the running loop.
#[derive(Clone, Debug, PartialEq)]
pub struct BaseStep {
    pub bank: String,
    pub autoloop: String,
    pub transition: Transition,
    pub begin_beat: f64,
    pub seed: u32,
    /// Set on the frame a cue activates: its palette, crossfaded over `transition`.
    pub palette: Option<String>,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Span {
    /// Elapsed musical beats at the first rendered frame (t0).
    pub started: f64,
    /// Beats past the nominal begin at t0; nonzero for a late start.
    pub late_beats: f64,
    /// Elapsed musical beats at the nominal end.
    pub ends: f64,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum InstanceState {
    Pending,
    Running(Span),
    /// From `released_at` (elapsed musical beats) over the row's release beats.
    Releasing {
        span: Span,
        released_at: f64,
    },
}

/// One scheduled play of an overlay row. Done instances are removed and never return.
#[derive(Clone, Debug, PartialEq)]
pub struct OverlayInstance {
    pub overlay: OverlayCue,
    /// Start order across all cues; ties of priority compose in this order.
    pub order: u64,
    pub state: InstanceState,
    /// The owning cue's, for sf.random.
    pub seed: u32,
    owner: u64,
    row: usize,
    /// The beat index `overlay.begin_beats` counts from.
    anchor_index: i128,
}

impl OverlayInstance {
    /// `autoloop_elapsed_beats` for the overlay script: beats since its nominal begin.
    pub fn elapsed_beats(&self, elapsed_musical_beats: f64) -> Option<f64> {
        match self.state {
            InstanceState::Pending => None,
            InstanceState::Running(span) | InstanceState::Releasing { span, .. } => {
                Some(span.late_beats + (elapsed_musical_beats - span.started).max(0.0))
            }
        }
    }

    /// The nominal end as a grid beat; `None` before any beat.
    pub fn nominal_end(&self, now: &CueSample) -> Option<f64> {
        Some(self.nominal_begin(now)? + self.overlay.length_beats)
    }

    fn nominal_begin(&self, now: &CueSample) -> Option<f64> {
        Some(now.grid_of(self.anchor_index)? + self.overlay.begin_beats)
    }

    /// The same row of a re-sent cue: it carries on instead of restarting.
    fn continues(&self, row: &OverlayCue, anchor_index: i128) -> bool {
        let own = &self.overlay;
        self.anchor_index == anchor_index
            && (own.bank.as_str(), own.autoloop.as_str(), own.every_bars)
                == (row.bank.as_str(), row.autoloop.as_str(), row.every_bars)
            && own.begin_beats == row.begin_beats
            && own.length_beats == row.length_beats
    }

    fn release(&mut self, at: f64) {
        if let InstanceState::Running(span) = self.state {
            self.state = InstanceState::Releasing {
                span,
                released_at: at.min(span.ends),
            };
        }
    }

    /// Starts a due instance (false once it can never play); `live` counts the cap.
    fn start(&mut self, now: &CueSample, live: &mut usize, refused: &mut Option<String>) -> bool {
        let Some(begin) = self.nominal_begin(now) else {
            return true;
        };
        let end = begin + self.overlay.length_beats;
        if now.beat_position >= end {
            return false;
        }
        if now.beat_position < begin {
            return true;
        }
        if *live >= MAX_ACTIVE_OVERLAYS {
            *refused = Some(format!(
                "overlay {}/{} not started: {MAX_ACTIVE_OVERLAYS} overlays already play",
                self.overlay.bank, self.overlay.autoloop
            ));
            return false;
        }
        *live += 1;
        let at = now.elapsed_musical_beats;
        self.state = InstanceState::Running(Span {
            started: at,
            late_beats: now.beat_position - begin,
            ends: at + (end - now.beat_position),
        });
        true
    }

    /// Ends and releases a started instance; false once done.
    fn finish(&mut self, at: f64) -> bool {
        if let InstanceState::Running(span) = self.state
            && at >= span.ends
        {
            self.release(span.ends);
        }
        match self.state {
            InstanceState::Releasing { released_at, .. } => {
                at < released_at + self.overlay.release_beats
            }
            _ => true,
        }
    }
}

#[derive(Clone, Debug)]
struct PendingCue {
    cue: SectionCue,
    id: u64,
    /// Elapsed musical beats at receipt: when the room's next change became known.
    received_at: f64,
    /// Fixed when first resolved: the start, or for a cue resolved after its start the next
    /// bar line (a late cue never enters mid-bar).
    activates_at: Option<i128>,
}

#[derive(Clone, Debug)]
struct ActiveCue {
    cue: SectionCue,
    id: u64,
    /// The rotation step playing, counted over cycles; only increases.
    step: Option<u64>,
    /// Per overlay row: the next repetition a recurring row may start (`u64::MAX` once stopped).
    next_repetition: Vec<u64>,
}

#[derive(Clone, Debug, Default)]
pub struct CueTimeline {
    pending: Vec<PendingCue>,
    active: Option<ActiveCue>,
    instances: Vec<OverlayInstance>,
    next_id: u64,
    next_order: u64,
    overlay_error: Option<String>,
}

impl CueTimeline {
    /// Supersedes, then queues. Content (identifiers, ranges, scripts, palette) is validated by
    /// the caller first, so a refused cue changes nothing.
    pub fn receive(&mut self, cue: SectionCue, now: CueSample) -> Result<()> {
        let start = cue.start_beat_index;
        let kept = self
            .pending
            .iter()
            .filter(|pending| pending.cue.start_beat_index < start)
            .count();
        ensure!(
            kept < MAX_PENDING_CUES,
            "more than {MAX_PENDING_CUES} cues would be pending"
        );
        let dropped: Vec<u64> = self
            .pending
            .iter()
            .filter(|pending| pending.cue.start_beat_index >= start)
            .map(|pending| pending.id)
            .collect();
        self.pending
            .retain(|pending| pending.cue.start_beat_index < start);
        self.next_id += 1;
        let mut pending = PendingCue {
            cue,
            id: self.next_id,
            received_at: now.elapsed_musical_beats,
            activates_at: None,
        };
        self.resolve(&mut pending, &now);
        for id in dropped {
            self.hand_over(id, pending.id, None, &now);
            self.release_owner(id, now.elapsed_musical_beats);
        }
        self.pending.push(pending);
        Ok(())
    }

    /// Ends every cue: their overlays release, or vanish when `release` is false.
    pub fn clear(&mut self, now: f64, release: bool) {
        self.pending.clear();
        self.active = None;
        if release {
            self.instances
                .retain(|instance| instance.state != InstanceState::Pending);
            for instance in &mut self.instances {
                instance.release(now);
            }
        } else {
            self.instances.clear();
            self.overlay_error = None;
        }
    }

    /// A frozen clock would hold a release at its level and replay it on play.
    pub fn drop_releasing(&mut self) {
        self.instances
            .retain(|instance| !matches!(instance.state, InstanceState::Releasing { .. }));
    }

    /// Resolves, activates and schedules for this frame; returns the base loop the rotation
    /// wants when it changes.
    pub fn advance(&mut self, now: CueSample) -> Option<BaseStep> {
        now.anchor?;
        let mut pending = std::mem::take(&mut self.pending);
        for cue in &mut pending {
            self.resolve(cue, &now);
        }
        self.pending = pending;
        self.activate_due(&now);
        self.schedule_recurring(&now);
        // Finished instances leave before new ones start, so a boundary frame keeps its slot.
        let at = now.elapsed_musical_beats;
        self.instances
            .retain_mut(|instance| instance.state == InstanceState::Pending || instance.finish(at));
        let mut live = self.active_overlay_count();
        let mut refused = None;
        self.instances.retain_mut(|instance| {
            instance.state != InstanceState::Pending
                || (instance.start(&now, &mut live, &mut refused)
                    && (instance.state == InstanceState::Pending || instance.finish(at)))
        });
        if let Some(reason) = refused {
            self.record_overlay_error(reason);
        }
        self.rotate(&now)
    }

    pub fn context(&self, now: &CueSample) -> SectionContext {
        let mut context = SectionContext::default();
        if let Some(active) = &self.active {
            context.section_intent = Some(active.cue.intent);
            context.section_elapsed_beats = self
                .start_grid(active, now)
                .map(|start| (now.beat_position - start).max(0.0));
            context.drop_strength = active.cue.drop_strength;
            context.seed = active.cue.seed;
        }
        if let Some((pending, grid)) = self.next_pending(now) {
            let cue = &pending.cue;
            let until = (grid - now.beat_position).max(0.0);
            context.next_received_at = Some(pending.received_at);
            context.next_intent = Some(cue.intent);
            context.beats_until_next = Some(until);
            context.next_drop_strength = cue.drop_strength;
            context.beats_until_dip = cue
                .overlays
                .iter()
                .filter(|row| row.bank == DIP_BANK && row.begin_beats < 0.0)
                .map(|row| row.begin_beats)
                .reduce(f64::min)
                .map(|begin| (until + begin).max(0.0));
        }
        context
    }

    /// The earliest resolved pending cue and the grid beat it activates at. A re-sent cue for
    /// the active section is no next section.
    pub fn next_cue(&self, now: &CueSample) -> Option<(&SectionCue, f64)> {
        self.next_pending(now)
            .map(|(pending, grid)| (&pending.cue, grid))
    }

    fn next_pending(&self, now: &CueSample) -> Option<(&PendingCue, f64)> {
        let active_start = self
            .active
            .as_ref()
            .map(|active| active.cue.start_beat_index);
        let (at, pending) = self
            .pending
            .iter()
            .filter(|pending| Some(pending.cue.start_beat_index) != active_start)
            .filter_map(|pending| Some((pending.activates_at?, pending)))
            .min_by_key(|(at, pending)| (*at, pending.id))?;
        Some((pending, now.grid_of(at)?))
    }

    /// Running and releasing overlays, in start order; W2 composes them by priority.
    pub fn instances(&self) -> &[OverlayInstance] {
        &self.instances
    }

    pub fn pending_count(&self) -> usize {
        self.pending.len()
    }

    pub fn active_overlay_count(&self) -> usize {
        self.instances
            .iter()
            .filter(|instance| instance.state != InstanceState::Pending)
            .count()
    }

    pub fn overlay_error(&self) -> Option<&str> {
        self.overlay_error.as_deref()
    }

    /// Bounded for Metrics; an overlay failure never becomes the show's script error.
    pub fn record_overlay_error(&mut self, mut message: String) {
        if message.len() > MAX_OVERLAY_ERROR_BYTES {
            message.truncate(message.floor_char_boundary(MAX_OVERLAY_ERROR_BYTES));
        }
        self.overlay_error = Some(message);
    }

    /// Stops a broken overlay at once, its release skipped, and its row for the rest of the
    /// owning cue: a recurring row starts no further repetitions.
    pub fn stop_row(&mut self, order: u64) {
        let Some(index) = self
            .instances
            .iter()
            .position(|instance| instance.order == order)
        else {
            return;
        };
        let stopped = self.instances.remove(index);
        if let Some(active) = &mut self.active
            && active.id == stopped.owner
        {
            active.next_repetition[stopped.row] = u64::MAX;
        }
    }

    fn resolve(&mut self, pending: &mut PendingCue, now: &CueSample) {
        if pending.activates_at.is_some() {
            return;
        }
        let start = i128::from(pending.cue.start_beat_index);
        let Some(start_grid) = now.grid_of(start) else {
            return;
        };
        let replaces_active = self
            .active
            .as_ref()
            .is_some_and(|active| active.cue.start_beat_index == pending.cue.start_beat_index);
        // Strictly as Autolux's switch clock: a start the room already passed waits for the bar
        // line, so the entry hit, the room-side state and the specials land together.
        let activates_at = if start_grid >= now.beat_position {
            start
        } else {
            let bar = now.bar_beats();
            let bar_line = (now.beat_position / bar).ceil() * bar;
            now.index_of_grid(bar_line as i128).expect("anchor present")
        };
        pending.activates_at = Some(activates_at);
        for (row, overlay) in pending.cue.overlays.iter().enumerate() {
            if overlay.every_bars != 0 {
                continue;
            }
            // A late cue enters on its bar line with its entry rows; its pre-roll, and every row
            // of a re-sent cue (same start as the active one), keep the true start and so are
            // skipped as past, never fired twice.
            let entry = if replaces_active || overlay.begin_beats < 0.0 {
                start
            } else {
                activates_at
            };
            self.next_order += 1;
            self.instances.push(OverlayInstance {
                overlay: overlay.clone(),
                order: self.next_order,
                state: InstanceState::Pending,
                seed: pending.cue.seed,
                owner: pending.id,
                row,
                anchor_index: entry,
            });
        }
    }

    fn activate_due(&mut self, now: &CueSample) {
        // Several cues may pass in one frame (after a resume): the latest wins, the rest drop.
        let passed: Vec<usize> = (0..self.pending.len())
            .filter(|&index| {
                self.pending[index]
                    .activates_at
                    .and_then(|at| now.grid_of(at))
                    .is_some_and(|grid| now.beat_position >= grid)
            })
            .collect();
        let Some(&latest) = passed.iter().max_by_key(|&&index| {
            let pending = &self.pending[index];
            (pending.activates_at, pending.id)
        }) else {
            return;
        };
        let winner = self.pending[latest].id;
        let mut chosen = None;
        let mut losers = Vec::new();
        for index in passed.into_iter().rev() {
            let cue = self.pending.remove(index);
            if cue.id == winner {
                chosen = Some(cue);
            } else {
                losers.push(cue.id);
            }
        }
        let cue = chosen.expect("the latest passed cue");
        for id in losers {
            self.release_owner(id, now.elapsed_musical_beats);
        }
        let mut active = ActiveCue {
            next_repetition: vec![0; cue.cue.overlays.len()],
            cue: cue.cue,
            id: cue.id,
            step: None,
        };
        if let Some(previous) = self.active.take() {
            self.hand_over(previous.id, active.id, Some(&mut active), now);
            self.release_owner(previous.id, now.elapsed_musical_beats);
        }
        self.overlay_error = None;
        self.active = Some(active);
    }

    /// Gives `to` the running instances of `from` that its own rows would play again, so a
    /// re-sent cue's riser, accent or fill carries on instead of restarting.
    fn hand_over(
        &mut self,
        from: u64,
        to: u64,
        mut active: Option<&mut ActiveCue>,
        now: &CueSample,
    ) {
        let mut index = 0;
        while index < self.instances.len() {
            let instance = &self.instances[index];
            index += 1;
            if instance.owner != from || !matches!(instance.state, InstanceState::Running(_)) {
                continue;
            }
            let index = index - 1;
            // A one-shot row of `to` already waits as a pending instance; it yields.
            let waiting = self.instances.iter().position(|candidate| {
                candidate.owner == to
                    && candidate.state == InstanceState::Pending
                    && instance.continues(&candidate.overlay, candidate.anchor_index)
            });
            if let Some(waiting) = waiting {
                let (row, seed) = (self.instances[waiting].row, self.instances[waiting].seed);
                let carried = &mut self.instances[index];
                (carried.owner, carried.row, carried.seed) = (to, row, seed);
                self.instances.remove(waiting);
                // Carried instances now belong to `to`, so a rescan skips them.
                return self.hand_over(from, to, active, now);
            }
            // A recurring row of the newly active cue: that repetition carries on.
            let Some(active) = active.as_deref_mut() else {
                continue;
            };
            let start = i128::from(active.cue.start_beat_index);
            let offset = instance.anchor_index - start;
            let matched = active
                .cue
                .overlays
                .iter()
                .enumerate()
                .find_map(|(row, overlay)| {
                    let period = i128::from(overlay.every_bars) * i128::from(now.beats_per_bar);
                    (period > 0
                        && offset >= 0
                        && offset % period == 0
                        && instance.continues(overlay, instance.anchor_index))
                    .then(|| (row, (offset / period) as u64))
                });
            if let Some((row, repetition)) = matched {
                let carried = &mut self.instances[index];
                (carried.owner, carried.row, carried.seed) = (to, row, active.cue.seed);
                active.next_repetition[row] = active.next_repetition[row].max(repetition + 1);
            }
        }
    }

    fn start_grid(&self, active: &ActiveCue, now: &CueSample) -> Option<f64> {
        now.grid_of(i128::from(active.cue.start_beat_index))
    }

    /// Recurring rows play on phrase lines counted from the true start, while the cue is active.
    fn schedule_recurring(&mut self, now: &CueSample) {
        let Some(active) = &mut self.active else {
            return;
        };
        let start = i128::from(active.cue.start_beat_index);
        let Some(start_grid) = now.grid_of(start) else {
            return;
        };
        let bar = now.bar_beats();
        for (row, overlay) in active.cue.overlays.iter().enumerate() {
            if overlay.every_bars == 0 {
                continue;
            }
            let period = f64::from(overlay.every_bars) * bar;
            let since = now.beat_position - start_grid - overlay.begin_beats;
            if since < 0.0 {
                continue;
            }
            // Only the current repetition: after a jump the skipped ones are long over.
            let due = (since / period).floor() as u64;
            if due < active.next_repetition[row] {
                continue;
            }
            active.next_repetition[row] = due + 1;
            self.next_order += 1;
            self.instances.push(OverlayInstance {
                overlay: overlay.clone(),
                order: self.next_order,
                state: InstanceState::Pending,
                seed: active.cue.seed,
                owner: active.id,
                row,
                anchor_index: start + i128::from(due) * (period as i128),
            });
        }
    }

    fn rotate(&mut self, now: &CueSample) -> Option<BaseStep> {
        let start_grid = self.start_grid(self.active.as_ref()?, now)?;
        let active = self.active.as_mut()?;
        let bar = now.bar_beats();
        let rotation = &active.cue.rotation;
        let cycle: f64 = rotation
            .iter()
            .map(|entry| f64::from(entry.bars) * bar)
            .sum();
        let elapsed = (now.beat_position - start_grid).max(0.0);
        let cycles = (elapsed / cycle).floor();
        let mut entry_begin = cycles * cycle;
        let mut index = rotation.len() - 1;
        for (candidate, entry) in rotation.iter().enumerate().take(rotation.len() - 1) {
            let length = f64::from(entry.bars) * bar;
            if elapsed < entry_begin + length {
                index = candidate;
                break;
            }
            entry_begin += length;
        }
        let step = cycles as u64 * rotation.len() as u64 + index as u64;
        let entering = active.step.is_none();
        if active.step.is_some_and(|current| step <= current) {
            return None;
        }
        active.step = Some(step);
        let entry = &rotation[index];
        Some(BaseStep {
            bank: entry.bank.clone(),
            autoloop: entry.autoloop.clone(),
            transition: entry.transition.clone(),
            begin_beat: start_grid + entry_begin,
            seed: active.cue.seed,
            palette: entering.then(|| active.cue.palette.clone()),
        })
    }

    /// Ends one cue's overlays: running ones release from now, unstarted ones are discarded.
    fn release_owner(&mut self, owner: u64, now: f64) {
        self.instances
            .retain(|instance| instance.owner != owner || instance.state != InstanceState::Pending);
        for instance in &mut self.instances {
            if instance.owner == owner {
                instance.release(now);
            }
        }
    }
}

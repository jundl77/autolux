//! Realtime scheduling for the capture callback thread. cpal 0.18 registers MMCSS only
//! behind its `realtime` feature (via `audio_thread_priority`, task "Audio"), which is off.
#![allow(unsafe_code)]

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Scheduling {
    Mmcss,
    TimeCritical,
    Denied,
}

impl Scheduling {
    pub fn describe(self) -> &'static str {
        match self {
            Self::Mmcss => "Radius capture thread registered with MMCSS \"Pro Audio\"",
            Self::TimeCritical => {
                "Radius capture thread runs at THREAD_PRIORITY_TIME_CRITICAL; MMCSS \"Pro Audio\" unavailable"
            }
            Self::Denied => {
                "Radius capture thread runs without realtime scheduling; capture discontinuities are more likely under load"
            }
        }
    }
}

/// Call on the audio callback thread. The registration ends with the thread, which cpal
/// joins when the stream drops.
#[cfg(windows)]
pub fn promote_current_thread() -> Scheduling {
    use windows::Win32::System::Threading::{
        AvSetMmThreadCharacteristicsW, GetCurrentThread, SetThreadPriority,
        THREAD_PRIORITY_TIME_CRITICAL,
    };
    let mut task_index = 0;
    // SAFETY: a static NUL-terminated task name and a live out pointer.
    if unsafe { AvSetMmThreadCharacteristicsW(windows::core::w!("Pro Audio"), &mut task_index) }
        .is_ok()
    {
        return Scheduling::Mmcss;
    }
    // SAFETY: the current-thread pseudo handle is always valid.
    match unsafe { SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL) } {
        Ok(()) => Scheduling::TimeCritical,
        Err(_) => Scheduling::Denied,
    }
}

#[cfg(not(windows))]
pub fn promote_current_thread() -> Scheduling {
    Scheduling::Denied
}

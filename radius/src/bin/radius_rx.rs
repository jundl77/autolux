use anyhow::{Context, Result};
use clap::Parser;
use radius::{RxConfig, audio, rx::run_receiver};
use std::{
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::Duration,
};

#[derive(Parser)]
#[command(about = "Synchronized Radius audio and DMX receiver")]
struct Arguments {
    config: Option<PathBuf>,
    #[arg(long)]
    check_config: bool,
    #[arg(long)]
    list_devices: bool,
}

fn main() -> Result<()> {
    let args = Arguments::parse();
    if args.list_devices {
        println!("{}", serde_json::to_string_pretty(&audio::list_devices()?)?);
        return Ok(());
    }
    let config = RxConfig::from_file(
        args.config
            .as_deref()
            .context("provide a receiver configuration file")?,
    )?;
    if args.check_config {
        if config.audio_enabled {
            audio::check_device(&config.audio_device_id)?;
        }
        println!("Radius receiver configuration and audio endpoint are ready");
        return Ok(());
    }
    let runtime = std::env::var_os("AUTOLUX_RUNTIME_DIR").map(PathBuf::from);
    if let Some(directory) = &runtime {
        std::fs::create_dir_all(directory)?;
    }
    let ready = runtime.as_ref().map(|directory| directory.join("ready"));
    let health = runtime
        .as_ref()
        .map(|directory| directory.join("health.json"));
    let stop_file = runtime.as_ref().map(|directory| directory.join("stop"));
    let stop = Arc::new(AtomicBool::new(false));
    let signal_stop = stop.clone();
    ctrlc::set_handler(move || {
        signal_stop.store(true, Ordering::Release);
    })?;
    let watcher_stop = stop.clone();
    let watcher = thread::spawn(move || {
        while !watcher_stop.load(Ordering::Acquire) {
            if stop_file.as_ref().is_some_and(|path| path.exists()) {
                watcher_stop.store(true, Ordering::Release);
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }
    });
    let result = run_receiver(config, stop.clone(), ready.as_deref(), health.as_deref());
    stop.store(true, Ordering::Release);
    let _ = watcher.join();
    if let Some(path) = ready {
        let _ = std::fs::remove_file(path);
    }
    result
}

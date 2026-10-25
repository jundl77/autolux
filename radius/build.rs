fn main() -> Result<(), Box<dyn std::error::Error>> {
    const SCHEMA: &str = "../proto/radius/radius.proto";
    println!("cargo:rerun-if-changed={SCHEMA}");
    prost_build::Config::new()
        .type_attribute(".autolux.radius.Metrics", "#[derive(serde::Serialize)]")
        .type_attribute(".autolux.radius.TxMetrics", "#[derive(serde::Serialize)]")
        .protoc_executable(protoc_bin_vendored::protoc_bin_path()?)
        .compile_protos(&[SCHEMA], &[".."])?;
    Ok(())
}

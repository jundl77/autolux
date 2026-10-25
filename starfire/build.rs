fn main() -> Result<(), Box<dyn std::error::Error>> {
    const SCHEMA: &str = "../proto/starfire/starfire.proto";
    println!("cargo:rerun-if-changed={SCHEMA}");
    prost_build::Config::new()
        .protoc_executable(protoc_bin_vendored::protoc_bin_path()?)
        .compile_protos(&[SCHEMA], &["../proto/starfire"])?;
    Ok(())
}

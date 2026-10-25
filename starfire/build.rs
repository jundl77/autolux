fn main() -> Result<(), Box<dyn std::error::Error>> {
    const SCHEMAS: &[&str] = &[
        "../proto/starfire/starfire.proto",
        "../proto/starfire/rig.proto",
    ];
    for schema in SCHEMAS {
        println!("cargo:rerun-if-changed={schema}");
    }
    prost_build::Config::new()
        .protoc_executable(protoc_bin_vendored::protoc_bin_path()?)
        .compile_protos(SCHEMAS, &[".."])?;
    Ok(())
}

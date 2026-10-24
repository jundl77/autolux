[CmdletBinding()]
param([string]$Cargo = '')

$ErrorActionPreference = 'Stop'
if (-not $Cargo) {
    $cargoCommand = Get-Command cargo -ErrorAction SilentlyContinue
    if ($cargoCommand) {
        $Cargo = $cargoCommand.Source
    }
    else {
        $cargoDirectory = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE '.cargo' }
        $Cargo = Join-Path $cargoDirectory 'bin\cargo.exe'
    }
}
if (-not (Test-Path -LiteralPath $Cargo -PathType Leaf)) {
    throw 'Install Rust with rustup; rust-toolchain.toml selects the compiler and components.'
}

Push-Location $PSScriptRoot
try {
    # Cargo/rustup use stderr for ordinary progress on Windows PowerShell.
    $ErrorActionPreference = 'Continue'
    & $Cargo fmt --manifest-path starfire/Cargo.toml
    if ($LASTEXITCODE -ne 0) { throw 'Rust formatting failed.' }
    & $Cargo clippy --manifest-path starfire/Cargo.toml --locked --all-targets -- -D warnings
    if ($LASTEXITCODE -ne 0) { throw 'Rust linting failed.' }
    & $Cargo test --manifest-path starfire/Cargo.toml --locked
    if ($LASTEXITCODE -ne 0) { throw 'Starfire tests failed.' }
    & $Cargo build --manifest-path starfire/Cargo.toml --locked --release
    if ($LASTEXITCODE -ne 0) { throw 'Starfire release build failed.' }
}
finally {
    $ErrorActionPreference = 'Stop'
    Pop-Location
}

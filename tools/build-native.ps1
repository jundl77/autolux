[CmdletBinding()]
param(
    [ValidateSet('workspace', 'radius', 'starfire')]
    [string]$Package = 'workspace',
    [string]$Cargo = ''
)

$ErrorActionPreference = 'Stop'
if (-not $Cargo) {
    $cargoCommand = Get-Command cargo -ErrorAction SilentlyContinue
    if ($cargoCommand) {
        $Cargo = $cargoCommand.Source
    }
    else {
        $cargoDirectory = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE '.cargo' }
        $Cargo = Join-Path $cargoDirectory 'bin/cargo.exe'
    }
}
if (-not (Test-Path -LiteralPath $Cargo -PathType Leaf)) {
    throw 'Install Rust with rustup; rust-toolchain.toml selects the compiler and components.'
}

Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    # Native tools use stderr for ordinary progress on Windows PowerShell.
    $ErrorActionPreference = 'Continue'
    uv run --locked --extra dev ruff format autolux cli proto tools
    if ($LASTEXITCODE -ne 0) { throw 'Python formatting failed.' }
    $protoSources = @(git -c core.quotepath=false ls-files --cached --others --exclude-standard -- proto |
        Where-Object { $_ -match '\.proto$' -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
        Sort-Object -Unique)
    if ($LASTEXITCODE -ne 0) { throw 'Could not enumerate protocol schemas.' }
    if ($protoSources.Count -gt 0) {
        uv run --locked --extra dev clang-format -i --style=file @protoSources
        if ($LASTEXITCODE -ne 0) { throw 'Protobuf formatting failed.' }
    }
    uv run --no-project --with grpcio-tools==1.62.3 --with setuptools python tools/build_proto.py
    if ($LASTEXITCODE -ne 0) { throw 'Protocol generation failed.' }

    [string[]]$packageArguments = if ($Package -eq 'workspace') { @('--workspace') } else { @('--package', $Package) }
    & $Cargo fmt --all
    if ($LASTEXITCODE -ne 0) { throw 'Rust formatting failed.' }
    & $Cargo clippy @packageArguments --locked --all-targets -- -D warnings
    if ($LASTEXITCODE -ne 0) { throw 'Rust linting failed.' }
    # Tee so stdout still streams live (success path unchanged) while we keep
    # the lines needed to pinpoint a failure without re-running the suite.
    & $Cargo test @packageArguments --locked | Tee-Object -Variable cargoTestOutput
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host '--- Rust test failures ---'
        $cargoTestOutput | Select-String -Pattern '^test .* FAILED', 'panicked at', 'test result: FAILED' |
            ForEach-Object { $_.Line }
        Write-Host ''
        Write-Host '--- Last 30 lines of cargo test output ---'
        $cargoTestOutput | Select-Object -Last 30
        throw 'Rust tests failed.'
    }
    & $Cargo build @packageArguments --locked --release
    if ($LASTEXITCODE -ne 0) { throw 'Rust release build failed.' }
}
finally {
    $ErrorActionPreference = 'Stop'
    Pop-Location
}

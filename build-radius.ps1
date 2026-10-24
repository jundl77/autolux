[CmdletBinding()]
param([string]$CMake = '')

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    # Native tools use stderr for normal progress on Windows PowerShell.
    $ErrorActionPreference = 'Continue'
    uv run --locked --extra dev ruff format autolux cli radius tools
    if ($LASTEXITCODE -ne 0) { throw 'Python formatting failed.' }
    $nativeSources = @(git -c core.quotepath=false ls-files --cached --others --exclude-standard -- radius proto |
        Where-Object {
            $_ -match '\.(cc|cpp|h|hpp|proto)$' -and
            $_ -notmatch '^radius/(x_|cmake-build-)' -and
            (Test-Path -LiteralPath $_ -PathType Leaf)
        } |
        Sort-Object -Unique)
    if ($LASTEXITCODE -ne 0) { throw 'Could not enumerate native sources.' }
    if ($nativeSources.Count -gt 0) {
        uv run --locked --extra dev clang-format -i --style=file @nativeSources
        if ($LASTEXITCODE -ne 0) { throw 'C++ formatting failed.' }
    }
}
finally {
    $ErrorActionPreference = 'Stop'
    Pop-Location
}
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $vswhere)) { throw 'Install Visual Studio C++ Build Tools first.' }
$vsRoot = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsRoot) { throw 'Visual Studio C++ Build Tools were not found.' }
if (-not $CMake) {
    $CMake = Join-Path $vsRoot 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
}
if (-not (Test-Path -LiteralPath $CMake)) { throw 'CMake was not found. Pass -CMake with the path to Windows cmake.exe.' }
$sourceDir = Join-Path $PSScriptRoot 'radius'
$buildDir = Join-Path $sourceDir 'cmake-build-release'
$devCmd = Join-Path $vsRoot 'Common7\Tools\VsDevCmd.bat'
foreach ($dependency in @('x_njson\CMakeLists.txt', 'x_libsoundio\CMakeLists.txt')) {
    if (-not (Test-Path -LiteralPath (Join-Path $sourceDir $dependency))) {
        throw 'Initialize native dependencies with: git submodule update --init --recursive'
    }
}
$command = 'call "{0}" -arch=x86 -host_arch=x64 && "{1}" -S "{2}" -B "{3}" -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release -DBUILD_EXAMPLE_PROGRAMS=OFF -DBUILD_TESTS=OFF -DJSON_BuildTests=OFF && "{1}" --build "{3}" --target radius_tx radius_rx audio_delay_test transport_test bus_test usb_test' -f $devCmd, $CMake, $sourceDir, $buildDir
$ErrorActionPreference = 'Continue'
try {
    & $env:ComSpec /d /c $command
    $buildExit = $LASTEXITCODE
}
finally { $ErrorActionPreference = 'Stop' }
if ($buildExit -ne 0) { throw 'Native build failed.' }
$ctest = Join-Path (Split-Path $CMake -Parent) 'ctest.exe'
& $ctest --test-dir $buildDir --output-on-failure
if ($LASTEXITCODE -ne 0) { throw 'Native tests failed.' }

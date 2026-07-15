# Native PowerShell script to compile IOGraph as a standalone single-file .exe

# Determine project root directory (parent directory of this script's directory)
$ProjectRoot = Resolve-Path "$PSScriptRoot/.."
Push-Location $ProjectRoot

# 1. Determine the build version
$version = $env:IOGRAPH_VERSION
if (-not $version) {
    if (Test-Path "VERSION") {
        $version = (Get-Content "VERSION").Trim()
    }
}
if (-not $version) {
    $version = "2.0.3-dev"
}

# Normalize version (strip leading 'v')
if ($version.StartsWith("v")) {
    $version = $version.Substring(1)
}

# Write version file if build version came from env or didn't exist
if ($env:IOGRAPH_VERSION -or -not (Test-Path "VERSION")) {
    $utf8NoBomEncoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText((Join-Path $ProjectRoot "VERSION"), $version, $utf8NoBomEncoding)
}

Write-Host "Building version: $version"

# 2. Parse version for version_info.txt
$major = 0; $minor = 0; $patch = 0
$m = [regex]::Match($version, '^(\d+)\.(\d+)\.(\d+)')
if ($m.Success) {
    $major = [int]$m.Groups[1].Value
    $minor = [int]$m.Groups[2].Value
    $patch = [int]$m.Groups[3].Value
}

# Ensure build directory exists
if (-not (Test-Path "build")) {
    New-Item -ItemType Directory -Path "build" -Force | Out-Null
}

# Create version_info.txt
$versionContent = @(
    "# UTF-8"
    "VSVersionInfo("
    "  ffi=FixedFileInfo("
    "    filevers=($major, $minor, $patch, 0),"
    "    prodvers=($major, $minor, $patch, 0),"
    "    mask=0x3f,"
    "    flags=0x0,"
    "    OS=0x40004,"
    "    fileType=0x1,"
    "    subtype=0x0,"
    "    date=(0, 0)"
    "  ),"
    "  kids=["
    "    StringFileInfo("
    "      ["
    "        StringTable("
    "          u'040904B0',"
    "          [StringStruct(u'CompanyName', u'IOGraphica'),"
    "           StringStruct(u'FileDescription', u'IOGraph'),"
    "           StringStruct(u'FileVersion', u'$version'),"
    "           StringStruct(u'InternalName', u'IOGraph'),"
    "           StringStruct(u'OriginalFilename', u'IOGraph.exe'),"
    "           StringStruct(u'ProductName', u'IOGraph'),"
    "           StringStruct(u'ProductVersion', u'$version')]"
    "        )"
    "      ]"
    "    ),"
    "    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])"
    "  ]"
    ")"
) -join "`r`n"

$utf8NoBomEncoding = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $ProjectRoot "build/version_info.txt"), $versionContent, $utf8NoBomEncoding)

# 3. Locate pyinstaller
$pyinstallerPath = ""
$venvPyInstaller = Join-Path $ProjectRoot ".venv\Scripts\pyinstaller.exe"
if (Test-Path $venvPyInstaller) {
    $pyinstallerPath = $venvPyInstaller
} else {
    $pyinstallerPath = Get-Command pyinstaller -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
}

if (-not $pyinstallerPath) {
    Write-Error "PyInstaller was not found in the virtual environment or on the system PATH."
    Pop-Location
    exit 1
}

Write-Host "Using PyInstaller: $pyinstallerPath"
Write-Host "Running PyInstaller compilation..."

# 4. Run PyInstaller
& $pyinstallerPath --noconfirm --windowed --onefile --name IOGraph --icon iograph/resources/icon256.png --version-file build/version_info.txt --add-data "iograph/resources;iograph/resources" --add-data "VERSION;." --collect-all PyQt6 run_iograph.py

if ($LASTEXITCODE -eq 0) {
    Write-Host "Build complete! Standalone executable created at: $(Join-Path $ProjectRoot 'dist\IOGraph.exe')"
} else {
    Write-Error "PyInstaller compilation failed."
}

Pop-Location
exit $LASTEXITCODE

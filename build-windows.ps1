param(
    [string]$OutputDirectory = "release"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name Snatcher `
    --collect-all playwright `
    packaging/windows_entry.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$stagingDirectory = Join-Path "build" "Snatcher-Windows-x64"
$archivePath = Join-Path $OutputDirectory "Snatcher-Windows-x64.zip"

Remove-Item $stagingDirectory -Recurse -Force -ErrorAction SilentlyContinue
New-Item $stagingDirectory -ItemType Directory -Force | Out-Null
New-Item $OutputDirectory -ItemType Directory -Force | Out-Null

Copy-Item "dist/Snatcher.exe" $stagingDirectory
Copy-Item "courses.txt" $stagingDirectory
Copy-Item "packaging/README-Windows.txt" $stagingDirectory

Remove-Item $archivePath -Force -ErrorAction SilentlyContinue
Compress-Archive -Path "$stagingDirectory/*" -DestinationPath $archivePath -CompressionLevel Optimal

$archive = Get-Item $archivePath
$hash = Get-FileHash $archivePath -Algorithm SHA256
Write-Host "Created: $($archive.FullName)"
Write-Host "Size: $($archive.Length) bytes"
Write-Host "SHA256: $($hash.Hash)"

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
Set-Location $project

$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$ffprobe = (Get-Command ffprobe -ErrorAction Stop).Source
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    throw 'PyInstaller no está instalado. Ejecuta: python -m pip install pyinstaller'
}

python -m PyInstaller --noconfirm --clean --noconsole --onedir --name NoiseCutStudio `
    --icon assets/icon.ico --add-data 'assets;assets' --collect-all PySide6.QtMultimedia noisecut.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller no pudo crear la aplicación.' }

$bundle = Join-Path $project 'dist\NoiseCutStudio\ffmpeg'
New-Item -ItemType Directory -Path $bundle -Force | Out-Null
Copy-Item -LiteralPath $ffmpeg -Destination (Join-Path $bundle 'ffmpeg.exe') -Force
Copy-Item -LiteralPath $ffprobe -Destination (Join-Path $bundle 'ffprobe.exe') -Force
Write-Host "Aplicación lista en: $project\dist\NoiseCutStudio\NoiseCutStudio.exe"

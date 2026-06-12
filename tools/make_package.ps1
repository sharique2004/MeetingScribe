# Builds MeetingScribe-portable.zip — everything needed to run the app on
# another Windows PC. Excludes the venv (not portable across machines) and
# your recordings (privacy). Includes the downloaded AI models by default so
# the other PC doesn't have to re-download ~550 MB; pass -NoModels to skip.
param([switch]$NoModels)

$ErrorActionPreference = "Stop"
$app = Split-Path -Parent (Split-Path -Parent $PSCommandPath)   # MeetingScribe folder
$dest = Join-Path (Split-Path -Parent $app) "MeetingScribe-portable.zip"
$staging = Join-Path $env:TEMP "MeetingScribe-package"

Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force (Join-Path $staging "MeetingScribe") | Out-Null

$exclude = @("venv", "recordings", "__pycache__", ".pytest_cache")
if ($NoModels) { $exclude += "models" }

Get-ChildItem $app -Force | Where-Object { $exclude -notcontains $_.Name } |
    Copy-Item -Destination (Join-Path $staging "MeetingScribe") -Recurse -Force
Get-ChildItem (Join-Path $staging "MeetingScribe") -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force

Compress-Archive -Path (Join-Path $staging "MeetingScribe") -DestinationPath $dest -Force
Remove-Item -Recurse -Force $staging

$size = (Get-Item $dest).Length / 1MB
Write-Output ("Package ready: {0}  ({1:N0} MB)" -f $dest, $size)

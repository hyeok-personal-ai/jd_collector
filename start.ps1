param([int]$Port = 8000, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:RADAR_WORKER = '1'
$env:RADAR_DATA_DIR = Join-Path $PSScriptRoot 'data'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 이상이 필요합니다.' }
    & $python -m pip install -e '.[dev]'
    if ($LASTEXITCODE -ne 0) { throw '패키지 설치 실패' }
}
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port" }
& $python -X utf8 -m app.launcher --port $Port
exit $LASTEXITCODE

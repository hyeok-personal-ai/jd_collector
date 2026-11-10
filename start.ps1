param([int]$Port = 8000, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:RADAR_WORKER = '1'
$env:RADAR_DATA_DIR = Join-Path $PSScriptRoot 'data'
$activeRuntime = Join-Path $PSScriptRoot 'data\active-runtime.json'
if (Test-Path -LiteralPath $activeRuntime) {
    $active = Get-Content -LiteralPath $activeRuntime -Raw | ConvertFrom-Json
    $runtimePath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $active.data_dir))
    $allowedRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'data')) + [IO.Path]::DirectorySeparatorChar
    if (-not $runtimePath.StartsWith($allowedRoot,[StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath (Join-Path $runtimePath 'recruit.db'))) { throw 'Invalid active runtime data directory' }
    if ($active.port -lt 1024 -or $active.port -gt 65535) { throw 'Invalid active runtime port' }
    if (-not $PSBoundParameters.ContainsKey('Port')) { $Port = [int]$active.port }
    $env:RADAR_DATA_DIR = $runtimePath
    $env:DATABASE_URL = 'sqlite:///' + (Join-Path $runtimePath 'recruit.db')
}
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 이상이 필요합니다.' }
    & $python -m pip install -e '.[dev,analysis,documents]'
    if ($LASTEXITCODE -ne 0) { throw '패키지 설치 실패' }
}
& $python -c "import fastembed" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install -e '.[analysis]'
    if ($LASTEXITCODE -ne 0) { throw '유사도 분석 패키지 설치 실패' }
}
& $python -c "import pypdf, reportlab" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install -e '.[documents]'
    if ($LASTEXITCODE -ne 0) { throw '문서 분석/리포트 패키지 설치 실패' }
}
$alreadyRunning = $false
try {
    $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
    $definition = Invoke-RestMethod "http://127.0.0.1:$Port/openapi.json" -TimeoutSec 2
    $alreadyRunning = $health.ok -and $definition.info.title -eq 'Recruit Radar'
} catch {}
if ($alreadyRunning) {
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port" }
    Write-Host "Recruit Radar is already running at http://127.0.0.1:$Port/"
    exit 0
}
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port" }
& $python -X utf8 -m app.launcher --port $Port
exit $LASTEXITCODE

<#
End-to-end WoWs ship render pipeline.

For each ship:
  1. wows-gltf-exporter  -> raw .glb (geometry + textures from the game install)
  2. sanitize_glb.py     -> drop empty meshes so Blender can import it
  3. Blender + Cycles    -> 4K PNG renders

Output layout mirrors the existing wiki gallery:
  WoWs_Renders/<Nation>/<Type>/Tier <Tier>/<ShipKey>/<view>.png

Usage
-----
  # one ship
  powershell -File render_all.ps1 -Ships PGSB108_Bismarck

  # a tier's worth
  powershell -File render_all.ps1 -Nation Germany -Type Battleship -Tier 8

  # everything (long!)
  powershell -File render_all.ps1 -All

Params: -Res 3840x2160 -Samples 128 -Views hero,side -Preset day -SkipExisting
#>
[CmdletBinding()]
param(
    [string[]]$Ships    = @(),
    [string] $Nation    = "",
    [string] $Type      = "",
    [string] $Tier      = "",
    [switch] $Everything,
    [string] $Res       = "3840x2160",
    [int]    $Samples   = 128,
    [string] $Views     = "hero,side,bow,top",
    [string] $Preset    = "day",
    [string] $TexSize   = "4096",
    [switch] $SkipExisting,
    [switch] $KeepGlb,
    # 以下两项可覆盖环境变量和内置默认值
    [string] $GameDir   = "",
    [string] $Python    = ""
)

$ErrorActionPreference = "Stop"

# ------------------------------------------------------------------ paths
$Root       = $PSScriptRoot
$Warships   = Split-Path $Root -Parent
$ToolsDir   = Join-Path $Warships "_tools"

# Blender 的 Windows 压缩包解压后目录名带版本号（如 blender-4.5.10-windows-x64），
# 每次升级都会变，所以这里自动查找，取版本号最大的那个。
# 优先级：WOWS_BLENDER 环境变量 > _tools/blender-*/blender.exe > 历史默认路径
function Find-Blender([string]$ToolsDir) {
    if ($env:WOWS_BLENDER) { return $env:WOWS_BLENDER }

    $candidates = Get-ChildItem -Path $ToolsDir -Directory -Filter "blender-*" -ErrorAction SilentlyContinue |
        ForEach-Object {
            $exe = Join-Path $_.FullName "blender.exe"
            if (Test-Path $exe) {
                # 从目录名解析 x.y.z 用于排序；解析不出来就当 0.0.0
                $m = [regex]::Match($_.Name, 'blender-(\d+)\.(\d+)\.?(\d+)?')
                $ver = if ($m.Success) {
                    [version]("{0}.{1}.{2}" -f [int]$m.Groups[1].Value,
                                              [int]$m.Groups[2].Value,
                                              $(if ($m.Groups[3].Value) { [int]$m.Groups[3].Value } else { 0 }))
                } else { [version]"0.0.0" }
                [pscustomobject]@{ Path = $exe; Version = $ver }
            }
        }
    if ($candidates) {
        return ($candidates | Sort-Object Version -Descending | Select-Object -First 1).Path
    }
    return Join-Path $ToolsDir "blender-4.5.10-windows-x64\blender.exe"
}

$Blender    = Find-Blender $ToolsDir
$Exporter   = Join-Path $ToolsDir "model-exporter\wows-gltf-exporter.exe"
$ListShips  = Join-Path $ToolsDir "model-exporter\wows-list-ships.exe"
$RenderPy   = Join-Path $Root "render_ship.py"
$SanitizePy = Join-Path $Root "sanitize_glb.py"

# ------------------------------------------------------------------ overridable paths
# 这两项可以用环境变量覆盖，也可以在命令行显式传参：
#   $env:WOWS_GAME_DIR = "E:\Games\World of Warships"
#   $env:WOWS_PYTHON   = "C:\Python312\python.exe"
# 没设环境变量时退回下面写的默认值（作者本机配置）。
$DefaultGameDir = "D:\SteamLibrary\steamapps\common\World of Warships"
$GameDir = $GameDir
if (-not $GameDir) { $GameDir = if ($env:WOWS_GAME_DIR) { $env:WOWS_GAME_DIR } else { $DefaultGameDir } }

# Python：-Python 参数 > WOWS_PYTHON 环境变量 > 逐个探测常见安装位置 > PATH 里的 python
$Python = $Python
if (-not $Python) { $Python = $env:WOWS_PYTHON }
if (-not $Python) {
    $candidates = @(
        "C:\Program Files\Python313\python.exe"
        "C:\Program Files\Python312\python.exe"
        "C:\Program Files\Python311\python.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { $Python = $c; break } }
}
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cmd.Source }
}

$GlbDir     = Join-Path $Root "glb"
$OutRoot    = Join-Path $Warships "WoWs_Renders"
$LogPath    = Join-Path $Root "render_log.tsv"

# ------------------------------------------------------------------ resolve -Python / -GameDir to absolute
# 用 Convert-Path 的宽松写法：盘符不存在时不炸，交给下面的存在性检查给出友好提示。
function Resolve-UserPath([string]$p) {
    if (-not $p) { return "" }
    try {
        $abs = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p)
        if ($abs) { return $abs }
    } catch {
        # 盘符或提供程序解析失败，原样返回，由后续 Test-Path 报错
    }
    return $p
}

if ($Python) { $Python = Resolve-UserPath $Python }
if ($GameDir) { $GameDir = Resolve-UserPath $GameDir }

if (-not $Python) {
    throw '找不到 Python。请设置环境变量 WOWS_PYTHON，或用 -Python 指定 python.exe 的完整路径。'
}

Write-Host "python   : $Python" -ForegroundColor DarkGray
Write-Host "game dir : $GameDir" -ForegroundColor DarkGray

foreach ($p in @($Blender, $Exporter, $ListShips, $RenderPy, $SanitizePy, $Python)) {
    if (-not (Test-Path $p)) { throw "missing dependency: $p" }
}
if (-not (Test-Path $GameDir)) {
    throw "game dir not found: $GameDir`n  请设置环境变量 WOWS_GAME_DIR，或用 -GameDir 指定游戏安装目录。"
}
New-Item -ItemType Directory -Force -Path $GlbDir, $OutRoot | Out-Null

# ------------------------------------------------------------------ ship list
Write-Host "Reading ship list from game files..." -ForegroundColor Cyan
$raw = & $ListShips -W $GameDir 2>&1 | Out-String
$shipList = @()
foreach ($line in ($raw -split "`r?`n")) {
    # columns: Key  Index  Nation  Type  Tier
    $m = [regex]::Match($line, '^\s*(\S+)\s+(\S+)\s+(.+?)\s{2,}(.+?)\s{2,}(\S+)\s*$')
    if ($m.Success -and $m.Groups[1].Value -ne "Key") {
        $shipList += [pscustomobject]@{
            Key    = $m.Groups[1].Value
            Nation = $m.Groups[3].Value.Trim()
            Type   = $m.Groups[4].Value.Trim()
            Tier   = $m.Groups[5].Value.Trim()
        }
    }
}
Write-Host "  $($shipList.Count) ships available"

# ------------------------------------------------------------------ selection
$targets = @()
if ($Everything) {
    $targets = $shipList
} else {
    if ($Ships.Count -gt 0) {
        foreach ($s in $Ships) {
            $hit = $shipList | Where-Object { $_.Key -eq $s -or $_.Key -like "*$s*" }
            if (-not $hit) { Write-Warning "ship not found: $s"; continue }
            $targets += $hit
        }
    } else {
        $targets = $shipList
    }
    if ($Nation) { $targets = $targets | Where-Object { $_.Nation -eq $Nation } }
    if ($Type)   { $targets = $targets | Where-Object { $_.Type   -eq $Type } }
    if ($Tier)   { $targets = $targets | Where-Object { $_.Tier   -eq $Tier } }
}
if (-not $targets -or $targets.Count -eq 0) { throw "no ships matched the selection" }
Write-Host "Selected $($targets.Count) ship(s)" -ForegroundColor Green

# ------------------------------------------------------------------ helpers
$nationMap = @{
    "United_Kingdom" = "UK"; "USA" = "USA"; "Russia" = "USSR"
}
$typeMap = @{
    "AirCarrier" = "AircraftCarriers"; "Battleship" = "Battleships"
    "Cruiser" = "Cruisers"; "Destroyer" = "Destroyers"; "Submarine" = "Submarines"
}

function Get-OutDir($ship) {
    $n = $ship.Nation
    if ($nationMap.ContainsKey($n)) { $n = $nationMap[$n] }
    $t = $ship.Type
    if ($typeMap.ContainsKey($t)) { $t = $typeMap[$t] }
    $tierNum = $ship.Tier
    $tierDir = switch ($tierNum) {
        "1"  { "Tier I" };    "2"  { "Tier II" };   "3"  { "Tier III" }
        "4"  { "Tier IV" };   "5"  { "Tier V" };    "6"  { "Tier VI" }
        "7"  { "Tier VII" };  "8"  { "Tier VIII" }; "9"  { "Tier IX" }
        "10" { "Tier X" };    "11" { "Tier Star" }
        default { "Tier $tierNum" }
    }
    # keep it filesystem-safe
    $tierDir = $tierDir -replace [regex]::Escape([string][char]0x2605), "Star"
    return Join-Path $OutRoot (Join-Path (Join-Path (Join-Path $n $t) $tierDir) $ship.Key)
}

function Write-Log($status, $ship, $note) {
    $line = (@((Get-Date).ToString("s"), $status, $ship.Key, $ship.Nation, $ship.Type, $ship.Tier, $note) -join "`t")
    Add-Content -Path $LogPath -Value $line -Encoding utf8
}

if (-not (Test-Path $LogPath)) {
    Set-Content -Path $LogPath -Value ("Time`tStatus`tShip`tNation`tType`tTier`tNote") -Encoding utf8
}

# ------------------------------------------------------------------ main loop
$idx = 0
$okCount = 0; $failCount = 0; $skipCount = 0

foreach ($ship in $targets) {
    $idx++
    $outDir = Get-OutDir $ship
    $mask = "RENDER $idx/$($targets.Count) $($ship.Key) [$($ship.Nation)/$($ship.Type)/T$($ship.Tier)]"

    $heroName = ($Views -split ",")[0].Trim()
    $heroPath = Join-Path $outDir "$heroName.png"
    if ($SkipExisting -and (Test-Path $heroPath)) {
        Write-Host "$mask -> skip (exists)" -ForegroundColor DarkGray
        $skipCount++
        continue
    }

    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
    $safeKey = $ship.Key -replace '[\\/:*?"<>|]', "_"
    $rawGlb  = Join-Path $GlbDir "$safeKey.raw.glb"
    $cleanGlb = Join-Path $GlbDir "$safeKey.glb"

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        # ---- 1. export
        Write-Host "$mask | exporting..." -ForegroundColor Cyan -NoNewline
        $expArgs = @("-W", $GameDir, "-s", $ship.Key, "-o", $rawGlb)
        if ($TexSize) { $expArgs += @("-Z", $TexSize) }
        $expOut = & $Exporter @expArgs 2>&1 | Out-String
        if (-not (Test-Path $rawGlb)) {
            throw "export failed: $(($expOut -split "`n" | Select-Object -Last 3) -join ' ')"
        }

        # ---- 2. sanitize
        Write-Host "`r$mask | sanitizing..." -ForegroundColor Cyan -NoNewline
        $sanOut = & $Python $SanitizePy $rawGlb $cleanGlb 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $cleanGlb)) {
            throw "sanitize failed: $sanOut"
        }

        # ---- 3. render
        Write-Host "`r$mask | rendering $Views ..." -ForegroundColor Yellow -NoNewline
        $blenderArgs = @(
            "-b", "--factory-startup", "-P", $RenderPy, "--",
            "--glb", $cleanGlb,
            "--out", $heroPath,
            "--res", $Res,
            "--samples", "$Samples",
            "--views", $Views,
            "--preset", $Preset
        )
        $rOut = & $Blender @blenderArgs 2>&1 | Out-String
        if ($rOut -notmatch "RENDER_OK") {
            throw "blender failed: $(($rOut -split "`n" | Where-Object { $_ -match 'Error|Traceback|RENDER_ERROR' } | Select-Object -Last 3) -join ' ')"
        }

        if (-not $KeepGlb) {
            Remove-Item $rawGlb, $cleanGlb -Force -ErrorAction SilentlyContinue
        }

        $sw.Stop()
        $secs = [math]::Round($sw.Elapsed.TotalSeconds, 1)
        Write-Host "`r$mask | OK in ${secs}s" -ForegroundColor Green
        Write-Log "OK" $ship "${secs}s"
        $okCount++
    }
    catch {
        $sw.Stop()
        Write-Host "`r$mask | FAILED" -ForegroundColor Red
        Write-Host "    $($_.Exception.Message)" -ForegroundColor DarkRed
        Write-Log "FAIL" $ship $_.Exception.Message
        $failCount++
        foreach ($f in @($rawGlb, $cleanGlb)) {
            Remove-Item $f -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host ""
Write-Host "Done. ok=$okCount skip=$skipCount fail=$failCount" -ForegroundColor Green
Write-Host "Output: $OutRoot"
Write-Host "Log:    $LogPath"

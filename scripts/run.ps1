<#
.SYNOPSIS
  「15 分钟生活圈」智能体检与规划助手 —— 一键启动（Windows PowerShell）

.DESCRIPTION
  复用或创建 backend/.venv → 校验/安装依赖 → 启动 uvicorn（可选同时启动前端）。

.EXAMPLE
  .\scripts\run.ps1 -Offline
  离线模式，读取 data/seed 快照，零 API 配额（默认，评审方首选）。

.EXAMPLE
  .\scripts\run.ps1 -Live -WithFrontend
  在线模式（.env 需填服务端 AK/SK 与浏览器端 AK），同时启动前端开发服务器。
#>
[CmdletBinding()]
param(
    [switch]$Offline,
    [switch]$Live,
    [switch]$Hybrid,
    [string]$BindHost = '',
    [int]$Port = 8000,
    [switch]$WithFrontend,
    [switch]$Install,
    [switch]$InstallFrontend,
    [switch]$SkipCheck
)

$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------------ 0. 基本环境
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path (Join-Path $Root 'backend'))) {
    throw "未找到 backend/ 目录。请把本脚本放在仓库的 scripts/ 下再运行。当前推断的仓库根目录：$Root"
}

$Mode = 'replay'
if ($Live)    { $Mode = 'live' }
if ($Hybrid)  { $Mode = 'hybrid' }
if ($Offline) { $Mode = 'replay' }
if ([string]::IsNullOrWhiteSpace($BindHost)) { $BindHost = '127.0.0.1' }

Write-Host '==============================================================' -ForegroundColor Cyan
Write-Host ' 15 分钟生活圈 · 一键启动' -ForegroundColor Cyan
Write-Host "   仓库根目录 : $Root"
Write-Host "   数据模式   : $Mode   (live=真实 API / replay=零配额快照 / hybrid=缓存优先)"
Write-Host "   监听地址   : http://${BindHost}:$Port"
Write-Host "   前端       : $(if ($WithFrontend) { '同时启动' } else { '不启动（仅后端 API）' })"
Write-Host '==============================================================' -ForegroundColor Cyan

# 本机实测：Windows 控制台默认 GBK，中文输出可能乱码；这里尽量切到 UTF-8。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# ------------------------------------------------------------------ 1. 解释器
$VenvDir = Join-Path $Root 'backend\.venv'
$PyCandidates = @(
    (Join-Path $VenvDir 'Scripts\python.exe'),
    (Join-Path $VenvDir 'bin/python')
)
$Py = $PyCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($Py) {
    Write-Host "[1/5] 复用已有虚拟环境：$VenvDir" -ForegroundColor Green
} else {
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if ($uv) {
        Write-Host "[1/5] 未发现虚拟环境，用 uv 创建：$VenvDir"
        & $uv venv $VenvDir
    } else {
        $sysPy = (Get-Command python -ErrorAction SilentlyContinue).Source
        if (-not $sysPy) { throw '找不到 Python 3.11+，也找不到 uv。请先安装 Python（https://www.python.org/downloads/）。' }
        Write-Host "[1/5] 未发现虚拟环境，用 python -m venv 创建：$VenvDir"
        & $sysPy -m venv $VenvDir
    }
    $Py = $PyCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Py) { throw "虚拟环境创建后仍未找到 python 解释器：$VenvDir" }
}

$PyVer = & $Py -c "import sys;print('.'.join(map(str,sys.version_info[:3])))"
Write-Host "      Python 版本：$PyVer"

# ------------------------------------------------------------------ 2. 依赖
$ReqFile = Join-Path $Root 'backend\requirements.txt'
$ProbeScript = @'
import importlib.util
mods = ["fastapi","uvicorn","pydantic","pydantic_settings","httpx","numpy","scipy","shapely","contourpy","pytest"]
print(",".join(m for m in mods if importlib.util.find_spec(m) is None))
'@
$ProbeFile = Join-Path $env:TEMP 'life_circle_dep_probe.py'
Set-Content -Path $ProbeFile -Value $ProbeScript -Encoding UTF8
try {
    $Missing = ((& $Py $ProbeFile) -join '').Trim()
} finally {
    Remove-Item -Force $ProbeFile -ErrorAction SilentlyContinue
}

if ($SkipCheck) {
    Write-Host '[2/5] 跳过依赖校验（-SkipCheck）' -ForegroundColor Yellow
} elseif ([string]::IsNullOrWhiteSpace($Missing)) {
    Write-Host '[2/5] 依赖完整，无需安装' -ForegroundColor Green
} else {
    Write-Host "[2/5] 缺失依赖：$Missing" -ForegroundColor Yellow
    if (-not $Install) {
        Write-Host '      为避免擅自改动你的环境，需要显式加 -Install 才会安装。例如：' -ForegroundColor Yellow
        Write-Host '        .\scripts\run.ps1 -Offline -Install'
        Write-Host "      （也可手动执行：uv pip install --python '$Py' -r '$ReqFile'）"
        exit 3
    }
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if ($uv) {
        & $uv pip install --python $Py -r $ReqFile
    } else {
        $hasPip = $true
        try { & $Py -m pip --version | Out-Null } catch { $hasPip = $false }
        if (-not $hasPip) {
            throw "该虚拟环境里没有 pip。请安装 uv 后重试：uv pip install --python '$Py' -r '$ReqFile'"
        }
        & $Py -m pip install -r $ReqFile
    }
}

# ------------------------------------------------------------------ 3. ASGI 入口探测
$EntryModule = ''
Push-Location (Join-Path $Root 'backend')
try {
    foreach ($cand in @('app.main:app', 'app.api.main:app', 'app.asgi:app', 'app.server:app', 'main:app')) {
        $mod = $cand.Split(':')[0]
        $code = "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$mod') else 1)"
        & $Py -c $code 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $EntryModule = $cand; break }
    }
} finally {
    Pop-Location
}

if ([string]::IsNullOrWhiteSpace($EntryModule)) {
    Write-Host ''
    Write-Host '[3/5] [!] 未在 backend/ 下探测到 FastAPI 应用入口。' -ForegroundColor Yellow
    Write-Host '      已尝试：app.main:app / app.api.main:app / app.asgi:app / app.server:app / main:app'
    Write-Host '      说明当前代码快照还没有 API 层（由并行子任务产出）。现在可以先验证算法本体：'
    Write-Host "        cd backend; & '$Py' scripts/selftest.py"
    Write-Host "        cd backend; & '$Py' bench/benchmark_isochrone.py"
    exit 4
}
Write-Host "[3/5] ASGI 入口：$EntryModule" -ForegroundColor Green

# ------------------------------------------------------------------ 4. 环境变量
$env:DATA_MODE = $Mode
$env:ENABLE_OFFLINE_REPLAY = $(if ($Mode -eq 'live') { 'false' } else { 'true' })
$env:BACKEND_HOST = $BindHost
$env:BACKEND_PORT = "$Port"
$env:PYTHONPATH = (Join-Path $Root 'backend')
Write-Host "[4/5] 环境变量已就绪（DATA_MODE=$Mode）"

if ($Mode -eq 'live' -and -not (Test-Path (Join-Path $Root '.env'))) {
    Write-Host '[警告] 在线模式但未发现 .env —— 请先 Copy-Item .env.example .env 并填入服务端 AK/SK。' -ForegroundColor Yellow
}
if ($Mode -eq 'live') {
    Write-Host '       配额提醒：地点检索 100/日、批量算路 5000 路线/日、QPS 3。请勿反复刷新分析结果。' -ForegroundColor Yellow
}

# ------------------------------------------------------------------ 5. 前端（可选）
if ($WithFrontend) {
    $FeDir = Join-Path $Root 'frontend'
    $FePkg = Join-Path $FeDir 'package.json'
    if (-not (Test-Path $FePkg)) {
        Write-Host '[警告] 未发现 frontend/package.json，跳过前端。' -ForegroundColor Yellow
    } else {
        $pm = 'npm'
        if (Test-Path (Join-Path $FeDir 'pnpm-lock.yaml')) { $pm = 'pnpm' }
        $needInstall = -not (Test-Path (Join-Path $FeDir 'node_modules'))
        if ($needInstall -and -not $InstallFrontend) {
            Write-Host "[警告] frontend/node_modules 不存在，跳过前端（加 -InstallFrontend 允许执行 $pm install）。" -ForegroundColor Yellow
        } else {
            if ($needInstall) { & $pm install --prefix $FeDir }
            Write-Host "[5/5] 启动前端开发服务器（$pm run dev）..." -ForegroundColor Green
            Start-Process -FilePath $pm -ArgumentList 'run', 'dev' -WorkingDirectory $FeDir | Out-Null
        }
    }
}

# ------------------------------------------------------------------ 6. uvicorn
Write-Host "[5/5] 启动后端：uvicorn $EntryModule --app-dir $Root\backend" -ForegroundColor Green
Write-Host "      打开浏览器访问 http://${BindHost}:$Port   （Ctrl+C 停止）" -ForegroundColor Green
Write-Host ''

# 端口占用预检：避免 uvicorn 静默失败（Windows 上通常报 [Errno 10048] address already in use）
$portBusy = $false
try {
    $portBusy = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
} catch {
    $portBusy = [bool](netstat -ano | Select-String -Pattern (":$Port\s+.*LISTENING"))
}
if ($portBusy) {
    Write-Host "[错误] 端口 $Port 已被占用。换一个端口即可，例如：" -ForegroundColor Red
    Write-Host "        .\scripts\run.ps1 -Offline -Port 8001"
    Write-Host "       查看占用进程：Get-NetTCPConnection -LocalPort $Port | Select-Object OwningProcess"
    exit 5
}

Set-Location (Join-Path $Root 'backend')
& $Py -m uvicorn $EntryModule --app-dir (Join-Path $Root 'backend') --host $BindHost --port $Port --proxy-headers

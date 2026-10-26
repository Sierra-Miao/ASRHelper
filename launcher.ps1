param(
    [switch]$NoInstall,
    [switch]$NoBrowser,
    [switch]$CheckOnly,
    [switch]$SetupOnly,
    [ValidateSet('auto', 'cpu')][string]$Device = 'auto',
    [ValidateSet('nano', 'sensevoice', 'paraformer', 'ensemble')][string]$Model,
    [ValidateRange(1024, 65525)][int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:UV_CACHE_DIR = Join-Path $PSScriptRoot '.uv-cache'
$env:FUNASR_DEVICE = $Device
if ($Model) { $env:FUNASR_ASR_MODEL = $Model }
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$approved = $false

function Confirm-Install([string]$Message) {
    if ($NoInstall) { throw '依赖或模型不完整；NoInstall 模式不会下载或修改环境。' }
    Write-Host ''
    Write-Host $Message -ForegroundColor Yellow
    $answer = Read-Host '是否允许安装/下载？输入 Y 继续，其他输入取消'
    if ($answer -notmatch '^(y|yes)$') {
        Write-Host '已取消。没有开始安装。'
        exit 2
    }
}

function Find-Uv {
    $localUv = Join-Path $PSScriptRoot '.tools\uv\uv.exe'
    if (Test-Path -LiteralPath $localUv) { return $localUv }
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Install-LocalUv {
    $architecture = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'aarch64' } else { 'x86_64' }
    $toolDir = Join-Path $PSScriptRoot '.tools\uv'
    New-Item -ItemType Directory -Force -Path $toolDir | Out-Null
    $archiveName = "uv-$architecture-pc-windows-msvc.zip"
    $url = "https://github.com/astral-sh/uv/releases/download/0.9.11/$archiveName"
    $archive = Join-Path $toolDir $archiveName
    Write-Host '正在下载项目专用 uv（官方发布），并验证校验值…'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive
    $checksum = (Invoke-WebRequest -UseBasicParsing -Uri "$url.sha256").Content
    if ($checksum -is [byte[]]) { $checksum = [Text.Encoding]::UTF8.GetString($checksum) }
    $match = [regex]::Match([string]$checksum, '[a-fA-F0-9]{64}')
    if (-not $match.Success) { throw '无法读取 uv 官方校验值，已停止安装。' }
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
    if ($actual -ne $match.Value) { throw 'uv 文件校验失败，已停止安装。' }
    Expand-Archive -LiteralPath $archive -DestinationPath $toolDir -Force
    $binary = Get-ChildItem -LiteralPath $toolDir -Filter uv.exe -Recurse | Select-Object -First 1
    if (-not $binary) { throw 'uv 解压失败。' }
    return $binary.FullName
}

function Install-ProjectDependencies([string]$Uv, [string]$Backend, [bool]$ReplaceTorch) {
    $requirements = if (Test-Path -LiteralPath 'requirements.lock.txt') { 'requirements.lock.txt' } else { 'requirements.in' }
    $installArgs = @('pip', 'install', '--python', $python, '--torch-backend', $Backend, '-r', (Join-Path $PSScriptRoot $requirements))
    if ($ReplaceTorch) { $installArgs += @('--reinstall-package', 'torch', '--reinstall-package', 'torchaudio') }
    Write-Host "正在安装匹配硬件的依赖：$Backend"
    & $Uv @installArgs
    if ($LASTEXITCODE -eq 0) { return }
    if ($Backend -ne 'cpu') {
        Write-Host 'CUDA 依赖安装失败，尝试使用 CPU 依赖继续。' -ForegroundColor Yellow
        & $Uv pip install --python $python --torch-backend cpu --reinstall-package torch --reinstall-package torchaudio -r (Join-Path $PSScriptRoot $requirements)
        if ($LASTEXITCODE -eq 0) {
            $env:FUNASR_DEVICE = 'cpu'
            return
        }
    }
    throw '依赖安装失败，请检查网络后重新启动。'
}

try {
    if ($CheckOnly) {
        if (-not (Test-Path -LiteralPath $python)) {
            Write-Output '{"ready":false,"reason":"local_environment_missing"}'
            exit 1
        }
        & $python (Join-Path $PSScriptRoot 'scripts\check_environment.py')
        exit $LASTEXITCODE
    }
    Write-Host '听笺 · 本地语音工作台' -ForegroundColor Green
    Write-Host '正在检查项目目录中的 Python、FunASR、网页依赖和模型…'
    $report = $null
    if (Test-Path -LiteralPath $python) {
        $reportText = & $python (Join-Path $PSScriptRoot 'scripts\check_environment.py')
        if ($reportText) {
            try { $report = $reportText | ConvertFrom-Json } catch { $report = $null }
        }
    }
    if ($report -and $report.hardware) {
        Write-Host ("处理器：{0}；{1} 核 / {2} 线程" -f $report.hardware.cpu_name, $report.hardware.physical_cores, $report.hardware.logical_cores)
        Write-Host ("显卡：{0}" -f ($report.hardware.gpu_names -join ', '))
        Write-Host $report.hardware.reason
    }
    if (-not $report -or -not $report.python_ok -or -not $report.packages_ok -or -not $report.ffmpeg_ok) {
        if ($report -and $report.missing) {
            Write-Host '缺失或版本不匹配的依赖：'
            $report.missing | ForEach-Object { Write-Host "  $_" }
        }
        Confirm-Install '需要创建/修复本目录的独立 Python 3.12 环境，自动按硬件选择 CPU/CUDA 依赖。模型可在网页中选择并确认下载；不会更改全局 Python 包。'
        $approved = $true
        $uv = Find-Uv
        if (-not $uv) { $uv = Install-LocalUv }
        if (-not $report -or -not $report.python_ok) {
            if (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.venv')) {
                $existing = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '.venv'))
                $rootPath = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'
                if (-not $existing.StartsWith($rootPath, [StringComparison]::OrdinalIgnoreCase)) { throw '环境路径不在项目目录中。' }
                $backup = '.venv.backup-' + (Get-Date -Format 'yyyyMMddHHmmss')
                Rename-Item -LiteralPath $existing -NewName $backup
                Write-Host "原有环境已保留为 $backup"
            }
            $env:UV_PYTHON_INSTALL_DIR = Join-Path $PSScriptRoot '.tools\python'
            & $uv venv --python 3.12 (Join-Path $PSScriptRoot '.venv')
            if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败，请检查网络。' }
        }
        $hardware = (& $python (Join-Path $PSScriptRoot 'hardware.py') --json) | ConvertFrom-Json
        Write-Host $hardware.reason
        $replaceTorch = $hardware.backend -ne 'cpu' -and (-not $report -or -not $report.acceleration_ok)
        Install-ProjectDependencies $uv $hardware.backend $replaceTorch
        $report = (& $python (Join-Path $PSScriptRoot 'scripts\check_environment.py')) | ConvertFrom-Json
        if (-not $report.packages_ok -or -not $report.ffmpeg_ok -or -not $report.python_ok) { throw '环境检查仍未通过，请查看上方提示。' }
    }
    elseif (-not $report.acceleration_ok -and $report.hardware.backend -ne 'cpu') {
        if ($NoInstall) {
            Write-Host '当前依赖可运行；NoInstall 模式保留现有依赖，暂不切换 CUDA。' -ForegroundColor Yellow
        } else {
            $answer = Read-Host '检测到可加速的 NVIDIA 显卡，但当前依赖不匹配。是否安装对应 CUDA 依赖？Y 安装，其他输入保留当前环境继续'
            if ($answer -match '^(y|yes)$') {
                $uv = Find-Uv
                if (-not $uv) { $uv = Install-LocalUv }
                Install-ProjectDependencies $uv $report.hardware.backend $true
            } else {
                Write-Host '已保留当前依赖，运行时将验证可用设备。'
            }
        }
    }
    if (-not $report.models_ready) {
        if ($SetupOnly) {
            if (-not $approved) { Confirm-Install '所选语音模型尚未下载齐全。将从官方模型仓库下载到本目录；下载量取决于所选模型。' }
            & $python (Join-Path $PSScriptRoot 'transcribe.py') --prepare
            if ($LASTEXITCODE -ne 0) { throw '模型准备失败，请检查网络后重新启动。' }
        } elseif ($NoInstall) {
            throw '所选模型缺失；NoInstall 模式不会下载模型。可不带此参数启动后在网页选择已下载模型。'
        } else {
            Write-Host '所选语音模型尚未准备好，请在网页“语音模型”中选择并点击“下载并切换”。' -ForegroundColor Yellow
        }
    }
    Write-Host '环境检查通过。' -ForegroundColor Green
    if ($SetupOnly) { exit 0 }
    $projectTemp = Join-Path $PSScriptRoot 'data\tmp'
    New-Item -ItemType Directory -Force -Path $projectTemp | Out-Null
    $env:TEMP = $projectTemp
    $env:TMP = $projectTemp
    Write-Host '正在启动本地网页。保持此窗口打开，按 Ctrl+C 停止服务。'
    $serverArgs = @((Join-Path $PSScriptRoot 'run.py'), '--port', $Port)
    if ($NoBrowser) { $serverArgs += '--no-browser' }
    & $python @serverArgs
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}

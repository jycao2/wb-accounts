# wb-accounts 一键安装 / 升级脚本（Windows PowerShell 5.1+）
#
# 用法：
#   双击 install.cmd                     （最简单，推荐）
#   powershell -ExecutionPolicy Bypass -File install.ps1
#   powershell -ExecutionPolicy Bypass -File install.ps1 -SkillsRoot "D:\skills"
#   powershell -ExecutionPolicy Bypass -File install.ps1 -SkipDoctor
#
# 它做四件事：
#   1. 把 wb-accounts 安装到 %USERPROFILE%\.workbuddy\skills\wb-accounts\
#   2. 把 vendor\workbuddy-daily-claim 安装到 ...\skills\workbuddy-daily-claim\
#   3. 保留已有 state\（快照 / 令牌 / 别名不会被覆盖）
#   4. 跑一次 doctor 自检，把缺什么直接打出来
#
# 重复执行 = 升级，不会破坏已有账号数据。

[CmdletBinding()]
param(
    [string]$SkillsRoot = "",
    [switch]$SkipDoctor
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($SkillsRoot)) {
    $SkillsRoot = Join-Path $env:USERPROFILE ".workbuddy\skills"
}

$repoRoot = $PSScriptRoot

function Write-Step([string]$text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Write-Ok([string]$text) {
    Write-Host "    [OK]   $text" -ForegroundColor Green
}

function Write-Warn2([string]$text) {
    Write-Host "    [WARN] $text" -ForegroundColor Yellow
}

# 复制单个条目（文件或目录）到目标目录，目录内容合并而非整体替换
function Install-Item {
    param(
        [string]$SourceRoot,
        [string]$DestRoot,
        [string]$Name
    )
    $src = Join-Path $SourceRoot $Name
    if (-not (Test-Path $src)) {
        return $false
    }
    if (Test-Path $src -PathType Container) {
        $destDir = Join-Path $DestRoot $Name
        if (-not (Test-Path $destDir)) {
            New-Item -ItemType Directory -Path $destDir -Force | Out-Null
        }
        Copy-Item -Path (Join-Path $src "*") -Destination $destDir -Recurse -Force
    }
    else {
        Copy-Item -Path $src -Destination (Join-Path $DestRoot $Name) -Force
    }
    return $true
}

# 找 Python：优先 WorkBuddy 自带运行时，其次 PATH
function Find-Python {
    $candidates = @()
    $pyVersions = Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions"
    if (Test-Path $pyVersions) {
        foreach ($dir in (Get-ChildItem -Path $pyVersions -Directory -ErrorAction SilentlyContinue)) {
            $candidates += (Join-Path $dir.FullName "python.exe")
            $candidates += (Join-Path $dir.FullName "pythonw.exe")
        }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $cmd) {
        $candidates += $cmd.Source
    }
    foreach ($c in $candidates) {
        if (Test-Path $c) {
            return $c
        }
    }
    return ""
}

Write-Host ""
Write-Host "wb-accounts 安装程序" -ForegroundColor White
Write-Host "  源目录：$repoRoot"
Write-Host "  技能目录：$SkillsRoot"

if (-not (Test-Path (Join-Path $repoRoot "scripts\wb_accounts.py"))) {
    Write-Host ""
    Write-Host "[错误] 没有在 $repoRoot 找到 scripts\wb_accounts.py" -ForegroundColor Red
    Write-Host "       请把 install.cmd / install.ps1 放在 wb-accounts 仓库根目录再运行。" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $SkillsRoot)) {
    New-Item -ItemType Directory -Path $SkillsRoot -Force | Out-Null
    Write-Step "已创建技能目录"
    Write-Ok $SkillsRoot
}

# ---------- 1. wb-accounts ----------
$wbDest = Join-Path $SkillsRoot "wb-accounts"
$isUpgrade = Test-Path (Join-Path $wbDest "scripts\wb_accounts.py")

Write-Step ($(if ($isUpgrade) { "升级 wb-accounts（保留 state\）" } else { "安装 wb-accounts" }))
New-Item -ItemType Directory -Path $wbDest -Force | Out-Null
foreach ($item in @("scripts", "bin", "references", "SKILL.md", "README.md", "LICENSE", "CHANGELOG.md")) {
    if (Install-Item -SourceRoot $repoRoot -DestRoot $wbDest -Name $item) {
        Write-Ok $item
    }
}
# state\ 只建目录，绝不清空
$wbState = Join-Path $wbDest "state"
if (-not (Test-Path $wbState)) {
    New-Item -ItemType Directory -Path $wbState -Force | Out-Null
}

# ---------- 2. workbuddy-daily-claim ----------
$vendorRoot = Join-Path $repoRoot "vendor\workbuddy-daily-claim"
$claimDest = Join-Path $SkillsRoot "workbuddy-daily-claim"

if (Test-Path $vendorRoot) {
    $claimExists = Test-Path (Join-Path $claimDest "scripts\claim.py")
    Write-Step ($(if ($claimExists) { "升级 workbuddy-daily-claim（保留 state\）" } else { "安装 workbuddy-daily-claim" }))
    New-Item -ItemType Directory -Path $claimDest -Force | Out-Null
    foreach ($item in @("scripts", "SKILL.md")) {
        if (Install-Item -SourceRoot $vendorRoot -DestRoot $claimDest -Name $item) {
            Write-Ok $item
        }
    }
    $claimState = Join-Path $claimDest "state"
    if (-not (Test-Path $claimState)) {
        New-Item -ItemType Directory -Path $claimState -Force | Out-Null
    }
}
else {
    Write-Step "跳过 workbuddy-daily-claim"
    Write-Warn2 "vendor\workbuddy-daily-claim 不存在（签到/积分将不可用）"
}

# ---------- 3. 环境自检 ----------
$py = Find-Python
if ($py -eq "") {
    Write-Step "环境自检"
    Write-Warn2 "没找到 Python。wb-accounts 需要 Python 3.8+"
    Write-Warn2 "装上 WorkBuddy 客户端即可（自带运行时），或自行安装 Python 并加入 PATH"
}
else {
    Write-Ok "Python: $py"
    if (-not $SkipDoctor) {
        Write-Step "运行 doctor 自检"
        $doctorScript = Join-Path $wbDest "scripts\wb_accounts.py"
        try {
            & $py $doctorScript doctor
        }
        catch {
            Write-Warn2 "doctor 执行失败：$($_.Exception.Message)"
        }
    }
}

# ---------- 4. 收尾 ----------
Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
Write-Host ""
Write-Host "接下来（三步）：" -ForegroundColor White
Write-Host "  1. 打开 WorkBuddy 客户端并登录你的账号（每个要管的号各登一次）"
Write-Host "  2. 启动面板：双击 $wbDest\bin\open-dashboard.vbs"
Write-Host "     （面板会自动采集当前登录号、并尝试自动配置令牌通道）"
Write-Host "  3. 需要签到/查积分时，在面板上点对应按钮即可"
Write-Host ""
Write-Host "命令行用法：" -ForegroundColor White
Write-Host "  $py $wbDest\scripts\wb_accounts.py --help"
Write-Host ""
Write-Host "注意：" -ForegroundColor Yellow
Write-Host "  * 面板只监听 127.0.0.1，令牌与快照用本机 Windows 账户加密，无法跨机器复制"
Write-Host "  * 换电脑后需要在新机器上重新登录客户端 → 采集一次"
Write-Host "  * 不要把 state\ 目录提交到任何仓库或发给别人"
Write-Host ""

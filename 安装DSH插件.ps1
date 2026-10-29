﻿# 安装大肥鱼桌宠的 DSH 插件（可移植版）
# 用法：双击「安装DSH插件.bat」，或右键 -> 使用 PowerShell 运行
# 作用：把本目录的 dsh-plugin 登记为 DSH profile 的依赖 + bundle，并在 cordis.patch.yml 里写配置
#
# 设计目标：整个「大肥鱼桌宠」文件夹复制到任何电脑、任何盘符、任何路径，本脚本都能用。
# 不硬编码任何绝对路径：项目位置自算，DSH 安装位置自动探测。

$ErrorActionPreference = 'Stop'

# ---------- 0) 自算路径 ----------
$here = if ($PSScriptRoot) { $PSScriptRoot }
        elseif ($MyInvocation.MyCommand.Path) { Split-Path -Parent $MyInvocation.MyCommand.Path }
        else { (Get-Location).Path }
$here = (Resolve-Path $here).Path
$pluginSrc = Join-Path $here 'dsh-plugin'

Write-Host "==== 大肥鱼 DSH 插件安装 ====" -ForegroundColor Cyan
Write-Host "安装目录: $here"

if (-not (Test-Path $pluginSrc)) {
    Write-Host "[错误] 找不到 dsh-plugin 目录: $pluginSrc" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $pluginSrc 'package.json'))) {
    Write-Host "[错误] dsh-plugin 里没有 package.json" -ForegroundColor Red
    exit 1
}

# ---------- 1) 找 DSH profile ----------
# 依次尝试 ~/.dsh/profiles/<name> 与 %APPDATA%/.dsh/profiles/<name>
$profileDir = $null
foreach ($name in @('desktop', 'web')) {
    foreach ($root in @((Join-Path $env:USERPROFILE ".dsh\profiles\$name"),
                        (Join-Path $env:APPDATA      ".dsh\profiles\$name"))) {
        if (Test-Path (Join-Path $root 'package.json')) { $profileDir = $root; break }
        if (Test-Path $root) { $profileDir = $root; break }
    }
    if ($profileDir) { break }
}
if (-not $profileDir) {
    Write-Host "[错误] 没找到 DSH profile 目录（请先运行一次 DSH）" -ForegroundColor Red
    exit 1
}
Write-Host "DSH profile: $profileDir"

# ---------- 2) 找 node / pnpm（优先 DSH 自带） ----------
# 已知布局：<安装根>\resources\runtime\primary-runtime\dependencies\{node,pnpm}\...
# 兜底思路：从 dsh.cmd 的位置（<安装根>\resources\runtime\cli\bin\dsh.cmd）往上 4 级到 <安装根>\resources。
# 注意：<安装根>\resources 就是终点，不能再拼一层 resources（2026-10-19 实测踩过）。
function Get-DshResourcesRoot {
    $dsh = Get-Command dsh.cmd -ErrorAction SilentlyContinue
    if ($dsh -and (Test-Path $dsh.Source)) {
        # dsh.cmd -> bin -> cli -> runtime -> resources
        return (Split-Path (Split-Path (Split-Path (Split-Path (Resolve-Path $dsh.Source).Path))))
    }
    return $null
}
function Find-Node {
    $c = @()
    $c += Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness\resources\runtime\primary-runtime\dependencies\node\bin\node.exe'
    $rr = Get-DshResourcesRoot
    if ($rr) { $c += Join-Path $rr 'runtime\primary-runtime\dependencies\node\bin\node.exe' }
    $sys = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($sys) { $c += $sys.Source }
    foreach ($x in $c) { if ($x -and (Test-Path $x)) { return (Resolve-Path $x).Path } }
    return $null
}
function Find-Pnpm {
    $c = @()
    $c += Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness\resources\runtime\primary-runtime\dependencies\pnpm\bin\pnpm.cjs'
    $rr = Get-DshResourcesRoot
    if ($rr) { $c += Join-Path $rr 'runtime\primary-runtime\dependencies\pnpm\bin\pnpm.cjs' }
    $sys = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
    if ($sys) { $c += $sys.Source }
    foreach ($x in $c) { if ($x -and (Test-Path $x)) { return (Resolve-Path $x).Path } }
    return $null
}

$nodeExe = Find-Node
$pnpmExe = Find-Pnpm

# node 与 pnpm 只要各找到一个就能跑；pnpm.cjs 是 JS，用任意 node 都能执行。
# 找不到 DSH 自带的就退到系统的，都没有才报错。
if (-not $nodeExe) {
    Write-Host "[错误] 找不到 node.exe。" -ForegroundColor Red
    Write-Host "       已尝试：DSH 自带 node、系统 PATH 上的 node。" -ForegroundColor Yellow
    Write-Host "       解决：装一次 DSH（自带 node/pnpm），或安装 Node.js 并加入 PATH。" -ForegroundColor Yellow
    exit 1
}
if (-not $pnpmExe) {
    Write-Host "[错误] 找不到 pnpm。" -ForegroundColor Red
    Write-Host "       已尝试：DSH 自带 pnpm、系统 PATH 上的 pnpm。" -ForegroundColor Yellow
    Write-Host "       解决：装一次 DSH（自带 pnpm），或 npm install -g pnpm。" -ForegroundColor Yellow
    exit 1
}
Write-Host "node : $nodeExe"
Write-Host "pnpm : $pnpmExe"

# 组合自检：用选中的 node 实际执行一次 pnpm --version，确认能跑
try {
    $ver = & $nodeExe $pnpmExe --version 2>&1
    if ($LASTEXITCODE -ne 0 -or -not $ver) { throw "pnpm --version 返回码 $LASTEXITCODE" }
    Write-Host "pnpm 版本: $ver（自检通过）" -ForegroundColor Green
} catch {
    Write-Host "[错误] 选中的 node/pnpm 组合跑不起来: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "       node=$nodeExe" -ForegroundColor Yellow
    Write-Host "       pnpm=$pnpmExe" -ForegroundColor Yellow
    Write-Host "       通常是系统 node 版本太旧。请改用 DSH 自带的 node。" -ForegroundColor Yellow
    exit 1
}

# ---------- 3) 备份 profile 的三个文件 ----------
$stamp = Get-Date -Format 'HHmmss'
foreach ($f in @('package.json','pnpm-lock.yaml','cordis.patch.yml')) {
    $p = Join-Path $profileDir $f
    if (Test-Path $p) { Copy-Item -LiteralPath $p -Destination "$p.bak-$stamp" -Force }
}

# ---------- 4) 登记为 profile 依赖（pnpm add link:） ----------
# pnpm 自己建 Junction、写 dependencies、写 pnpm-lock.yaml。
# 这样才不是"野目录"，pnpm 后续 install/prune 都不会清掉它。
#
# 注意：link 目标是绝对路径。若项目放在中文目录（如 D:\大肥鱼桌宠），
# package.json 与 pnpm-lock.yaml 里会含中文。pnpm 读写都是 UTF-8，实测正常；
# 跨盘符无法改用相对路径（Resolve-Path -Relative 会生成假路径）。
$linkSpec = 'link:' + $pluginSrc
Push-Location $profileDir
try {
    # 先试 --offline：link: 是本地依赖，本来就不需要联网。
    # 对方电脑没网时这一步能直接过；失败（如缓存缺失）再回退在线。
    Write-Host "`n---- pnpm add $linkSpec ----" -ForegroundColor Cyan
    & $nodeExe $pnpmExe add $linkSpec --offline 2>&1 | ForEach-Object { Write-Host "    $_" }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  --offline 失败，回退在线模式重试" -ForegroundColor Yellow
        & $nodeExe $pnpmExe add $linkSpec 2>&1 | ForEach-Object { Write-Host "    $_" }
        if ($LASTEXITCODE -ne 0) { throw "pnpm add 失败，退出码 $LASTEXITCODE" }
    }
} finally { Pop-Location }
Write-Host "已登记为 profile 依赖" -ForegroundColor Green

# 登记结果自检：Junction 是否建好、目标是否可达
$nmDir = Join-Path $profileDir 'node_modules'
$linkDir = Join-Path $nmDir 'dsh-pet-pyside6'
if (Test-Path $linkDir) {
    $li = Get-Item $linkDir -Force
    $ok = Test-Path $linkDir
    if ($ok) {
        Write-Host "链接校验通过: $linkDir -> $($li.Target)" -ForegroundColor Green
    } else {
        Write-Host "[错误] 链接存在但目标不可达（LinkType=$($li.LinkType), Target=$($li.Target)）" -ForegroundColor Red
        Write-Host "       这通常是中文路径在不同 locale 的 Windows 上被转码导致。" -ForegroundColor Yellow
        Write-Host "       解决办法：把「大肥鱼桌宠」文件夹移到纯英文路径（如 D:\bigfish-pet）再重跑本脚本。" -ForegroundColor Yellow
        exit 1
    }
} else {
    Write-Host "[错误] pnpm 没有创建 $linkDir" -ForegroundColor Red
    exit 1
}

# ---------- 5) 登记到 dsh.profile.bundles ----------
# cordis 只加载 bundles 列表里的包；不登记则插件栏不显示、配置 schema 不生效。
$pkgPath = Join-Path $profileDir 'package.json'
$pkgText = [IO.File]::ReadAllText($pkgPath, [Text.Encoding]::UTF8)
$pkgChanged = $false

if ($pkgText -notmatch '"dsh-pet-pyside6"') {
    if ($pkgText -match '(?s)("bundles"\s*:\s*\[)(.*?)(\s*\])') {
        $m = [regex]::Match($pkgText, '(?s)("bundles"\s*:\s*\[)(.*?)(\s*\])')
        $body = $m.Groups[2].Value.TrimEnd().TrimEnd(',')
        if ($body.Length -gt 0) { $body += ',' }
        $body += "`n        `"dsh-pet-pyside6`""
        $new = $m.Groups[1].Value + "`n" + $body + "`n      ]"
        $pkgText = $pkgText.Substring(0, $m.Index) + $new + $pkgText.Substring($m.Index + $m.Length)
        $pkgChanged = $true
        Write-Host "已加入 dsh.profile.bundles" -ForegroundColor Green
    } else {
        # 没有 dsh.profile.bundles 段：补一个（在最后一个 } 前插入）
        if ($pkgText -match '(?s)^\s*\{') {
            $pkgText = [regex]::Replace($pkgText, '\}\s*$', @'
,

  "dsh": {
    "profile": {
      "bundles": [
        "dsh-pet-pyside6"
      ]
    }
  }
}
'@)
            $pkgChanged = $true
            Write-Host "已新建 dsh.profile.bundles 并登记插件" -ForegroundColor Green
        } else {
            Write-Host "[警告] package.json 格式不认识，请手工把 dsh-pet-pyside6 加进 dsh.profile.bundles" -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "dsh.profile.bundles 已包含插件，跳过" -ForegroundColor Green
}

if ($pkgChanged) {
    # JSON 合法性自检，失败则回滚
    try {
        $null = $pkgText | ConvertFrom-Json
        [IO.File]::WriteAllText($pkgPath, $pkgText, [Text.UTF8Encoding]::new($false))
        Write-Host "package.json JSON 校验通过" -ForegroundColor Green
    } catch {
        Write-Host "[错误] package.json 不是合法 JSON，已回滚（备份在同目录 .bak-$stamp）" -ForegroundColor Red
        throw
    }
}

# ---------- 6) cordis.patch.yml 写插件配置 ----------
$patch = Join-Path $profileDir 'cordis.patch.yml'
$txt = if (Test-Path $patch) { [IO.File]::ReadAllText($patch, [Text.Encoding]::UTF8) } else { "# Your patch layer for this dsh profile.`r`n[]`r`n" }

if ($txt -match 'dsh-pet-pyside6') {
    # 已存在：只更新 projectDir（复制到别的电脑后项目目录变了）
    $txt = [regex]::Replace($txt, "(projectDir:\s*)'[^']*'", "`$1'$here'")
    [IO.File]::WriteAllText($patch, $txt, [Text.UTF8Encoding]::new($false))
    Write-Host "已更新 cordis.patch.yml 的 projectDir -> $here" -ForegroundColor Green
} else {
    $block = @"

# 大肥鱼桌宠（pet-pyside6）
- id: dsh-pet-pyside6
  name: dsh-pet-pyside6
  config:
    enabled: true
    autoStart: true
    projectDir: '$here'
    port: 3080
    balanceEnabled: true
    balanceSource: auto
    forwardEvents: true
"@
    $merged = $txt.TrimEnd() + "`n" + $block + "`n"
    [IO.File]::WriteAllText($patch, $merged, [Text.UTF8Encoding]::new($false))
    Write-Host "已在 cordis.patch.yml 登记插件配置" -ForegroundColor Green
}

# ---------- 7) 结果 ----------
$title = 'PySide6 桌宠'
$localeFile = Join-Path $pluginSrc 'locale\zh.json'
if (Test-Path $localeFile) {
    try { $title = (Get-Content $localeFile -Raw | ConvertFrom-Json).meta.title } catch {}
}

Write-Host ""
Write-Host "==== 安装完成 ====" -ForegroundColor Cyan
Write-Host "1. 完全退出 DSH（托盘右键退出，或结束所有 DeepSeek Harness 进程）"
Write-Host "2. 重新启动 DSH"
Write-Host "3. 设置 -> 插件 里应出现「$title」"
Write-Host ""
Write-Host "之后 DSH 启动会自动拉起桌宠；也可以双击 启动桌宠.bat 独立运行。"
Write-Host "（已在运行时会自动复用，不会出现两只）"

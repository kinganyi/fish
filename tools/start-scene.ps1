#requires -Version 5.1
<#
    fish 现场 · 一键启动器            tools/start-scene.ps1

    ## 这个文件解决的问题

    「看一眼现场」目前要三条手工活：起 API（8790）、起静态 UI（8799）、自己开浏览器。
    手工做会错在四处：漏起一边、端口记错、重复双击起出第二份互相抢端口、关不掉留下孤儿
    python。Electron 壳还在路上，所以先要一个**双击一次就看到界面、再双击不会起第二份**的入口。

    ## 做法（先判后做，与 p0/ 的「先证明再动手」一致）

    1. **两端口各自判三态**：running / free / conflict。
       - running：端点 200 **且内容认得出是本服务** → 复用，不重启（不重复起第二份）。
       - free：才起。
       - conflict：端口有人在听但内容不是本服务 → **一个字节都不动**，打印占用者 pid + 命令行，退出码 2。
    2. **合并日志用 cmd 外壳**：PS 的 Start-Process 不允许 stdout/stderr 指向同一个文件
       （实测原文：无法运行此命令，因为"RedirectStandardOutput"和"RedirectStandardError"相同），
       而 python 的启动横幅和请求日志都写在 **stderr** —— 拆成两个文件时 stdout 那份是空的，
       「日志有内容」这条验收会假过。所以用 `cmd /c ""py" args > x.log 2>&1"` 把两股流合成一个文件。
    3. **PID 只留自己起的，且以端口占用者为准**：`.scene-pids.json` 记的是两个 **python** 的 pid
       （不是 cmd 外壳的）。这个 pid **不从「等 cmd 的子进程」拿** —— 本机实测 CIM 查询 ~0.9s/次、
       python 子进程可见延迟 1~7s，用「等子进程」判「起没起来」会把「正在起」误判成「起不来」
       （第一版实现真踩过：UI 其实已在 8799 上服务，脚本却报「没能起来」，进而把刚起的 API 也收掉）。
       改成：谁在听这个端口，谁就是服务 —— 就绪之后按端口占用者取 pid，并复核命令行特征。
    4. **停止逐个复核身份**：PID 会被系统复用，光凭数字 `Stop-Process` 就是误杀别人的入口；
       全脚本没有一处 `taskkill /IM`。
    5. **不静默**：任何一步失败都打印「哪个端点、哪个 URL、日志尾、进程还活着吗」，并给非零退出码。

    ## 编码约定（本机踩出来的，不是模板）

    本文件必须是 **UTF-8 with BOM**。PS 5.1 读无 BOM 的 .ps1 会按本地代码页(936)解码，
    脚本里的中文会全变成 `鎺㈤拡涓枃` 那类乱码（实测对照：无 BOM → 乱码；有 BOM → 正常）。
    两个 .cmd 保持纯 ASCII，所以它们无 BOM 也安全。

    另：本脚本会把 `[Console]::OutputEncoding` 设成 UTF-8（为了读子进程输出不乱码），
    所以**请经 .cmd 调用**（它先 `chcp 65001`，两边才对得上）。在没切代码页的 936 控制台里
    直接 `powershell -File start-scene.ps1`，中文提示会花屏 —— 功能不受影响，但那不是支持的入口。

    ## 用法

        双击 start-scene.cmd（或桌面「fish 现场.lnk」）      启动 / 复用 / 只开浏览器
        双击 stop-scene.cmd                                 停止（只杀留痕里的那两个 pid）

        脚本直调（自测用，不弹浏览器、不等人回车）：
        powershell -NoProfile -ExecutionPolicy Bypass -File tools\start-scene.ps1 -NoPause -NoBrowser
        powershell -NoProfile -ExecutionPolicy Bypass -File tools\start-scene.ps1 -Stop -NoPause

    ## 端口可配置（换端口 = 换一对，留痕按端口复核）

        -ApiPort <n> / -UiPort <n>        显式指定（默认 8790 / 8799）
        环境变量 FISH_SCENE_API_PORT / FISH_SCENE_UI_PORT   未给参数时生效
        优先级：参数 > 环境变量 > 默认值。

        为什么要有这个：stop 会杀进程，而「验证 stop 有没有杀错」只能在**不相干**的端口上做。
        8790/8799 常常被开发实例占着（它们不能被杀），所以自测一律用 8791/8792 这类临时端口，
        验完即停。非默认端口时日志改成 api-<port>.log / ui-<port>.log，不和生产日志混读。

    ## 状态判据（为什么不是「看自己的 JSON」）

        本地 JSON 只记「上一轮我起了谁」，它不能回答「现在这两个端口上是谁」。所以三态改成
        **端口 + 健康**双判据，留痕只用于停止：

            没人在听                                → free      （才启动）
            有人在听 且 探针 200                    → running   （复用，只开浏览器，绝不 spawn）
            有人在听 但 探针不是 200                → conflict  （异常占用，不动，退出码 2）

        API 的探针是 GET /api/health，UI 的探针是 GET /。

    ## 退出码

        0  成功（含「本来就在运行」）
        1  停止没做完（仍有端口在听 / 停不掉）
        2  端口被别的程序占着（没有动手）
        3  起不来或没在超时内就绪
        4  前置缺失（文件/python/留痕不存在、端口参数非法）

    ## 验收命令（每条都能失败，不是形式）

        powershell -NoProfile -ExecutionPolicy Bypass -File tools\start-scene.ps1 -NoPause -NoBrowser
        curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8790/api/health     # 200
        curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8799/              # 200
        Get-Content tools\.scene-pids.json -Raw                                  # 含 pid/port/时间
        tools\stop-scene.cmd                                                     # 两端口都不在听

        临时端口自测（不动 8790/8799）：
        ... -File tools\start-scene.ps1 -ApiPort 8791 -UiPort 8792 -NoPause -NoBrowser
        ... -File tools\start-scene.ps1 -Stop -ApiPort 8791 -UiPort 8792 -NoPause
#>

[CmdletBinding()]
param(
    [switch]$Stop,          # 停止模式：只处理 .scene-pids.json 里留痕的 PID
    [switch]$NoBrowser,     # 不起浏览器（自测用）
    [switch]$NoPause,       # 结束不等人按回车（自测/自动化用）
    [int]$TimeoutSec = 20,  # 就绪等待上限（秒）
    [int]$ApiPort = 0,      # API 端口；0 = 用环境变量 FISH_SCENE_API_PORT，再没有就 8790
    [int]$UiPort  = 0       # 静态 UI 端口；0 = 用环境变量 FISH_SCENE_UI_PORT，再没有就 8799
)

# ── 0 · 运行环境校正（这两段是本机踩出来的） ──────────────────────────────────
# 原生命令（python/git）输出 UTF-8，而 PS 5.1 默认按本地代码页解码 → 中文乱码。这行纠正它。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# 本机进程环境里有 Path / PATH / path 三个大小写变体（安装器留下的）。Start-Process
# 重建环境字典时撞上重复键会直接抛「字典中的关键字:Path 所添加的关键字:PATH」，
# 子进程根本起不来。归一化成一个键即可；值不变，不影响路径解析。
try {
    $pathVals = [System.Environment]::GetEnvironmentVariables('Process').Keys |
                Where-Object { $_ -match '(?i)^path$' }
    if (@($pathVals).Count -gt 1) {
        $pathValue = $env:Path
        foreach ($k in @($pathVals)) { Remove-Item -Path ("Env:\" + $k) -ErrorAction SilentlyContinue }
        $env:Path = $pathValue
    }
} catch { }

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

# ── 1 · 端口解析（参数 > 环境变量 > 默认；非法值当场拒） ──────────────────────
function Resolve-ScenePort {
    param([int]$Given, [string]$EnvName, [int]$Default)
    if ($Given -ne 0) {
        if ($Given -lt 1 -or $Given -gt 65535) { throw ('端口参数越界：' + $Given + '（合法范围 1-65535）') }
        return [pscustomobject]@{ Port = $Given; From = '命令行参数' }
    }
    $raw = [System.Environment]::GetEnvironmentVariable($EnvName)
    if ($raw) {
        $n = 0
        if ([int]::TryParse($raw.Trim(), [ref]$n) -and $n -ge 1 -and $n -le 65535) {
            return [pscustomobject]@{ Port = $n; From = ('环境变量 ' + $EnvName) }
        }
        throw ('环境变量 ' + $EnvName + ' 的值不是合法端口：' + $raw)
    }
    return [pscustomobject]@{ Port = $Default; From = '默认值' }
}

try {
    $apiPortInfo = Resolve-ScenePort -Given $ApiPort -EnvName 'FISH_SCENE_API_PORT' -Default 8790
    $uiPortInfo  = Resolve-ScenePort -Given $UiPort  -EnvName 'FISH_SCENE_UI_PORT'  -Default 8799
} catch {
    Write-Host ''
    Write-Host ('  [X]    ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '  用法：start-scene.ps1 [-ApiPort 8791] [-UiPort 8792]' -ForegroundColor Gray
    if (-not $NoPause) { try { Read-Host '按回车退出' | Out-Null } catch { } }
    exit 4
}
if ($apiPortInfo.Port -eq $uiPortInfo.Port) {
    Write-Host ''
    Write-Host ('  [X]    API 端口和 UI 端口不能相同（都是 ' + $apiPortInfo.Port + '）') -ForegroundColor Red
    if (-not $NoPause) { try { Read-Host '按回车退出' | Out-Null } catch { } }
    exit 4
}

# ── 2 · 常量（唯一真相，改路径只改这里） ──────────────────────────────────────
$Root      = 'I:\app\fish'
$ToolsDir  = Join-Path $Root 'tools'
$LogDir    = Join-Path $ToolsDir 'logs'
$StateFile = Join-Path $ToolsDir '.scene-pids.json'
$ApiScript = Join-Path $Root 'p0\fish_workbench_api.py'
$UiDist    = Join-Path $Root 'ui\dist'

$ApiPortNo = $apiPortInfo.Port
$UiPortNo  = $uiPortInfo.Port
$ApiUrl    = 'http://127.0.0.1:' + $ApiPortNo
$UiUrl     = 'http://127.0.0.1:' + $UiPortNo

# 打开浏览器用的地址：静态 UI 带 api 参数（前端据此找后端，别把 ?api= 去掉）
$OpenUrl   = $UiUrl + '/?api=' + $ApiUrl

# 非默认端口时日志分开写：临时端口的失败不该混进生产日志，否则「日志尾」这栏就不可信了
function Get-ServiceLogPath {
    param([string]$Base, [int]$Port, [int]$DefaultPort)
    if ($Port -eq $DefaultPort) { return (Join-Path $LogDir ($Base + '.log')) }
    return (Join-Path $LogDir ($Base + '-' + $Port + '.log'))
}

# 两个服务。CmdNeedle 是「命令行里出现它才算本服务」，用于 adopt 与停止前的身份复核。
$Services = @(
    [pscustomobject]@{
        Key         = 'api'
        Name        = 'API'
        Port        = $ApiPortNo
        Url         = $ApiUrl
        ProbeUrl    = $ApiUrl + '/api/health'
        Log         = (Get-ServiceLogPath -Base 'api' -Port $ApiPortNo -DefaultPort 8790)
        WorkDir     = $Root
        CmdNeedle   = 'fish_workbench_api.py'
        ProbeNeedle = 'fish-workbench'
        Args        = @('-X', 'utf8', '-u', $ApiScript, '--port', "$ApiPortNo")
        Describe    = ('python -X utf8 -u "' + $ApiScript + '" --port ' + $ApiPortNo)
    }
    [pscustomobject]@{
        Key         = 'ui'
        Name        = '静态 UI'
        Port        = $UiPortNo
        Url         = $UiUrl
        ProbeUrl    = $UiUrl + '/'
        Log         = (Get-ServiceLogPath -Base 'ui' -Port $UiPortNo -DefaultPort 8799)
        WorkDir     = $Root
        CmdNeedle   = 'http.server'
        ProbeNeedle = 'id="root"'
        # --bind 127.0.0.1：本机读口只给本机看，别在局域网里裸奔（探针也走 127.0.0.1）
        Args        = @('-X', 'utf8', '-u', '-m', 'http.server', "$UiPortNo", '--bind', '127.0.0.1', '--directory', $UiDist)
        Describe    = ('python -X utf8 -u -m http.server ' + $UiPortNo + ' --bind 127.0.0.1 --directory "' + $UiDist + '"')
    }
)

# ── 3 · 输出（house 风格：一条消息一个色块，不画表格） ────────────────────────
function Say     { param([string]$m) Write-Host ('  ' + $m) -ForegroundColor Gray }
function SayOk   { param([string]$m) Write-Host ('  [OK]   ' + $m) -ForegroundColor Green }
function SayInfo { param([string]$m) Write-Host ('  [..]   ' + $m) -ForegroundColor Cyan }
function SayWarn { param([string]$m) Write-Host ('  [!]    ' + $m) -ForegroundColor Yellow }
function SayErr  { param([string]$m) Write-Host ('  [X]    ' + $m) -ForegroundColor Red }
function SayHead { param([string]$m) Write-Host ''; Write-Host $m -ForegroundColor White }

function Show-Block {
    param([string]$Text, [string]$Prefix = '    ')
    foreach ($line in ($Text -split "`r?`n")) { Say ($Prefix + $line) }
}

# 输入被重定向时 Read-Host 会立刻 EOF 或挂住 —— 自动化场景直接跳过（比 -NoPause 更兜底）
function Invoke-Pause {
    param([string]$Prompt)
    if ($NoPause) { return }
    try { if ([Console]::IsInputRedirected) { return } } catch { }
    try { Read-Host $Prompt | Out-Null } catch { }
}

# ── 4 · 探针（全部走 127.0.0.1，绝不用 localhost） ────────────────────────────
# localhost 在 Windows 上会先解析到 IPv6 ::1，而两个服务都只绑 IPv4；
# .NET 的 Invoke-WebRequest 走 localhost 会被 ::1 拖到超时（curl 有 IPv4 回退所以看不出来）。
function Test-Tcp {
    param([int]$Port)
    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($iar)
        return $client.Connected
    } catch { return $false }
    finally { if ($client) { try { $client.Close() } catch { } } }
}

function Get-HttpProbe {
    param([string]$Url)
    $res = [pscustomobject]@{ Ok = $false; Status = 0; Body = ''; Error = '' }
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        $res.Status = [int]$r.StatusCode
        $res.Body   = [string]$r.Content
        $res.Ok     = ($res.Status -eq 200)
    } catch {
        $res.Error = $_.Exception.Message
        try { if ($_.Exception.Response) { $res.Status = [int]$_.Exception.Response.StatusCode } } catch { }
    }
    return $res
}

# 谁在监听这个端口：返回 pid（默认只取 pid，快）。要进程名/命令行加 -Details。
# 本机实测 Get-CimInstance 一次约 0.9 秒，所以 CIM 只在真需要时（报冲突、复核身份）才查，
# 绝不放进任何轮询循环里 —— 放进去了就是「每轮 1 秒、20 秒只够问 20 次」。
function Get-PortOwner {
    param([int]$Port, [switch]$Details)

    $ownerPid = $null
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($conn) { $ownerPid = [int]$conn.OwningProcess }
    } catch { }

    if (-not $ownerPid) {
        # 兜底：拿不到 Get-NetTCPConnection（老系统/精简环境）就从 netstat 解析 LISTENING 那一行的 pid
        try {
            foreach ($line in @(netstat -ano -p TCP)) {
                if ($line -match ('^\s*TCP\s+127\.0\.0\.1:' + $Port + '\s+\S+\s+LISTENING\s+(\d+)')) {
                    $ownerPid = [int]$matches[1]; break
                }
            }
        } catch { }
    }
    if (-not $ownerPid) { return $null }

    $name = ''; $cmdLine = ''
    if ($Details) {
        try {
            $proc = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $ownerPid) -ErrorAction SilentlyContinue
            if ($proc) { $name = [string]$proc.Name; $cmdLine = [string]$proc.CommandLine }
        } catch { }
        if (-not $name) { $name = '(读不到)' }
    }
    return [pscustomobject]@{ Pid = $ownerPid; Name = $name; CommandLine = $cmdLine }
}

function Get-LogTail {
    param([string]$Path, [int]$Lines = 15)
    if (-not (Test-Path -LiteralPath $Path)) { return '(日志文件不存在)' }
    try {
        $all = @(Get-Content -LiteralPath $Path -Encoding UTF8 -ErrorAction Stop)
        if ($all.Count -eq 0) { return '(日志为空)' }
        $take = [Math]::Min($Lines, $all.Count)
        return ($all[($all.Count - $take)..($all.Count - 1)] -join "`r`n")
    } catch { return ('(日志读不出来：' + $_.Exception.Message + ')') }
}

# ── 5 · 留痕（JSON 用 UTF-8 无 BOM 写：BOM 会让 python/json 那边多一个字符） ──
function Read-State {
    if (-not (Test-Path -LiteralPath $StateFile)) { return $null }
    try { return (Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json) }
    catch { return $null }
}

function Write-State {
    param($State)
    try {
        $json = $State | ConvertTo-Json -Depth 6
        $tmp  = $StateFile + '.tmp'
        [IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmp -Destination $StateFile -Force
        return $true
    } catch {
        SayErr ('PID 留痕写不进去：' + $_.Exception.Message)
        Say    ('  目标路径：' + $StateFile)
        return $false
    }
}

# 留痕的形状是 { startedAt, api: {port,pid,...}, ui: {port,pid,...} }。
# 旧版本把这两条塞在 services.api / services.ui 下面，这里两种都认 —— 留痕格式变了不该让
# 「升级前起的那一份」变得停不掉。
function Get-StateServiceRecord {
    param($State, [string]$Key)
    if (-not $State) { return $null }
    try {
        if ($State.PSObject.Properties.Name -contains $Key) { return $State.$Key }
    } catch { }
    try {
        if ($State.services) {
            if ($State.services.PSObject.Properties.Name -contains $Key) { return $State.services.$Key }
        }
    } catch { }
    return $null
}

function New-ServiceRecord {
    param($Svc, $PidValue, $ShimValue, [bool]$Managed, [string]$StartedAt)
    return [ordered]@{
        label     = $Svc.Key
        port      = $Svc.Port
        url       = $Svc.Url
        pid       = $PidValue
        shimPid   = $ShimValue
        managed   = $Managed
        startedAt = $StartedAt
        log       = $Svc.Log
    }
}

# ── 6 · python 解释器（这台机器 PATH 上同时有真 python 和 WindowsApps 商店跳板） ─
function Test-PythonCandidate {
    param([string]$Exe)
    try {
        $out = & $Exe -c "import sys;print(sys.version_info[0],sys.version_info[1])" 2>&1
        if ($LASTEXITCODE -ne 0) { return $null }
        if (("$out").Trim() -match '^3\s+(\d+)$') {
            $minor = [int]$matches[1]
            if ($minor -ge 8) { return [pscustomobject]@{ Exe = $Exe; Minor = $minor } }
        }
    } catch { }
    return $null
}

function Resolve-PythonExe {
    $candidates = New-Object System.Collections.Generic.List[string]

    # 1) 上次成功用过的解释器（留痕里记着）——路径没搬动就继续用它
    $prior = Read-State
    if ($prior -and $prior.python -and (Test-Path -LiteralPath ([string]$prior.python))) {
        $candidates.Add([string]$prior.python)
    }
    # 2) PATH 上的 python.exe，排除 WindowsApps 的商店跳板（跑它会弹应用商店）
    foreach ($c in @(Get-Command 'python.exe' -All -ErrorAction SilentlyContinue)) {
        if ($c.Source -and ($c.Source -notmatch '\\WindowsApps\\')) { $candidates.Add($c.Source) }
    }
    # 3) py 启动器：让它自己报出真解释器的绝对路径
    $py = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $real = (& $py.Source -3 -c "import sys;print(sys.executable)" 2>&1 | Select-Object -First 1)
            if ($real -and ($LASTEXITCODE -eq 0) -and (Test-Path -LiteralPath ([string]$real).Trim())) {
                $candidates.Add(([string]$real).Trim())
            }
        } catch { }
    }

    foreach ($cand in $candidates) {
        $ok = Test-PythonCandidate -Exe $cand
        if ($ok) { return $ok.Exe }
    }
    return $null
}

# ── 7 · 三态判定：running / free / conflict（端口 + 健康，不看自己的 JSON） ──────
# 判据说明（上一版的真缺陷就在这）：
#   旧实现把「本启动器上一轮记的 JSON」当成「现在谁在听」——JSON 只能说明我起过谁，
#   回答不了「此刻这个端口上是活着还是死了、还是别人」。于是 JSON 一缺（或 pid 对不上）
#   就把「正在跑的服务」误判成「未运行」，直接起了第二份，两份抢同一个数据目录。
#   现在的判据只有两条可观测事实：**端口有没有人在听** + **健康探针是不是 200**。
function Get-ServiceState {
    param($Svc)
    $owner = Get-PortOwner -Port $Svc.Port
    if (-not $owner) {
        return [pscustomobject]@{
            State = 'free'; Pid = $null; Identity = $false
            Owner = [pscustomobject]@{ Pid = $null; Name = ''; CommandLine = '' }
            Probe = $null; Why = '端口没人监听'
        }
    }
    $probe    = Get-HttpProbe -Url $Svc.ProbeUrl
    $healthy  = [bool]$probe.Ok                                   # 判据：HTTP 200
    $identity = ($probe.Ok -and $probe.Body -and $probe.Body.Contains($Svc.ProbeNeedle))

    if (-not $healthy) {
        $why = 'HTTP ' + $probe.Status
        if ($probe.Error) { $why = $why + '（' + $probe.Error + '）' }
        # 只在「端口被人占但健康不过」时才花那次 CIM：报明细要用命令行
        $detail = Get-PortOwner -Port $Svc.Port -Details
        $ownerInfo = $owner
        if ($detail) { $ownerInfo = $detail }
        return [pscustomobject]@{ State = 'conflict'; Pid = $ownerInfo.Pid; Identity = $false; Owner = $ownerInfo; Probe = $probe; Why = $why }
    }

    # 有监听 + 200 → 已在运行。pid 一律取 **端口占用者**（Get-NetTCPConnection 的 OwningProcess），
    # 不取 health body 里自报的 pid：那个数只是进程自我介绍，未必等于监听者（重载父进程、子壳都可能），
    # 而留痕要回答的是「谁占着这个端口」，两者不一致就等于留痕没写对。
    return [pscustomobject]@{ State = 'running'; Pid = $owner.Pid; Identity = $identity; Owner = $owner; Probe = $probe; Why = '端点 HTTP 200' }
}

# ── 8 · 起服务：拉外壳 / 解析真实 pid（两件事分开，别混在一个「等」里） ────────
# 8.1 只负责把「cmd 外壳 + python」拉起来并返回**外壳** pid，不等子进程。
#     等子进程是不可靠的判据：本机 CIM ~0.9s/次、python 可见延迟 1~7s，
#     等不到就判「起不来」会把「正在起」判死（第一版真踩过）。
function Start-FishShim {
    param($Svc, [string]$PythonExe)
    $argStr = (($Svc.Args | ForEach-Object { '"' + $_ + '"' }) -join ' ')
    # cmd /c 的引号剥离规则下唯一稳定的写法：整条命令再包一层引号（别的写法要么红不了日志、
    # 要么把重定向路径吃掉 —— 实测过多种）。
    $inner = '"{0}" {1} > "{2}" 2>&1' -f $PythonExe, $argStr, $Svc.Log
    $shim = Start-Process -FilePath $env:ComSpec -ArgumentList ('/c', ('"' + $inner + '"')) `
            -WorkingDirectory $Svc.WorkDir -WindowStyle Hidden -PassThru -ErrorAction Stop
    if (-not $shim) { throw 'Start-Process 没有返回进程对象' }
    return $shim.Id
}

# 8.2 外壳的 python 子进程：只在「就绪失败要留痕/收尾」和端口占用者读不到时用，
#     不做轮询（一次 CIM = 0.9s，轮询就是拿秒表换噪声）。
function Get-ChildPythonPid {
    param([int]$ShimPid)
    if (-not $ShimPid) { return $null }
    try {
        $kids = @(Get-CimInstance Win32_Process -Filter ("ParentProcessId=" + $ShimPid) -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -eq 'python.exe' })
        if ($kids.Count -gt 0) { return [int]$kids[0].ProcessId }
    } catch { }
    return $null
}

# 8.3 真实 pid = 端口占用者（这才叫「谁在服务」），并复核命令行特征。
#     命令行对不上只警告、不放弃：留痕要记的是「谁占着这个端口」，记不到就等于 stop 没有依据。
#     （旧版在这里返回 $null，于是 pid 留成空 —— 留痕失效的一个直接原因。）
function Resolve-ServicePid {
    param($Svc)
    $owner = Get-PortOwner -Port $Svc.Port -Details
    if (-not $owner) { return $null }
    if ($owner.CommandLine -and ($owner.CommandLine.ToLower().IndexOf($Svc.CmdNeedle.ToLower()) -lt 0)) {
        SayWarn ('端口 {0} 的占用者 pid {1} 命令行里没有 "{2}"，仍按端口占用者记录' -f $Svc.Port, $owner.Pid, $Svc.CmdNeedle)
    }
    return $owner.Pid
}

function Wait-ServicesReady {
    param([int]$Timeout)
    $report = @{}
    $deadline = (Get-Date).AddSeconds($Timeout)
    while ($true) {
        $allOk = $true
        foreach ($svc in $Services) {
            $prev = $report[$svc.Key]
            if ($prev -and $prev.Ok) { continue }
            $probe = Get-HttpProbe -Url $svc.ProbeUrl
            $ok = ($probe.Ok -and $probe.Body -and $probe.Body.Contains($svc.ProbeNeedle))
            $report[$svc.Key] = [pscustomobject]@{ Ok = $ok; Probe = $probe }
            if (-not $ok) { $allOk = $false }
        }
        if ($allOk) { return $report }
        if ((Get-Date) -ge $deadline) { return $report }
        Start-Sleep -Milliseconds 250
    }
}

# ── 9 · 启动主流程 ───────────────────────────────────────────────────────────
function Invoke-Start {
    SayHead 'fish 现场 · 启动检查'

    # 9.1 前置：缺什么当场说缺什么，别等 20 秒后再报「超时」
    $missing = @()
    if (-not (Test-Path -LiteralPath $ApiScript)) { $missing += $ApiScript }
    if (-not (Test-Path -LiteralPath $UiDist))    { $missing += $UiDist }
    if ($missing.Count -gt 0) {
        SayErr '缺少启动所需的东西，没有动手：'
        foreach ($m in $missing) { Say ('  · ' + $m) }
        Say '先确认项目路径没搬动、ui/dist 已构建，再重试。'
        return 4
    }
    if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

    # 9.2 python
    $pythonExe = Resolve-PythonExe
    if (-not $pythonExe) {
        SayErr '找不到可用的 Python 3（需要 3.8+），没有动手。'
        Say '已试过：留痕里记过的解释器 / PATH 上的 python.exe（排除 WindowsApps 商店跳板）/ py -3'
        Say '修法：装 Python 3.11 并勾选 Add to PATH；或把 python.exe 绝对路径写进本脚本的 Resolve-PythonExe。'
        return 4
    }
    SayOk ('python：' + $pythonExe)

    # 9.3 探测（三态：只看端口 + 健康，不看上一轮的 JSON）
    SayInfo ('端口：API {0}（{1}） / UI {2}（{3}）' -f $ApiPortNo, $apiPortInfo.From, $UiPortNo, $uiPortInfo.From)
    $states = [ordered]@{}
    foreach ($svc in $Services) {
        $states[$svc.Key] = Get-ServiceState -Svc $svc
        $st = $states[$svc.Key]
        if ($st.State -eq 'running') {
            SayOk   ('{0,-7} {1}  已在运行（pid {2}）' -f $svc.Name, $svc.Url, $st.Pid)
            if (-not $st.Identity) { SayWarn ('  端点 200，但响应里没有 "{0}" 这个特征 —— 按「已在运行」处理，不重复启动' -f $svc.ProbeNeedle) }
        } elseif ($st.State -eq 'free') {
            SayInfo ('{0,-7} {1}  未在运行，稍后启动' -f $svc.Name, $svc.Url)
        } else {
            SayErr  ('{0,-7} {1}  端口被占但健康检查不过（pid {2}）：{3}' -f $svc.Name, $svc.Port, $st.Owner.Pid, $st.Why)
        }
    }

    # 9.4 端口被别的程序占着（异常占用）→ 一个字节都不动，不启动第二份
    $conflicts = @($Services | Where-Object { $states[$_.Key].State -eq 'conflict' })
    if ($conflicts.Count -gt 0) {
        SayHead '结论：没有动手（异常占用，没有启动第二份）'
        foreach ($svc in $conflicts) {
            $st = $states[$svc.Key]
            SayErr ('{0} 端口 {1} 被 pid {2} 占着，但 {3} 不是 200：{4}' -f $svc.Name, $svc.Port, $st.Owner.Pid, $svc.ProbeUrl, $st.Why)
            Say    ('  占用进程：pid {0}  名字 {1}' -f $st.Owner.Pid, $(if ($st.Owner.Name) { $st.Owner.Name } else { '(读不到)' }))
            Say    ('  它的命令行：' + $st.Owner.CommandLine)
            Say    ('  自己核对：netstat -ano | findstr :' + $svc.Port)
            if ($st.Probe -and $st.Probe.Body) { Show-Block ('响应开头：' + $st.Probe.Body.Substring(0, [Math]::Min(120, $st.Probe.Body.Length))) }
        }
        Say '我不杀别人的进程（也没有 taskkill /IM）：请先自己关掉它，或换端口后再启动。'
        Say ('换端口：start-scene.cmd --api-port {0} --ui-port {1}' -f $ApiPortNo, $UiPortNo)
        return 2
    }

    # 9.5 起缺的那些：先把外壳全拉起来（不串行等子进程），再统一等就绪。
    #     串行等的代价是两边时间叠加；而「等子进程」这个判据本身还不可靠（见函数注释）。
    $prior   = Read-State
    $shims   = [ordered]@{}          # key → 本次拉起的 cmd 外壳 pid
    $needed  = @($Services | Where-Object { $states[$_.Key].State -eq 'free' })

    if ($needed.Count -eq 0) {
        SayHead 'fish 现场已在运行（不重复启动）'
        SayInfo 'API 与静态 UI 都在跑，这次不再起进程，直接打开浏览器。'
    }

    foreach ($svc in $needed) {
        SayInfo ('启动 {0}' -f $svc.Describe)
        try { $shims[$svc.Key] = Start-FishShim -Svc $svc -PythonExe $pythonExe }
        catch {
            SayErr ('{0} 起不来：{1}' -f $svc.Name, $_.Exception.Message)
            # 半套服务比没有更糟：把已经拉起来的收掉再报错
            foreach ($k in @($shims.Keys)) {
                $c = Get-ChildPythonPid -ShimPid ([int]$shims[$k])
                if ($c) { Stop-Process -Id ([int]$c) -Force -ErrorAction SilentlyContinue }
                Stop-Process -Id ([int]$shims[$k]) -Force -ErrorAction SilentlyContinue
            }
            return 3
        }
        SayOk ('  外壳 pid {0}（真实 pid 待就绪后按端口占用者回填）' -f $shims[$svc.Key])
    }

    # 9.6 等就绪（两个端点都要 200 且内容认得出是本服务）
    Write-Host ''
    SayInfo ('等待就绪（最多 {0} 秒）' -f $TimeoutSec)
    $report = Wait-ServicesReady -Timeout $TimeoutSec
    $failed = @($Services | Where-Object { -not $report[$_.Key].Ok })

    # 9.7 回填 pid 并留痕。成功/失败都写：不留痕，stop-scene 就收不掉这半套。
    #     这里的 pid 一律是 **端口占用者反查**的结果（Get-NetTCPConnection … OwningProcess），
    #     不是「等子进程」也不是「健康响应里自报的 pid」—— 前两个都会记成别人或记成空。
    $priorSamePorts = $false
    if ($prior) {
        $priorApi = Get-StateServiceRecord -State $prior -Key 'api'
        if ($priorApi -and ($null -ne $priorApi.port) -and ([int]$priorApi.port -eq $ApiPortNo)) { $priorSamePorts = $true }
    }

    $records = [ordered]@{}
    foreach ($svc in $Services) {
        $st = $states[$svc.Key]
        if ($shims.Contains($svc.Key)) {
            $realPid = Resolve-ServicePid -Svc $svc
            if (-not $realPid) { $realPid = Get-ChildPythonPid -ShimPid ([int]$shims[$svc.Key]) }
            $records[$svc.Key] = New-ServiceRecord -Svc $svc -PidValue $realPid -ShimValue ([int]$shims[$svc.Key]) `
                                  -Managed $true -StartedAt ((Get-Date).ToString('o'))
        } else {
            $managed = $false; $shimPid = $null; $startedAt = (Get-Date).ToString('o')
            if ($priorSamePorts) {
                $old = Get-StateServiceRecord -State $prior -Key $svc.Key
                if ($old -and ($null -ne $old.pid) -and ([int]$old.pid -eq [int]$st.Pid)) {
                    $managed   = [bool]$old.managed
                    $shimPid   = $old.shimPid
                    $startedAt = [string]$old.startedAt
                }
            }
            $records[$svc.Key] = New-ServiceRecord -Svc $svc -PidValue $st.Pid -ShimValue $shimPid `
                                  -Managed $managed -StartedAt $startedAt
        }
    }

    # 失败原因写成一行（控制台说过的话必须留得下来 —— 「只在控制台说」等于没留痕）
    $reason = $null
    if ($failed.Count -gt 0) {
        $bits = @()
        foreach ($svc in $failed) {
            $r = $report[$svc.Key]
            $detail = 'HTTP ' + $r.Probe.Status
            if ($r.Probe.Error) { $detail = $detail + '（' + $r.Probe.Error + '）' }
            $bits += ('{0}（端口 {1}）{2} 秒内未就绪，最后探测 {3}' -f $svc.Name, $svc.Port, $TimeoutSec, $detail)
        }
        $reason = $bits -join ' ; '
    }

    # startedAt 是「第一次被本启动器记录的时刻」，重复双击只刷 updatedAt（复用不是重新启动）
    $firstAt = (Get-Date).ToString('o')
    if ($priorSamePorts -and $prior.startedAt) { $firstAt = [string]$prior.startedAt }

    $doc = [ordered]@{
        schema    = 'fish-scene/1'
        complete  = ($failed.Count -eq 0)
        failed    = ($failed.Count -gt 0)
        reason    = $reason
        startedAt = $firstAt
        updatedAt = (Get-Date).ToString('o')
        root      = $Root
        python    = $pythonExe
        openUrl   = $OpenUrl
        apiPort   = $ApiPortNo
        uiPort    = $UiPortNo
        api       = $records['api']
        ui        = $records['ui']
    }
    $stateOk = Write-State -State $doc

    if ($failed.Count -gt 0) {
        SayHead '结论：没等到就绪（超时）'
        foreach ($svc in $failed) {
            $r = $report[$svc.Key]
            SayErr ('{0}：{1} 在 {2} 秒内没有给出 200 + 正确内容' -f $svc.Name, $svc.ProbeUrl, $TimeoutSec)
            Say    ('  最后一次探测：status={0} 错误={1}' -f $r.Probe.Status, $(if ($r.Probe.Error) { $r.Probe.Error } else { '(无)' }))
            $p = $records[$svc.Key].pid
            if ($p) {
                $alive = [bool](Get-Process -Id ([int]$p) -ErrorAction SilentlyContinue)
                if ($alive) { Say ('  进程 pid {0}：还活着（进程没死，是端点没响应）' -f $p) }
                else        { Say ('  进程 pid {0}：已退出（看下面日志尾的报错）' -f $p) }
            } else {
                Say '  进程：端口上没有占用者，也没找到 python 子进程（多半已经退出）'
            }
            Say ('  日志：' + $svc.Log)
            Show-Block (Get-LogTail -Path $svc.Log -Lines 15)
        }
        Say '常见原因：库路径探测失败（api.log 里的 stderr）/ ui/dist 没构建 / 端口被半开占用。'
        if ($stateOk) {
            SayOk ('失败已留痕（failed: true）：' + $StateFile)
            Say '这一轮起的进程如果还活着，双击 stop-scene.cmd 可以收掉它们。'
        } else {
            SayErr '失败原因没能写进留痕文件 —— stop-scene 可能收不掉这一轮起的进程，请手工核对上面的 pid。'
        }
        return 3
    }

    SayHead '已就绪'
    foreach ($svc in $Services) {
        $rec = $records[$svc.Key]
        # 措辞按「这一轮做了什么」说，不按记录里的归属说 —— 否则重复双击会谎称「本次启动」
        if ($shims.Contains($svc.Key)) {
            $how = '本次启动'
        } elseif ($rec.managed) {
            $how = '复用（上一轮由本启动器起的）'
        } else {
            $how = '复用（留痕里没有它的来源记录，日志里可能没有它的输出）'
        }
        SayOk ('{0,-7} {1}  pid {2}（{3}）' -f $svc.Name, $svc.Url, $rec.pid, $how)
    }
    Say ('  日志：' + (($Services | ForEach-Object { $_.Log }) -join ' 、 '))
    Say ('  留痕：' + $StateFile + '（pid 取自端口占用者，stop 按它收）')
    Say ('  停止：双击 tools\stop-scene.cmd')

    if (-not $NoBrowser) {
        try { Start-Process $OpenUrl | Out-Null; SayOk ('已在浏览器打开：' + $OpenUrl) }
        catch { SayWarn ('自动开浏览器失败：' + $_.Exception.Message + '（手动打开 ' + $OpenUrl + '）') }
    }

    SayHead '说明'
    Say '· 关掉这个窗口不会停服务（服务是独立进程）。'
    Say '· 再双击一次不会起第二份：已在跑的会被复用，只帮你把浏览器打开。'
    if (-not $stateOk) {
        SayErr '服务已就绪，但 PID 留痕没写进去 → stop-scene 将没有依据（上面有原因）。'
        return 1
    }
    return 0
}

# ── 10 · 停止主流程（只杀留痕里的两个 pid，且逐个复核身份） ────────────────────
function Invoke-Stop {
    SayHead 'fish 现场 · 停止'

    $state = Read-State
    if (-not $state) {
        $listening = @($Services | Where-Object { Test-Tcp -Port $_.Port })
        if ($listening.Count -eq 0) {
            SayInfo '没有在运行的 fish 现场（两端口都没人监听），也没有 PID 留痕 —— 不需要做事。'
            return 0
        }
        SayWarn '端口还在听，但没有 .scene-pids.json：说明这两个进程不是本启动器起的。'
        foreach ($svc in $listening) {
            $o = Get-PortOwner -Port $svc.Port -Details
            if ($o) {
                Say ('  {0} 端口 {1} ← pid {2}（{3}）' -f $svc.Name, $svc.Port, $o.Pid, $o.Name)
                Say ('    命令行：' + $o.CommandLine)
            } else {
                Say ('  {0} 端口 {1} ← 占用者读不出来' -f $svc.Name, $svc.Port)
            }
        }
        Say '没有留痕就无法确认「哪个 pid 属于 fish」，所以我不动它们。'
        return 4
    }

    $stopped = @()
    $skipped = @()

    # 留痕记的是「某一对端口」的现场。若这次要停的端口和留痕里的对不上，那就是「这对端口没留痕」——
    # 不能凭别的端口的记录去杀进程，也不能顺手把那条记录删掉。
    $stateApiRec = Get-StateServiceRecord -State $state -Key 'api'
    $stateMatchesPorts = [bool]($stateApiRec -and ($null -ne $stateApiRec.port) -and ([int]$stateApiRec.port -eq $ApiPortNo))

    foreach ($svc in $Services) {
        $rec = Get-StateServiceRecord -State $state -Key $svc.Key
        if (-not $rec) {
            SayInfo ('{0}：留痕里没有这一项，跳过' -f $svc.Name)
            continue
        }

        # 留痕里的端口必须和这次要停的端口一致。否则「stop --api-port 8791」会照着留痕把
        # 8790 上那个（还在给生产用的）pid 杀掉 —— 留痕是「这对端口的现场记录」，不是全局清单。
        if (($null -ne $rec.port) -and ([int]$rec.port -ne [int]$svc.Port)) {
            SayWarn ('{0}：留痕记的是端口 {1}，这次要停的是 {2} → 不动它' -f $svc.Name, $rec.port, $svc.Port)
            continue
        }

        # 留痕里只有外壳、没有 pid 的情况（上一轮起失败留下的半套）：先认外壳的 python 子进程
        if (($null -eq $rec.pid) -or ([int]$rec.pid -le 0)) {
            if (-not $rec.shimPid) {
                SayInfo ('{0}：留痕里既没有 pid 也没有外壳 pid，跳过' -f $svc.Name)
                continue
            }
            $childPid = Get-ChildPythonPid -ShimPid ([int]$rec.shimPid)
            if (-not $childPid) {
                SayInfo ('{0}：没有 pid，外壳 {1} 也没有 python 子进程（多半已退出），收掉外壳' -f $svc.Name, $rec.shimPid)
                Stop-Process -Id ([int]$rec.shimPid) -Force -ErrorAction SilentlyContinue
                continue
            }
            SayInfo ('{0}：留痕里没有 pid，按外壳 {1} 的子进程取到 pid {2}' -f $svc.Name, $rec.shimPid, $childPid)
            $targetPid = [int]$childPid
        } else {
            $targetPid = [int]$rec.pid
        }

        if (-not (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) {
            SayInfo ('{0}：pid {1} 已经不在了（可能已手动关闭）' -f $svc.Name, $targetPid)
            continue
        }

        # 身份复核：命令行必须还带本服务特征。PID 会被系统复用，
        # 光凭数字就 Stop-Process 是「误杀别人」的唯一入口。
        $cmd = ''
        try {
            $ci = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $targetPid) -ErrorAction Stop
            if ($ci) { $cmd = [string]$ci.CommandLine }
        } catch { }

        if (-not $cmd) {
            SayWarn ('{0}：读不到 pid {1} 的命令行，按留痕直接停（只影响这一个 pid）' -f $svc.Name, $targetPid)
        } elseif ($cmd.ToLower().IndexOf($svc.CmdNeedle.ToLower()) -lt 0) {
            SayErr ('{0}：pid {1} 的命令行不含 "{2}" → 判为 PID 被复用，不杀。' -f $svc.Name, $targetPid, $svc.CmdNeedle)
            Say ('    它实际是：' + $cmd)
            $skipped += $targetPid
            continue
        }

        Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 250
        if (Get-Process -Id $targetPid -ErrorAction SilentlyContinue) {
            SayErr ('{0}：pid {1} 停不掉（可能需要管理员权限）' -f $svc.Name, $targetPid)
            $skipped += $targetPid
        } else {
            SayOk ('{0}：已停止 pid {1}（端口 {2}）' -f $svc.Name, $targetPid, $svc.Port)
            $stopped += $targetPid
        }

        # 顺带收掉 cmd 外壳（它只是日志重定向的架子，python 没了它自己也会退）
        if ($rec.shimPid) {
            $shimPid = [int]$rec.shimPid
            $shimProc = Get-Process -Id $shimPid -ErrorAction SilentlyContinue
            if ($shimProc) {
                try {
                    $sci = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $shimPid) -ErrorAction SilentlyContinue
                    if ($sci -and ([string]$sci.Name).ToLower() -eq 'cmd.exe') {
                        Stop-Process -Id $shimPid -Force -ErrorAction SilentlyContinue
                    }
                } catch { }
            }
        }
    }

    # 等端口真的释放（Stop-Process 之后内核释放监听有几十毫秒延迟）
    $deadline = (Get-Date).AddSeconds(5)
    $still = @($Services | Where-Object { Test-Tcp -Port $_.Port })
    while (($still.Count -gt 0) -and ((Get-Date) -lt $deadline)) {
        Start-Sleep -Milliseconds 200
        $still = @($Services | Where-Object { Test-Tcp -Port $_.Port })
    }

    if ($still.Count -eq 0) {
        SayHead '结论'
        SayOk ('两端口都已释放（' + (($Services | ForEach-Object { $_.Port }) -join ' / ') + '）')
        if ($stopped.Count -gt 0) {
            Say ('本次只处理留痕里的这两个 pid：' + ($stopped -join '、') + '；没有用 taskkill /IM，别的 python 不受影响。')
        } else {
            Say '没有需要停的进程（留痕里的都已经不在了）。'
        }
        if ($stateMatchesPorts) {
            if (Test-Path -LiteralPath $StateFile) { Remove-Item -LiteralPath $StateFile -Force }
            Say '留痕文件已清掉，下次启动重新记录。'
        } else {
            Say ('留痕里那对端口不是这次要停的（' + $StateFile + '）→ 保留不动，免得把别的端口的记录删掉。')
        }
        return 0
    }

    SayHead '结论：没停干净'
    foreach ($svc in $still) {
        $o = Get-PortOwner -Port $svc.Port -Details
        SayErr ('{0}：端口 {1} 仍在监听' -f $svc.Name, $svc.Port)
        if ($o) {
            if ($skipped -contains $o.Pid) { Say ('    占用者是 pid {0}，但它被判定为「不是本服务」→ 没动' -f $o.Pid) }
            else { Say ('    占用者是 pid {0}（{1}），不在留痕里 → 没动它' -f $o.Pid, $o.Name) }
            Say ('    命令行：' + $o.CommandLine)
        }
    }
    Say ('留痕文件保留在 ' + $StateFile + '，供人工核对。')
    return 1
}

# ── 11 · 入口 ────────────────────────────────────────────────────────────────
$exitCode = 0
if ($Stop) { $exitCode = Invoke-Stop } else { $exitCode = Invoke-Start }

Write-Host ''
if ($exitCode -eq 0) { Invoke-Pause '按回车关闭此窗口（服务继续运行）' }
else                 { Invoke-Pause '按回车退出' }
exit $exitCode

#Requires -Version 5.1
<#
.SYNOPSIS
  DSH 0.2.0-rc.2 -> 0.2.1-rc.5 switch script (prepare / apply / verify / rollback)

.DESCRIPTION
  Why: from 0.2.1, `dsh-hooks-claude-code` and `dsh-hooks-codex` are no longer
  dependencies of `dsh`; `dsh-experimental-schedule-bundle` is RETIRED (the host
  auto-drops that line from profile bundles).

  What this does:
    - swaps `checkout` (0.2.0 monorepo) for the already-installed 0.2.1 tree
      (313 of the 334 profile anchors then resolve automatically)
    - drops the 3 hooks packages into the new checkout and rebuilds their anchors
      (0.2.1 source no longer ships packages/hooks)
    - edits profile: removes the schedule-bundle line, bumps tool-session-query,
      adds the two hooks bridges, appends the 3-line schedule mount block
    - swaps `app` for `app-0.2.1` (rename, never delete)

  Everything is rename-based, so Rollback is just renaming back.

.PARAMETER Mode
  Prepare  - read-only preflight (safe while DSH is running)
  Apply    - performs the switch (DSH MUST be fully closed)
  Verify   - post-start health readings
  Rollback - undo the switch

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\switch-0.2.1.ps1 -Mode Prepare
  powershell -NoProfile -ExecutionPolicy Bypass -File .\switch-0.2.1.ps1 -Mode Apply
  powershell -NoProfile -ExecutionPolicy Bypass -File .\switch-0.2.1.ps1 -Mode Verify
  powershell -NoProfile -ExecutionPolicy Bypass -File .\switch-0.2.1.ps1 -Mode Rollback
#>
param(
  [ValidateSet('Prepare', 'Apply', 'Verify', 'Rollback')]
  [string]$Mode = 'Prepare',
  [switch]$Force
)

$ErrorActionPreference = 'Stop'
$script:ts  = Get-Date -Format 'yyyyMMdd-HHmmss'
$script:log = Join-Path $env:TEMP ("dsh-switch-" + $script:ts + ".log")

# -- paths ----------------------------------------------------------
$script:Root       = 'I:\app\DSH-Home'
$script:AppOld     = Join-Path $script:Root 'app'
$script:AppNew     = Join-Path $script:Root 'app-0.2.1'
$script:CkOld      = Join-Path $script:Root 'checkout'
$script:CkNew      = 'I:\app\dsh-src-0.2.1'
$script:Profile    = Join-Path $env:USERPROFILE '.dsh\profiles\desktop'
$script:HookPkgs   = 'I:\app\dsh-hooks-pkgs'
$script:BackupRoot = Join-Path $script:Root '历史版本'
$script:Anchors    = Join-Path $env:USERPROFILE '.dsh\profiles\node_modules\@deepseek-ai'
$script:HooksNames = @('dsh-hook-protocol', 'dsh-hooks-claude-code', 'dsh-hooks-codex')
$script:FishData   = 'I:\app\DSH-Home\workspace\learn\大肥鱼生命系统\p0\data'

function Log {
  param([string]$msg, [string]$level = 'INFO')
  $line = "[{0}] [{1}] {2}" -f (Get-Date -Format 'HH:mm:ss'), $level, $msg
  Write-Host $line
  Add-Content -Path $script:log -Value $line -Encoding UTF8
}
function Step { param([string]$msg) Log ("== " + $msg) 'STEP' }
function Fail { param([string]$msg) Log $msg 'FAIL'; throw $msg }

function Write-JsonNoBom {
  param([string]$path, $obj)
  $json = $obj | ConvertTo-Json -Depth 20
  $enc = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($path, $json + "`n", $enc)
}

function Get-DshProcs {
  # NOTE: never touch $_.Path here. Some system processes deny Path access, and with
  # $ErrorActionPreference='Stop' that throws and kills the whole script silently
  # (this is exactly what made an earlier run look like "window closed by itself").
  $list = @()
  foreach ($p in @(Get-Process -ErrorAction SilentlyContinue)) {
    try {
      if ($p.ProcessName -match 'DeepSeek' -or $p.ProcessName -match 'dsh-desktop') { $list += $p }
    } catch { }
  }
  return $list
}

# ==================================================================
function Invoke-Prepare {
  Step 'PREPARE (read-only)'
  $bad = 0

  if (-not (Test-Path $script:AppNew)) {
    Log ("missing " + $script:AppNew) 'FAIL'; $bad++
  } else {
    $asar = Join-Path $script:AppNew 'resources\app.asar'
    if (Test-Path $asar) {
      $mb = [math]::Round((Get-Item $asar).Length / 1MB, 1)
      Log ("app-0.2.1 resources app.asar = " + $mb + " MB")
    } else { Log 'app-0.2.1 missing app.asar' 'FAIL'; $bad++ }
    $exe = Join-Path $script:AppNew 'DeepSeek Harness.exe'
    if (Test-Path $exe) {
      Log ("app-0.2.1 exe FileVersion = " + (Get-Item $exe).VersionInfo.FileVersion)
    } else { Log 'app-0.2.1 missing exe' 'FAIL'; $bad++ }
  }

  if (-not (Test-Path $script:CkNew)) {
    Log ("missing " + $script:CkNew) 'FAIL'; $bad++
  } else {
  $pj = Join-Path $script:CkNew 'package.json'
    if (Test-Path $pj) {
      $j = Get-Content $pj -Raw -Encoding UTF8 | ConvertFrom-Json
      Log ("source tree: " + $j.name + "@" + $j.version)
      if ($j.version -ne '0.2.1-rc.5') { Log 'source tree is not 0.2.1-rc.5' 'FAIL'; $bad++ }
    } else { Log 'source tree missing package.json' 'FAIL'; $bad++ }
    foreach ($d in @('node_modules', 'packages', 'apps')) {
      if (-not (Test-Path (Join-Path $script:CkNew $d))) {
        Log ("source tree missing " + $d) 'FAIL'; $bad++
      }
    }
  }

  foreach ($n in $script:HooksNames) {
    $pj = Join-Path $script:HookPkgs ($n + '\package\package.json')
    if (Test-Path $pj) {
      $j = Get-Content $pj -Raw -Encoding UTF8 | ConvertFrom-Json
      Log ("hooks pkg: " + $j.name + "@" + $j.version)
    } else { Log ("hooks pkg missing: " + $n) 'FAIL'; $bad++ }
  }

  foreach ($p in @(($script:CkOld + '.pre-0.2.1'), ($script:AppOld + '.pre-0.2.1'))) {
    if (Test-Path $p) { Log ("name already taken (previous run?): " + $p) 'WARN' }
  }

  $pj = Join-Path $script:Profile 'package.json'
  if (Test-Path $pj) {
    $prof = Get-Content $pj -Raw -Encoding UTF8 | ConvertFrom-Json
    Log ("profile bundles: " + (($prof.dsh.profile.bundles) -join ', '))
    if (@($prof.dsh.profile.bundles) -contains '@deepseek-ai/dsh-experimental-schedule-bundle') {
      Log 'profile still has schedule-bundle (Apply will remove that line)' 'WARN'
    }
  } else { Log ("missing profile package.json: " + $pj) 'FAIL'; $bad++ }

  $patch = Join-Path $script:Profile 'cordis.patch.yml'
  if (Test-Path $patch) {
    $txt = Get-Content $patch -Raw -Encoding UTF8
    if ($txt -match 'id:\s*time-context') { Log 'schedule mount already present' }
    else { Log 'schedule mount will be appended' }
  }

  $bk = Get-ChildItem $script:BackupRoot -Directory -Filter 'pre-upgrade-0.2.1-*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
  if ($bk) { Log ("rollback payload: " + $bk.FullName) } else { Log 'no pre-upgrade backup dir found' 'WARN' }

  $procs = Get-DshProcs
  if ($procs.Count -gt 0) {
    Log ("running DSH: " + (($procs | ForEach-Object { $_.ProcessName + '#' + $_.Id }) -join ', '))
  } else { Log 'no DSH process running' }

  if ($bad -eq 0) {
    Log 'PREPARE PASSED - close the desktop app, then run -Mode Apply' 'OK'
  } else {
    Log ("PREPARE FAILED: " + $bad + " problem(s)") 'FAIL'
    exit 1
  }
}

# ==================================================================
function Invoke-Apply {
  Step 'APPLY'

  $procs = Get-DshProcs
  if ($procs.Count -gt 0) {
    Log ("DSH still running: " + (($procs | ForEach-Object { $_.ProcessName + '#' + $_.Id }) -join ', ')) 'FAIL'
    Fail 'Close the desktop app completely (including tray), then re-run -Mode Apply'
  }
  $lock = Join-Path $script:Profile 'lock'
  if (Test-Path $lock) {
    if (-not $Force) { Fail ("profile lock still present: " + $lock) }
    Log 'lock present but -Force given, continuing' 'WARN'
  }

  $bkDir = Join-Path $script:BackupRoot ('pre-switch-' + $script:ts)
  New-Item -ItemType Directory -Path $bkDir -Force | Out-Null
  foreach ($f in @('package.json', 'cordis.patch.yml', 'cordis.yml', 'pnpm-lock.yaml', 'pnpm-workspace.yaml')) {
    $src = Join-Path $script:Profile $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $bkDir $f) -Force }
  }
  Log ("profile backed up: " + $bkDir)

  Step '1/5 swap checkout'
  $ckBak = $script:CkOld + '.pre-0.2.1-' + $script:ts
  try {
    Move-Item -LiteralPath $script:CkOld -Destination $ckBak -Force
  } catch {
    Fail ("cannot rename old checkout (a process may still hold it): " + $_.Exception.Message)
  }
  Log ("old checkout -> " + $ckBak)
  try {
    Move-Item -LiteralPath $script:CkNew -Destination $script:CkOld -Force
  } catch {
    # 回退第一步，避免半截状态
    Move-Item -LiteralPath $ckBak -Destination $script:CkOld -Force -ErrorAction SilentlyContinue
    Fail ("cannot move new source tree into place: " + $_.Exception.Message + "  (old checkout restored)")
  }
  # ★ 断言：checkout 必须真的是 0.2.1，否则立刻停（这是上次半截状态的根因）
  $ckPj = Join-Path $script:CkOld 'package.json'
  if (-not (Test-Path $ckPj)) {
    Move-Item -LiteralPath $script:CkOld -Destination ($script:CkOld + '.bad-' + $script:ts) -Force -ErrorAction SilentlyContinue
    Move-Item -LiteralPath $ckBak -Destination $script:CkOld -Force -ErrorAction SilentlyContinue
    Fail "checkout has no package.json after swap -> rolled back that step, aborting"
  }
  $ckVer = (Get-Content $ckPj -Raw -Encoding UTF8 | ConvertFrom-Json).version
  if ($ckVer -ne '0.2.1-rc.5') {
    Fail ("checkout version is " + $ckVer + " (expected 0.2.1-rc.5) -> aborting before touching profile/app")
  }
  Log ("new source tree -> checkout  (verified version " + $ckVer + ")")

  Step '2/5 install hooks packages'
  $hooksDir = Join-Path $script:CkOld 'packages\hooks'
  New-Item -ItemType Directory -Path $hooksDir -Force | Out-Null
  foreach ($n in $script:HooksNames) {
    $src = Join-Path $script:HookPkgs ($n + '\package')
    $dst = Join-Path $hooksDir $n
    if (-not (Test-Path $src)) { Fail ("missing " + $src) }
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Copy-Item $src $dst -Recurse -Force
    Log ("  packages/hooks/" + $n + " OK")
  # tool-ralph: from 0.2.1 it is no longer in-box (moved into experimental-ralph-bundle),
  # but the hive preset still mounts it. Install it so the anchor resolves.
  $ralphSrc = Join-Path $script:HookPkgs 'dsh-tool-ralph\package'
  $ralphDst = Join-Path $script:CkOld 'packages\workflow\tool-ralph'
  if (Test-Path $ralphSrc) {
    New-Item -ItemType Directory -Path (Split-Path $ralphDst -Parent) -Force | Out-Null
    if (Test-Path $ralphDst) { Remove-Item $ralphDst -Recurse -Force }
    Copy-Item $ralphSrc $ralphDst -Recurse -Force
    Log '  packages/workflow/tool-ralph OK'
  } else {
    Log ("tool-ralph package missing: " + $ralphSrc) 'WARN'
  }

  }

  Step '3/5 rebuild anchors'
  foreach ($n in $script:HooksNames) {
    $link = Join-Path $script:Anchors $n
    $target = Join-Path $hooksDir $n
    if (Test-Path $link) { cmd /c rmdir "$link" 2>$null }
    cmd /c mklink /J "$link" "$target" | Out-Null
    Log ("  anchor " + $n + " -> " + $target)
  }
  $lo = Join-Path $script:AppNew 'resources\app.asar.unpacked\dsh\node_modules\@deepseek-ai'
  foreach ($n in @('libreoffice-kit', 'libreoffice-kit-win32-x64')) {
    $src = Join-Path $lo $n
    if (Test-Path $src) {
      $link = Join-Path $script:Anchors $n
      if (Test-Path $link) { cmd /c rmdir "$link" 2>$null }
      cmd /c mklink /J "$link" "$src" | Out-Null
      Log ("  anchor " + $n + " -> bundled runtime")
    }
  }

  # tool-ralph anchor
  $ralphLink = Join-Path $script:Anchors 'dsh-tool-ralph'
  $ralphTarget = Join-Path $script:CkOld 'packages\workflow\tool-ralph'
  if (Test-Path $ralphTarget) {
    if (Test-Path $ralphLink) { cmd /c rmdir "$ralphLink" 2>$null }
    cmd /c mklink /J "$ralphLink" "$ralphTarget" | Out-Null
    Log ("  anchor dsh-tool-ralph -> " + $ralphTarget)
  }

  Step '4/5 edit profile'
  $pjPath = Join-Path $script:Profile 'package.json'
  $prof = Get-Content $pjPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $bundles = @(@($prof.dsh.profile.bundles) | Where-Object { $_ -ne '@deepseek-ai/dsh-experimental-schedule-bundle' })
  $prof.dsh.profile.bundles = $bundles
  Log ("  bundles: removed schedule-bundle, now " + $bundles.Count + " entries")

  if ($null -eq $prof.dependencies) {
    $prof | Add-Member -NotePropertyName dependencies -NotePropertyValue ([pscustomobject]@{})
  }
  $prof.dependencies | Add-Member -NotePropertyName '@deepseek-ai/dsh-tool-session-query' -NotePropertyValue '0.2.1-rc.5' -Force
  $prof.dependencies | Add-Member -NotePropertyName '@deepseek-ai/dsh-hooks-claude-code' -NotePropertyValue '0.2.1-rc.5' -Force
  $prof.dependencies | Add-Member -NotePropertyName '@deepseek-ai/dsh-hooks-codex' -NotePropertyValue '0.2.1-rc.5' -Force
  Write-JsonNoBom $pjPath $prof
  Log '  dependencies updated (session-query 0.2.1-rc.5, + 2 hooks bridges)'

  $patchPath = Join-Path $script:Profile 'cordis.patch.yml'
  $txt = Get-Content $patchPath -Raw -Encoding UTF8
  if ($txt -notmatch 'id:\s*time-context') {
    $yamlFile = Join-Path $PSScriptRoot 'schedule-mount.yaml'
    if (-not (Test-Path $yamlFile)) { Fail ("missing " + $yamlFile) }
    $block = Get-Content $yamlFile -Raw -Encoding UTF8
    Add-Content -Path $patchPath -Value $block -Encoding UTF8
    Log '  cordis.patch.yml: appended schedule mount block'
  } else {
    Log '  cordis.patch.yml: schedule mount already present, skipped'
  }

  Step '5/5 swap app'
  $appBak = $script:AppOld + '.pre-0.2.1-' + $script:ts
  try {
    Move-Item -LiteralPath $script:AppOld -Destination $appBak -Force
  } catch {
    Fail ("cannot rename old app (close DSH completely): " + $_.Exception.Message)
  }
  Log ("old app -> " + $appBak)
  try {
    Move-Item -LiteralPath $script:AppNew -Destination $script:AppOld -Force
  } catch {
    Move-Item -LiteralPath $appBak -Destination $script:AppOld -Force -ErrorAction SilentlyContinue
    Fail ("cannot move new app into place: " + $_.Exception.Message + "  (old app restored)")
  }
  # ★ 断言：exe 必须真的是 0.2.1
  $exePath = Join-Path $script:AppOld 'DeepSeek Harness.exe'
  if (-not (Test-Path $exePath)) {
    Move-Item -LiteralPath $script:AppOld -Destination ($script:AppOld + '.bad-' + $script:ts) -Force -ErrorAction SilentlyContinue
    Move-Item -LiteralPath $appBak -Destination $script:AppOld -Force -ErrorAction SilentlyContinue
    Fail "app has no exe after swap -> rolled back that step, aborting"
  }
  $exeVer = (Get-Item $exePath).VersionInfo.FileVersion
  if ($exeVer -ne '0.2.1-rc.5') {
    Fail ("app exe version is " + $exeVer + " (expected 0.2.1-rc.5)")
  }
  Log ("app-0.2.1 -> app  (verified exe " + $exeVer + ")")

  Log 'APPLY DONE - start the desktop app, then run -Mode Verify' 'OK'
  Log ("rollback: powershell -NoProfile -ExecutionPolicy Bypass -File `"" + $PSCommandPath + "`" -Mode Rollback")
}

# ==================================================================
function Invoke-Verify {
  Step 'VERIFY'

  $exe = Join-Path $script:AppOld 'DeepSeek Harness.exe'
  if (Test-Path $exe) { Log ("exe FileVersion = " + (Get-Item $exe).VersionInfo.FileVersion) }
  else { Log 'app has no exe' 'FAIL' }

  $pj = Join-Path $script:CkOld 'package.json'
  if (Test-Path $pj) {
    $j = Get-Content $pj -Raw -Encoding UTF8 | ConvertFrom-Json
    Log ("checkout version = " + $j.version)
  } else { Log 'checkout has no package.json' 'FAIL' }

  $hbDir = Join-Path $script:FishData '_audit\hooks'
  if (Test-Path $hbDir) {
    $hb = @(Get-ChildItem $hbDir -Filter 'heartbeat-*' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
    foreach ($h in $hb) { Log ("  heartbeat " + $h.Name + "  " + $h.LastWriteTime) }
    $names = ($hb | ForEach-Object { $_.Name }) -join ' '
    foreach ($k in @('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse')) {
      if ($names -match $k) { Log ("  OK heartbeat " + $k) }
      else { Log ("  WARN no heartbeat for " + $k) 'WARN' }
    }
  } else { Log ("heartbeat dir not found: " + $hbDir) 'WARN' }

  $procs = Get-DshProcs
  if ($procs.Count -gt 0) {
    Log ("DSH running: " + (($procs | ForEach-Object { $_.ProcessName + '#' + $_.Id }) -join ', '))
  } else { Log 'DSH not running (verify should run after start)' 'WARN' }

  $ppj = Join-Path $script:Profile 'package.json'
  if (Test-Path $ppj) {
    $p = Get-Content $ppj -Raw -Encoding UTF8 | ConvertFrom-Json
    Log ("bundles: " + (($p.dsh.profile.bundles) -join ', '))
    $deps = @($p.dependencies.PSObject.Properties | ForEach-Object { $_.Name + '=' + $_.Value }) -join ', '
    Log ("deps: " + $deps)
  }

  Log 'VERIFY DONE - paste the readings back to the AI' 'OK'
}

# ==================================================================
function Invoke-Rollback {
  Step 'ROLLBACK'

  $procs = Get-DshProcs
  if ($procs.Count -gt 0) { Fail 'Close DSH completely before rollback' }

  $appBak = Get-ChildItem $script:Root -Directory -Filter 'app.pre-0.2.1-*' |
            Sort-Object Name -Descending | Select-Object -First 1
  $ckBak = Get-ChildItem $script:Root -Directory -Filter 'checkout.pre-0.2.1-*' |
           Sort-Object Name -Descending | Select-Object -First 1

  if ($appBak) {
    $broken = 'app.broken-' + $script:ts
    if (Test-Path $script:AppOld) { Rename-Item $script:AppOld $broken }
    Rename-Item $appBak.FullName 'app'
    Log ("app restored from " + $appBak.Name)
  } else { Log 'no app.pre-0.2.1-* backup' 'FAIL' }

  if ($ckBak) {
    $brokenCk = 'checkout.broken-' + $script:ts
    if (Test-Path $script:CkOld) { Rename-Item $script:CkOld $brokenCk }
    Rename-Item $ckBak.FullName 'checkout'
    Log ("checkout restored from " + $ckBak.Name)
  } else { Log 'no checkout.pre-0.2.1-* backup' 'FAIL' }

  $bk = Get-ChildItem $script:BackupRoot -Directory -Filter 'pre-switch-*' |
        Sort-Object Name -Descending | Select-Object -First 1
  if ($bk) {
    foreach ($f in @('package.json', 'cordis.patch.yml', 'cordis.yml', 'pnpm-lock.yaml', 'pnpm-workspace.yaml')) {
      $s = Join-Path $bk.FullName $f
      if (Test-Path $s) { Copy-Item $s (Join-Path $script:Profile $f) -Force }
    }
    Log ("profile restored from " + $bk.Name)
  } else { Log 'no pre-switch-* backup; profile not restored' 'WARN' }

  Log 'ROLLBACK DONE - start DSH, then run -Mode Verify' 'OK'
}

# ==================================================================
Log ("DSH switch  Mode=" + $Mode + "  log=" + $script:log)
try {
  switch ($Mode) {
    'Prepare'  { Invoke-Prepare }
    'Apply'    { Invoke-Apply }
    'Verify'   { Invoke-Verify }
    'Rollback' { Invoke-Rollback }
  }
} catch {
  Log ("UNHANDLED ERROR: " + $_.Exception.Message) 'FAIL'
  $stack = ($_.ScriptStackTrace -split "`n" | Select-Object -First 6) -join "`n            "
  Log ("STACK: " + $stack) 'FAIL'
  Log ("日志文件: " + $script:log) 'FAIL'
  if ($Mode -eq 'Apply' -or $Mode -eq 'Rollback') {
    Write-Host ''
    Read-Host '出错了。按回车关闭窗口（日志路径见上）'
  }
  exit 1
}

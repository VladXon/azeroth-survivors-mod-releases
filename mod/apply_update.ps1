param([Parameter(Mandatory=$true)][string]$PlanPath, [switch]$NoRestart)
$ErrorActionPreference = 'Stop'
$plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
$game = [IO.Path]::GetFullPath($plan.game).TrimEnd('\')
$stage = [IO.Path]::GetFullPath($plan.stage).TrimEnd('\')
if ($plan.format -ne 1 -or [IO.Path]::GetDirectoryName($stage) -ne $game -or
    [IO.Path]::GetFileName($stage) -notmatch '^\.azs-update-[0-9a-f]{12}$' -or
    [IO.Path]::GetFullPath($PlanPath) -ne (Join-Path $stage 'plan.json')) {
    throw 'Invalid update paths'
}
$mod = Join-Path $game 'mod'
$newMod = Join-Path $stage 'mod'
$backup = Join-Path $game '.azs-mod-backup'
$oldMod = Join-Path $backup 'mod'
$marker = Join-Path $backup 'owner.json'
$rootNames = @('Azeroth Survivors MODDED.bat', 'Azeroth Survivors MODDED Онлайн.bat',
               'ПРОЧИТАЙ - мод.txt', 'УСТАНОВКА И ОБНОВЛЕНИЕ.txt')
$logRoot = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'AzerothSurvivorsMod' } else { $stage }
$log = Join-Path $logRoot 'updater.log'
$movedOld = $false
$movedNew = $false
$writtenRoots = @()
$success = $false
function Write-UpdateLog([string]$message) {
    try {
        New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
        $message | Add-Content -LiteralPath $log -Encoding UTF8
    } catch { } # Logging cannot prevent installation rollback or restarting.
}
function Assert-RootFile([string]$path) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))) {
        throw 'Root update target cannot be a directory or link'
    }
}
try {
    if (-not $NoRestart) {
        $parent = Get-Process -Id $plan.parent_pid -ErrorAction SilentlyContinue
        if ($parent -and -not $parent.WaitForExit(30000)) { throw 'Launcher did not exit' }
    }
    if (Test-Path -LiteralPath (Join-Path $mod '.git')) { throw 'Git checkout is not a portable installation' }
    if ((Get-Item -LiteralPath $game).Attributes -band [IO.FileAttributes]::ReparsePoint -or
        (Get-Item -LiteralPath $stage).Attributes -band [IO.FileAttributes]::ReparsePoint -or
        (Get-Item -LiteralPath $mod).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Update directories cannot be junctions'
    }
    $version = (Get-Content -LiteralPath (Join-Path $newMod 'VERSION') -Raw -Encoding UTF8).Trim()
    if ($version -ne $plan.version) { throw 'Staged version changed' }
    if (Test-Path -LiteralPath $backup) {
        if ((Get-Item -LiteralPath $backup).Attributes -band [IO.FileAttributes]::ReparsePoint -or
            -not (Test-Path -LiteralPath $marker)) { throw 'Unrecognized backup directory' }
        $owner = Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($owner.magic -ne 'AZS-MOD-BACKUP-1' -or $owner.game -ne $game) { throw 'Unrecognized backup owner' }
        $pending = New-Object System.Collections.Stack
        $pending.Push($backup)
        while ($pending.Count -gt 0) {
            foreach ($item in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Backup contains a link' }
                if ($item.PSIsContainer) { $pending.Push($item.FullName) }
            }
        }
        Remove-Item -LiteralPath $backup -Recurse -Force
    }
    New-Item -ItemType Directory -Path (Join-Path $backup 'root') | Out-Null
    @{magic='AZS-MOD-BACKUP-1';game=$game} | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8
    foreach ($name in $rootNames) {
        $dest = Join-Path $game $name
        Assert-RootFile $dest
        if (Test-Path -LiteralPath $dest) {
            if (-not (Test-Path -LiteralPath $dest -PathType Leaf)) { throw 'Root update target is not a file' }
            Copy-Item -LiteralPath $dest -Destination (Join-Path $backup "root\$name")
        }
    }
    Move-Item -LiteralPath $mod -Destination $oldMod
    $movedOld = $true
    Move-Item -LiteralPath $newMod -Destination $mod
    $movedNew = $true
    foreach ($name in $rootNames) {
        $source = Join-Path $stage $name
        if (Test-Path -LiteralPath $source) {
            Assert-RootFile (Join-Path $game $name)
            $writtenRoots += $name
            Move-Item -LiteralPath $source -Destination (Join-Path $game $name) -Force
        }
    }
    $success = $true
    Write-UpdateLog "$(Get-Date -Format o) Installed $version"
} catch {
    Write-UpdateLog "$(Get-Date -Format o) Update failed: $_"
    if ($movedNew) { Move-Item -LiteralPath $mod -Destination (Join-Path $stage 'failed-mod') }
    if ($movedOld) { Move-Item -LiteralPath $oldMod -Destination $mod }
    foreach ($name in $writtenRoots) {
        try {
            $saved = Join-Path $backup "root\$name"
            $dest = Join-Path $game $name
            Assert-RootFile $dest
            if (Test-Path -LiteralPath $saved) { Move-Item -LiteralPath $saved -Destination $dest -Force }
            elseif (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest }
        } catch { Write-UpdateLog "Rollback root file failed: $_" }
    }
} finally {
    if (-not $NoRestart -and (Test-Path -LiteralPath (Join-Path $mod 'runtime\AzerothSurvivorsMod.exe'))) {
        $env:AZS_SKIP_UPDATE = '1'
        $runtime = Join-Path $mod 'runtime\AzerothSurvivorsMod.exe'
        if ($plan.online) { Start-Process -FilePath $runtime -ArgumentList '--pvp' -WorkingDirectory $game -WindowStyle Hidden }
        else { Start-Process -FilePath $runtime -WorkingDirectory $game -WindowStyle Hidden }
    }
    # Both resolved paths were checked above; no game or backup tree is deleted.
    if ($success) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
if (-not $success) { exit 1 }

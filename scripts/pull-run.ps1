<#
.SYNOPSIS
  Save a run's full log locally as Markdown, so it can be read and searched without waiting for the platform.

  Writes .swarm/runs/<run>-log.md (whole log, replaced on every pull) and adds a line to .swarm/runs/<run>.md (notes:
  status, scores and MCP results go there). .swarm/ is git-ignored and never packaged: run logs hold scenario
  values, which must never get into the package.

.EXAMPLE
  .\scripts\pull-run.ps1 run_415bcaed321664dc            # once (run ended or a snapshot)
  .\scripts\pull-run.ps1 run_415bcaed321664dc -Follow    # keep writing until the run ends

  From Git Bash / a Claude Code Monitor:
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/pull-run.ps1 <run> -Follow
#>
param(
    [Parameter(Mandatory = $true)][string]$RunId,
    [switch]$Follow
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$dir = ".swarm/runs"
New-Item -ItemType Directory -Force $dir | Out-Null
$notes = "$dir/$RunId.md"
$logFile = "$dir/$RunId-log.md"
$now = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm 'UTC'")

# cmd merges stderr (atira writes progress there) without PowerShell 5.1 turning each line into an error record
$status = (cmd /c "atira status 2>&1" | Select-String -SimpleMatch $RunId | Select-Object -First 1)
$status = if ($status) { $status.Line.Trim() } else { "(not in atira status)" }

if (-not (Test-Path $notes)) {
    Set-Content -Encoding utf8 $notes "# $RunId`n`nFull log: [$RunId-log.md]($RunId-log.md). Notes and MCP pulls below, newest last.`n"
}
Add-Content -Encoding utf8 $notes "`n## $now - log pulled`n`nStatus: $status`n"

# The platform keeps only the tail of a long log: a later pull can hold FEWER early lines than an earlier one. So the
# file is never replaced: lines it does not have yet are appended (in the order the platform returns them).
$known = New-Object 'System.Collections.Generic.HashSet[string]'
if (Test-Path $logFile) {
    Get-Content -Encoding utf8 $logFile | Where-Object { $_ -ne '```' } | ForEach-Object { [void]$known.Add($_) }
    $kept = Get-Content -Encoding utf8 $logFile | Where-Object { $_ -ne '```' }
    Set-Content -Encoding utf8 $logFile $kept
} else {
    Set-Content -Encoding utf8 $logFile ("# $RunId log`n`nLines from every pull, merged (the platform keeps only the tail " +
                                         "of a long log, so early lines exist only in earlier pulls).`n`n" + '```text')
}
Add-Content -Encoding utf8 $logFile "--- pull $now ($status) ---"
$add = { param($line) if ($known.Add($line)) { Add-Content -Encoding utf8 $logFile $line } }
if ($Follow) {
    cmd /c "atira logs $RunId --follow 2>&1" | ForEach-Object { & $add $_; $_ }
} else {
    cmd /c "atira logs $RunId 2>&1" | ForEach-Object { & $add $_ }
}
Add-Content -Encoding utf8 $logFile '```'
$lines = (Get-Content $logFile | Measure-Object -Line).Lines
Write-Host "saved $logFile ($lines lines) and $notes"

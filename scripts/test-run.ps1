<#
.SYNOPSIS
  Start a sandbox test run of this package (public data, unscored) and optionally follow its key log lines.

.EXAMPLE
  .\scripts\test-run.ps1                 # unit tests, then start the run; prints its id and page
  .\scripts\test-run.ps1 -Follow         # ... and stream milestones + failures until the run ends
  .\scripts\test-run.ps1 -SkipTests -Follow

  From Git Bash / a Claude Code Monitor:
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-run.ps1 -Follow
#>
param(
    [switch]$Follow,       # stream milestone/failure lines of the run's log until it ends
    [switch]$SkipTests,    # skip the local unit tests before packing
    [switch]$Open,         # open the run page in the browser
    [switch]$DryRun        # only show what would be packed
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

# Lines worth seeing while a run goes: phases, scraper/analyst results, the submission, and every kind of failure.
$Milestones = 'browse done|swarm phase|site roles|brief facts|early requirements|current revision|positions,|' +
              'discount \{|materials:|verification:|submission attempt|answer \(entered|model calls:|nothing saved|' +
              'board after collect|gaps|follow-up|candidates|tip for|guard .*->|' +
              'tab job failed|timed out|failed|Traceback|FATAL|CRITICAL|No lifecycle events'

if (-not $SkipTests) {
    Write-Host "== unit tests"
    $tests = cmd /c "uv run --no-sync python -m unittest discover -s tests -q 2>&1"
    if ($LASTEXITCODE -ne 0) { $tests | Select-Object -Last 40; throw "unit tests failed: not starting a test run" }
    $tests | Select-String -Pattern '^Ran \d+ tests|^OK' | ForEach-Object { $_.Line }
}

if ($DryRun) {
    atira test-run . --dry-run
    exit $LASTEXITCODE
}

Write-Host "== starting test run"
$openFlag = if ($Open) { "" } else { "--no-open" }
# atira prints the run id on stderr: merge it in cmd (PowerShell 5.1 would turn stderr lines into errors).
$out = (cmd /c "atira test-run . $openFlag 2>&1" | Out-String)
Write-Host $out.Trim()
$runId = [regex]::Match($out, 'run_[0-9a-f]{16}').Value
if (-not $runId) { throw "no run id in the atira output" }

Write-Host "== run $runId  https://ehl-challenge.atira.ai/runs/$runId"
if (-not $Follow) {
    Write-Host "follow its log: atira logs $runId --follow"
    exit 0
}

# The whole log goes to .swarm/runs/<run>-log.md (git-ignored, never packaged; see CLAUDE.md "Run data"), the milestones
# to the console. cmd merges stderr into stdout without PowerShell 5.1 wrapping each stderr line as an error record.
New-Item -ItemType Directory -Force ".swarm/runs" | Out-Null
$logFile = ".swarm/runs/$runId-log.md"
Set-Content -Encoding utf8 $logFile ("# $runId log`n`nFollowed from the start by scripts/test-run.ps1.`n`n" + '```text')
cmd /c "atira logs $runId --follow 2>&1" | ForEach-Object {
    Add-Content -Encoding utf8 $logFile $_
    if ($_ -match $Milestones) { $_ }
}
Add-Content -Encoding utf8 $logFile '```'
Write-Host "== log stream of $runId ended; whole log in $logFile"

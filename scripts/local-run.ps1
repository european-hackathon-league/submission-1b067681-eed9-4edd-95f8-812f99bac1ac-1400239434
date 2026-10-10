<#
.SYNOPSIS
  Run the swarm (or some agents) locally against the public scouting sites, headless, and save the whole log as
  Markdown in .swarm/local/ (git-ignored, never packaged). Nothing entered with scouting logins counts.

  Needs `atira models proxy` running (the local Qwen proxy). Locally every browse starts its own Chromium.

.EXAMPLE
  .\scripts\local-run.ps1 -Only "BriefReader,BidSubmitter" -Name submitter
  .\scripts\local-run.ps1 -Only "BriefReader,MailScraper,DiscountAnalyzer" -Name mail -Board .swarm/board.json
#>
param(
    [string]$Only = "",          # SWARM_ONLY: comma-separated agent names (empty = whole swarm)
    [string]$Name = "local",     # label for the log file
    [string]$Board = "",         # SWARM_BOARD_IN: start from a saved board (skip scraping that is already done)
    [switch]$Visible             # show the browsers (default headless)
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force ".swarm/local" | Out-Null
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmm")
$log = ".swarm/local/$stamp-$Name.md"

atira scout --env | Out-String | Invoke-Expression
$env:SWARM_ONLY = $Only
$env:SWARM_BOARD_IN = $Board
$env:HEADLESS = if ($Visible) { "0" } else { "1" }
$env:PYTHONIOENCODING = "utf-8"

Set-Content -Encoding utf8 $log ("# Local run $stamp ($Name)`n`nSWARM_ONLY=$Only; SWARM_BOARD_IN=$Board`n`n" + '```text')
cmd /c "uv run --no-sync python agent.py 2>&1" | ForEach-Object { Add-Content -Encoding utf8 $log $_ }
Add-Content -Encoding utf8 $log '```'
Write-Host "log: $log"

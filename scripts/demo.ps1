# Demo driver for the 5-minute video.
#
#   powershell -ExecutionPolicy Bypass -File scripts\demo.ps1          # press Enter between steps
#   powershell -ExecutionPolicy Bypass -File scripts\demo.ps1 -Auto    # no pauses (rehearsal / timing check)
#   powershell -ExecutionPolicy Bypass -File scripts\demo.ps1 -From 3  # start at step 3
#
# Each step prints the command it is about to run, so the recording shows a real
# command line rather than a script hiding one. Nothing here is video-only: the
# same commands are in README -> Reproducing Our Results.

param(
  [switch]$Auto,
  [int]$From = 1
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$steps = @(
  @{ Label = "DEMO 1 - intent-conditioned weights (requirement 3)"
     Beat  = "point at: INTENT troubleshooting -> WEIGHTS forum 1.00, docs 0.65"
     Args  = @("scripts\query.py", "Why do my jobs retry forever when the API returns 429?", "--explain")
     Head  = 26 },

  @{ Label = "DEMO 2 - reranking recovers a stage-1 failure (requirement 4)"
     Beat  = "point at: row 1, was #20, move +19, top1_changed=True"
     Args  = @("scripts\query.py", "How do I see the payload of a dead-lettered job from the CLI?", "--explain")
     Head  = 22 },

  @{ Label = "DEMO 3 - a wrong answer with 47 votes (requirement 5)"
     Beat  = "point at: CONFLICTS [misconception] -> rule=authority_canonical, then the 'Note on conflicting information' block"
     Args  = @("scripts\query.py", "Can I set timeout=0 to disable the timeout?", "--explain")
     Head  = 26 },

  @{ Label = "DEMO 4 - NOT a contradiction (the zero false-positive rate)"
     Beat  = "point at: CONFLICTS none / NON-CONFLICTS 3 (complementary) - no warning emitted"
     Args  = @("scripts\query.py", "What is the default worker concurrency?", "--explain")
     Head  = 24 },

  @{ Label = "DEMO 5 - out of scope: refuse instead of improvise"
     Beat  = "point at: the refusal text and the 0% term-coverage reason"
     Args  = @("scripts\query.py", "How do I integrate Zephyr with Kubernetes CronJobs?")
     Head  = 8 },

  @{ Label = "DEMO 6 - the audit trail for the DLQ query (requirement 6)"
     Beat  = "point at: WEIGHTS, SOURCES USED, rank was->now, rule fired, citations"
     Args  = @("scripts\show_log.py", "--grep", "dead-lettered")
     Head  = 40 }
)

for ($i = $From - 1; $i -lt $steps.Count; $i++) {
  $s = $steps[$i]
  $shown = ($s.Args | ForEach-Object { if ($_ -match '[ ?]') { '"' + $_ + '"' } else { $_ } }) -join ' '

  Write-Host ""
  Write-Host ("=" * 78) -ForegroundColor DarkGray
  Write-Host ("  {0}" -f $s.Label) -ForegroundColor Cyan
  Write-Host ("  {0}" -f $s.Beat) -ForegroundColor DarkYellow
  Write-Host ("=" * 78) -ForegroundColor DarkGray
  Write-Host ("PS> python {0}" -f $shown) -ForegroundColor Green
  if (-not $Auto) { Read-Host "  [Enter to run]" | Out-Null }

  & $py @($s.Args) 2>&1 | Select-Object -First $s.Head

  if (-not $Auto -and $i -lt $steps.Count - 1) { Read-Host "  [Enter for next step]" | Out-Null }
}

Write-Host ""
Write-Host "demo complete" -ForegroundColor Cyan

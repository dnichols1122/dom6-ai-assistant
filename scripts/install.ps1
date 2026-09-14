<#
.SYNOPSIS
    Set up the Dominions 6 assistant on Windows.

.DESCRIPTION
    Installs dependencies, builds the game reference database, and fetches the
    manual, the community wiki and the video-transcript fetcher.

    Everything is installed by default. The wiki mirror is around half an hour
    of polite scraping and is by far the slowest part; -Minimal or -Ask skip it.

    Re-running is safe: every step checks whether it has already been done.

    Nothing here is silent about the network. The reference data comes from the
    dom6inspector project, the manual from Illwinter, transcripts from YouTube.
    Each is fetched onto this machine and none is redistributed.

.EXAMPLE
    .\scripts\install.ps1
    Everything: the assistant and all reference libraries.

.EXAMPLE
    .\scripts\install.ps1 -Minimal
    Just the assistant, no reference libraries.

.EXAMPLE
    .\scripts\install.ps1 -Ask
    Choose each library individually.

.EXAMPLE
    .\scripts\install.ps1 -Start
    Also start the server when finished.

.EXAMPLE
    .\scripts\install.ps1 -Yes
    Consent in advance to installing uv, if it is missing.

.NOTES
    If Windows refuses to run this because of the execution policy, start it
    with that policy bypassed for this one process only:

        powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#>
[CmdletBinding()]
param(
    [switch]$All,      # kept for compatibility; this is the default
    [switch]$Minimal,
    [switch]$Ask,
    [switch]$Start,
    [switch]$Yes       # consent, in advance, to installing uv from the network
)

# Everything, unless told otherwise: the reference libraries are the point of
# the thing, and making people opt in one at a time meant most would end up
# without them and wonder why the assistant could not cite the manual.
$mode = if ($Minimal) { 'minimal' } elseif ($Ask) { 'ask' } else { 'all' }

$ErrorActionPreference = 'Stop'

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot }
             else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$repo = Split-Path -Parent $scriptDir
Set-Location $repo

function Say  { param([string]$Text) Write-Host "`n$Text" -ForegroundColor White }
function Note { param([string]$Text) Write-Host "  $Text" }
function Fail {
    param([string]$Text)
    Write-Host "`n$Text" -ForegroundColor Red
    exit 1
}

# Consent for the one thing that runs someone else's code from the network.
# Deliberately not Ask(): Ask answers yes automatically in the default mode,
# which would turn a remote installer into something that happens to you
# rather than something you agreed to.
function Confirm-Uv {
    if ($Yes) {
        Note '(-Yes given, so installing uv without asking)'
        return $true
    }
    $reply = Read-Host '  Run that now? [Y/n]'
    return ($reply -eq '') -or ($reply -match '^[Yy]')
}

function Ask {
    param([string]$Question)
    if ($mode -eq 'all')     { return $true }
    if ($mode -eq 'minimal') { return $false }
    # Enter means yes, matching the default-everything behaviour.
    $reply = Read-Host "  $Question [Y/n]"
    return ($reply -eq '') -or ($reply -match '^[Yy]')
}

# Run a command and stop if it failed. PowerShell does not treat a non-zero
# exit code from a native program as an error, so it has to be checked.
function Invoke-Step {
    param([string]$Exe, [string[]]$Arguments, [string]$WhenItFails)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Fail $WhenItFails }
}

# --- uv ---------------------------------------------------------------------
Say 'Checking for uv'
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    # uv installs here, and this session's PATH may predate that.
    foreach ($dir in @("$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")) {
        if (Test-Path (Join-Path $dir 'uv.exe')) { $env:PATH = "$dir;$env:PATH" }
    }
    $uv = Get-Command uv -ErrorAction SilentlyContinue
}

if (-not $uv) {
    Note 'uv is not installed. It manages the Python version and dependencies.'
    Note 'The official installer is:'
    Note '    irm https://astral.sh/uv/install.ps1 | iex'
    if (Confirm-Uv) {
        Invoke-Expression (Invoke-RestMethod https://astral.sh/uv/install.ps1)
        $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            Fail 'uv installed but is not on PATH. Open a new terminal and re-run this.'
        }
    } else {
        Fail @'
uv is required. Install it with the command above, or from
  https://docs.astral.sh/uv/ -- then run this script again.
'@
    }
}
Note (uv --version | Select-Object -First 1)

# --- what to install --------------------------------------------------------
$extras = @('--extra', 'web', '--extra', 'dev')
$wantManual = $false
$wantWiki   = $false
$wantVideos = $false

Say 'Reference libraries'
if ($mode -eq 'all') {
    Note 'Installing all of them. This is the slow part:'
    Note "  - Illwinter's manual        ~18 MB, about a minute"
    Note '  - video transcript fetcher  seconds; you add videos later'
    Note '  - community wiki mirror     around 30 minutes'
    Note ''
    Note 'Ctrl-C now and re-run with -Minimal to skip them, or -Ask to choose.'
    Note ''
} elseif ($mode -eq 'minimal') {
    Note 'Skipping all of them. Add any later; see RUNNING.md.'
} else {
    Note 'The assistant works without these. Each can be added later.'
}
Write-Host ''
if (Ask "Illwinter's manual? ~18 MB, about a minute. Searchable, cited by page.") {
    $wantManual = $true
    $extras += @('--extra', 'manual')
}
if (Ask 'Strategy video transcripts? Installs the fetcher; you add videos later.') {
    $wantVideos = $true
    $extras += @('--extra', 'videos')
}
if (Ask 'The community wiki mirror? Around 30 minutes of polite scraping.') {
    $wantWiki = $true
}

# --- dependencies -----------------------------------------------------------
Say 'Installing dependencies'
Note "uv sync $($extras -join ' ')"
Invoke-Step 'uv' (@('sync') + $extras) 'Dependency install failed. The output above says why.'
Note 'done'

# --- game reference data ----------------------------------------------------
Say 'Building the game reference database'
if (Test-Path 'knowledge\reference\reference.sqlite3') { Note 'already built; refreshing' }
Note 'Fetching unit, spell, event and nation data (from the dom6inspector project).'
Invoke-Step 'uv' @('run', 'python', '-m', 'dom6_assistant.reference.build_db', '--refresh') `
    'Reference build failed. Without it the assistant cannot name anything.'

# --- optional libraries -----------------------------------------------------
# A failure here is not fatal: the assistant reports the source as unavailable
# rather than inventing what it would have said.
if ($wantManual) {
    Say "Fetching Illwinter's manual"
    & uv run dom6-assistant manual-fetch
    if ($LASTEXITCODE -eq 0) { & uv run dom6-assistant manual-build }
    if ($LASTEXITCODE -ne 0) { Note 'Manual setup failed; it will show as unavailable.' }
}

if ($wantWiki) {
    Say 'Mirroring the community wiki (this is the slow one)'
    & uv run dom6-assistant wiki-scrape
    if ($LASTEXITCODE -eq 0) { & uv run dom6-assistant wiki-build }
    if ($LASTEXITCODE -ne 0) { Note 'Wiki setup failed; it will show as unavailable.' }
}

if ($wantVideos) {
    Say 'Video transcripts'
    Note 'The fetcher is installed. Add a video whenever you like:'
    Note '    uv run dom6-assistant video-add <youtube url>'
    Note 'or paste a YouTube link into the chat and ask for it to be indexed.'
}

# --- where saves are --------------------------------------------------------
Say 'Your Dominions saves'
$saveRoot = & uv run python -c 'from dom6_assistant.paths import describe_save_root; print(describe_save_root())' 2>$null
if ($saveRoot) {
    Note $saveRoot
    $rootOnly = ($saveRoot -split ' \(')[0]
    if (Test-Path $rootOnly) {
        Note 'found it; turns will be read automatically while the server runs'
    } else {
        Note 'not found yet. That is fine if you have not played a game.'
        Note 'If your saves live elsewhere, set DOM6_SAVE_ROOT to that folder.'
    }
}

# --- done -------------------------------------------------------------------
Say 'Ready'
Note 'Start it with:'
Note '    uv run uvicorn dom6_assistant.ui.app:app --port 8001'
Note ''
Note 'Then open http://127.0.0.1:8001/ and point the Model endpoint box at'
Note 'your model. Test connection tells you whether it worked.'
Write-Host ''

# Only on request, or when the questions were being asked anyway. A default
# install that ends in a blocking server would look like it had hung.
if ($Start -or ($mode -eq 'ask' -and (Ask 'Start it now?'))) {
    & uv run uvicorn dom6_assistant.ui.app:app --port 8001
}

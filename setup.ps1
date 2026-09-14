<#
  MM Story Bot - one-shot setup.

  Creates the GitHub repo, pushes the code, creates and enables the gh-pages
  branch, stores your Instagram secrets, and fires a dry run so you can see
  it work before anything is posted for real.

  Run from inside the mm-story-bot folder:

      powershell -ExecutionPolicy Bypass -File .\setup.ps1

  Everything is checked before anything is created, so a wrong token fails
  in five seconds instead of leaving you with a half-made repo.
#>

[CmdletBinding()]
param(
    [string]$RepoName  = "mm-story-bot",
    [string]$IgToken   = "",
    [string]$IgUserId  = "",
    [switch]$SkipDryRun
)

$ErrorActionPreference = "Stop"
$GRAPH = "https://graph.facebook.com/v21.0"

function Say  ($m) { Write-Host "  $m" }
function Step ($m) { Write-Host "`n> $m" -ForegroundColor Cyan }
function Good ($m) { Write-Host "  OK  $m" -ForegroundColor Green }
function Bad  ($m) { Write-Host "`nX  $m" -ForegroundColor Red; exit 1 }

# --------------------------------------------------------------------------
Step "Putting the workflow in place"
# Some file-sync paths cannot write into .github/workflows, so the workflow
# also ships at workflow/story.yml. Put it where GitHub Actions looks.
if ((-not (Test-Path ".github/workflows/story.yml")) -and (Test-Path "workflow/story.yml")) {
    New-Item -ItemType Directory -Force -Path ".github/workflows" | Out-Null
    Copy-Item "workflow/story.yml" ".github/workflows/story.yml" -Force
    Good ".github/workflows/story.yml created"
}
if (-not (Test-Path ".github/workflows/story.yml")) {
    Bad "workflow/story.yml is missing - re-download the folder."
}

Step "Checking tools"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Bad "git is not installed. Install Git for Windows from https://git-scm.com then run this again."
}

function Add-GhToPath {
    foreach ($d in @("$env:ProgramFiles\GitHub CLI",
                     "${env:ProgramFiles(x86)}\GitHub CLI",
                     "$env:LOCALAPPDATA\Programs\GitHub CLI")) {
        if (Test-Path (Join-Path $d "gh.exe")) { $env:PATH = "$d;$env:PATH"; return $true }
    }
    return $false
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { Add-GhToPath | Out-Null }

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Say "GitHub CLI (gh) is not installed. Installing it now - this takes a minute."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id GitHub.cli -e --source winget `
            --accept-package-agreements --accept-source-agreements
        Add-GhToPath | Out-Null
    } else {
        Say "winget is not available on this machine."
    }
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Bad ("GitHub CLI still not found.`n" +
         "   Install it from https://cli.github.com (or run: winget install --id GitHub.cli)`n" +
         "   then run this again.")
}
Good "git and gh found"

gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Say "You are not logged in to GitHub CLI. Opening the login flow ..."
    gh auth login
    if ($LASTEXITCODE -ne 0) { Bad "GitHub login did not complete." }
}
$ghUser = (gh api user --jq .login).Trim()
Good "Logged in to GitHub as $ghUser"

# --------------------------------------------------------------------------
Step "Checking your Instagram token"

# Windows consoles truncate a pasted line at ~254 characters, and Meta tokens
# are longer than that - so a file is the reliable way in.
if (-not $IgToken -and (Test-Path "token.txt")) {
    $IgToken = (Get-Content "token.txt" -Raw)
    Good "Read the token from token.txt"
}

if (-not $IgToken) {
    Say "Best way: put the token in a file, because Windows cuts off long pastes."
    Say "  1. In this folder, create a file called  token.txt"
    Say "  2. Paste the token into it and save"
    Say "  3. Run RUN-SETUP.bat again"
    Say ""
    $IgToken = Read-Host "  Or paste it here and press Enter (may get cut off)"
}

$IgToken = ($IgToken -replace "\s", "")
if (-not $IgToken) { Bad "No token given." }

Say "Token received: $($IgToken.Length) characters."
if ($IgToken.Length -lt 100) {
    Bad ("That is too short for a Meta access token - it was cut off in transit.`n" +
         "   Put the full token in a file called token.txt in this folder, then run`n" +
         "   RUN-SETUP.bat again. That path has no length limit.")
}

# Works with either a long-lived USER token or a never-expiring PAGE token.
$fbPageId = ""; $fbPageName = ""; $fbOk = $false

if (-not $IgUserId -or -not $fbPageId) {
    Say "Looking up your account ..."
    $accounts = $null; $me = $null
    try {
        $accounts = Invoke-RestMethod -Method Get `
            -Uri "$GRAPH/me/accounts?fields=id,name,access_token,instagram_business_account{id,username}&access_token=$IgToken"
    } catch { $accounts = $null }
    try {
        $me = Invoke-RestMethod -Method Get `
            -Uri "$GRAPH/me?fields=id,name,category,instagram_business_account{id,username}&access_token=$IgToken"
    } catch { $me = $null }

    $pages = @()
    if ($accounts -and $accounts.data) { $pages = @($accounts.data) }
    if ($pages.Count -eq 0 -and $me -and $me.category -and $me.id) {
        # A Page access token: /me IS the Page.
        $pages = @([pscustomobject]@{
            id = $me.id; name = $me.name; access_token = "self"
            instagram_business_account = $me.instagram_business_account
        })
    }

    $linked = @($pages | Where-Object { $_.instagram_business_account })
    if ($linked.Count -eq 0) {
        Bad ("That token has no Instagram Business account linked to a Facebook Page.`n" +
             "   Generate a fresh long-lived token with instagram_basic, instagram_content_publish,`n" +
             "   pages_show_list, pages_manage_posts and pages_read_engagement.")
    }

    if ($IgUserId) {
        $pick = @($linked | Where-Object { $_.instagram_business_account.id -eq $IgUserId })[0]
        if (-not $pick) { $pick = $linked[0] }
    } elseif ($linked.Count -gt 1) {
        for ($i = 0; $i -lt $linked.Count; $i++) {
            Say "  [$i] @$($linked[$i].instagram_business_account.username)  (page: $($linked[$i].name))"
        }
        $pick = $linked[[int](Read-Host "Which account should post the stories? Enter the number")]
    } else {
        $pick = $linked[0]
    }

    $IgUserId   = $pick.instagram_business_account.id
    $fbPageId   = $pick.id
    $fbPageName = $pick.name
    $fbOk       = [bool]$pick.access_token

    Good "Instagram: @$($pick.instagram_business_account.username)  (id $IgUserId)"
    if ($fbOk -and $fbPageId) {
        Good "Facebook Page: $fbPageName  (id $fbPageId)"
    } else {
        Say "WARNING: no usable Facebook Page on this token - Facebook stories will be skipped."
        Say "         The token needs pages_show_list, pages_manage_posts and pages_read_engagement."
    }
}

Say "Checking the token can actually read your posts ..."
try {
    $probe = Invoke-RestMethod -Method Get `
        -Uri "$GRAPH/$IgUserId/media?fields=id,media_type&limit=25&access_token=$IgToken"
} catch {
    Bad ("Could not read your media: " + $_.Exception.Message)
}
$videoCount = @($probe.data | Where-Object { $_.media_type -eq "VIDEO" }).Count
if ($videoCount -eq 0) {
    Say "WARNING: no videos in the 25 most recent posts. The bot only shares videos."
} else {
    Good "$videoCount video(s) in the newest 25 posts - plenty to rotate"
}

# --------------------------------------------------------------------------
Step "Creating the repository"

$slug = "$ghUser/$RepoName"
$exists = $false
gh repo view $slug 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) { $exists = $true }

if (-not (Test-Path ".git")) {
    git init -q
    git checkout -q -B main
}
git add -A | Out-Null
git -c user.name="$ghUser" -c user.email="$ghUser@users.noreply.github.com" `
    commit -q -m "MM Story Bot" --allow-empty | Out-Null

if ($exists) {
    Say "$slug already exists - pushing to it."
    git remote remove origin 2>&1 | Out-Null
    git remote add origin "https://github.com/$slug.git"
    git push -q -u origin main --force
} else {
    gh repo create $RepoName --public --source . --remote origin --push
    if ($LASTEXITCODE -ne 0) { Bad "Could not create the repository." }
}
Good "Code is on https://github.com/$slug"

# The repo must be public or GitHub Pages will not serve the video to Instagram.
$isPrivate = (gh api "repos/$slug" --jq .private).Trim()
if ($isPrivate -eq "true") {
    Say "Repo is private - making it public so Instagram can fetch the files ..."
    gh repo edit $slug --visibility public --accept-visibility-change-consequences
}

# --------------------------------------------------------------------------
Step "Setting up GitHub Pages"

# Pages needs the branch to exist first, so seed an empty one.
gh api "repos/$slug/branches/gh-pages" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("mmsb-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    Push-Location $tmp
    git init -q
    "" | Set-Content -Path ".nojekyll" -NoNewline
    "mm-story-bot media host" | Set-Content -Path "index.html"
    '{}' | Set-Content -Path "manifest.json"
    git add -A | Out-Null
    git -c user.name="mm-story-bot" -c user.email="mm-story-bot@users.noreply.github.com" `
        commit -q -m "init media host"
    git push -q --force "https://github.com/$slug.git" "HEAD:gh-pages"
    Pop-Location
    Remove-Item -Recurse -Force $tmp
    Good "gh-pages branch created"
} else {
    Good "gh-pages branch already there"
}

$pagesBody = '{"source":{"branch":"gh-pages","path":"/"}}'
$pagesBody | gh api "repos/$slug/pages" --method POST --input - 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    $pagesBody | gh api "repos/$slug/pages" --method PUT --input - 2>&1 | Out-Null
}
$pagesUrl = ""
try { $pagesUrl = (gh api "repos/$slug/pages" --jq .html_url).Trim() } catch {}
if ($pagesUrl) { Good "Pages: $pagesUrl" }
else { Say "Could not confirm Pages automatically - check Settings > Pages (branch gh-pages, root)." }

# --------------------------------------------------------------------------
Step "Storing your secrets"

# PowerShell has no stdin redirection for native commands, so --body is used.
gh secret set IG_USER_ID      --repo $slug --body $IgUserId
gh secret set IG_ACCESS_TOKEN --repo $slug --body $IgToken
Good "IG_USER_ID and IG_ACCESS_TOKEN stored (write-only - not readable again)"

$hook = Read-Host "Discord webhook for failure alerts (optional - press Enter to skip)"
if ($hook) {
    gh secret set DISCORD_WEBHOOK --repo $slug --body $hook
    Good "DISCORD_WEBHOOK stored"
}

# --------------------------------------------------------------------------
if (-not $SkipDryRun) {
    Step "Running a dry run (nothing gets posted)"
    Say "Pages can take a minute to go live on a brand new repo - that is normal."
    gh workflow run story.yml --repo $slug -f dry_run=true
    if ($LASTEXITCODE -ne 0) {
        Say "Could not start the run automatically. Go to the Actions tab and run it by hand."
    } else {
        Start-Sleep -Seconds 10
        $runId = (gh run list --repo $slug --workflow story.yml --limit 1 `
                    --json databaseId --jq ".[0].databaseId" 2>$null)
        if ($runId) {
            gh run watch $runId.Trim() --repo $slug --exit-status
            if ($LASTEXITCODE -eq 0) { Good "Dry run passed - the pipeline works end to end." }
            else { Say "Dry run failed. Open the log: gh run view $($runId.Trim()) --repo $slug --log-failed" }
        } else {
            Say "Run started. Watch it at https://github.com/$slug/actions"
        }
    }
}

Write-Host "`n===========================================================" -ForegroundColor Yellow
Write-Host " Setup done." -ForegroundColor Yellow
Write-Host "===========================================================" -ForegroundColor Yellow
Say "Repo:    https://github.com/$slug"
Say "Actions: https://github.com/$slug/actions"
Say ""
Say "One real story now (so you can see it on your profile):"
Say "    gh workflow run story.yml --repo $slug -f dry_run=false"
Say ""
Say "After that it runs on its own: 7 stories a day at"
Say "10am, 12, 2, 4, 6, 8 and 10pm IST."

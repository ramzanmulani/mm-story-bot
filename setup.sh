#!/usr/bin/env bash
# MM Story Bot - one-shot setup (Git Bash, WSL, macOS, Linux).
# Same thing as setup.ps1. Run from inside the mm-story-bot folder:
#     bash setup.sh
set -euo pipefail

REPO_NAME="${REPO_NAME:-mm-story-bot}"
GRAPH="https://graph.facebook.com/v21.0"

say()  { echo "  $*"; }
step() { printf '\n\033[36m> %s\033[0m\n' "$*"; }
good() { printf '\033[32m  OK  %s\033[0m\n' "$*"; }
bad()  { printf '\n\033[31mX  %s\033[0m\n' "$*"; exit 1; }

step "Putting the workflow in place"
# Some file-sync paths cannot write into .github/workflows, so the workflow
# also ships at workflow/story.yml. Put it where GitHub Actions looks.
if [[ ! -f .github/workflows/story.yml && -f workflow/story.yml ]]; then
  mkdir -p .github/workflows
  cp workflow/story.yml .github/workflows/story.yml
  echo "  OK  .github/workflows/story.yml created"
fi
[[ -f .github/workflows/story.yml ]] || bad "workflow/story.yml is missing - re-download the folder."

step "Checking tools"
for t in git curl python3; do
  command -v "$t" >/dev/null 2>&1 || bad "$t is not installed."
done

# GitHub CLI does the repo/secrets/Pages work. Install it if it is missing.
add_gh_to_path() {
  local d
  for d in "/c/Program Files/GitHub CLI" \
           "/c/Program Files (x86)/GitHub CLI" \
           "$HOME/AppData/Local/Programs/GitHub CLI"; do
    if [[ -x "$d/gh.exe" ]]; then PATH="$d:$PATH"; return 0; fi
  done
  return 1
}

if ! command -v gh >/dev/null 2>&1; then
  add_gh_to_path || true
fi

if ! command -v gh >/dev/null 2>&1; then
  say "GitHub CLI (gh) is not installed. Installing it now - this takes a minute."
  if command -v winget >/dev/null 2>&1; then
    winget install --id GitHub.cli -e --source winget \
      --accept-package-agreements --accept-source-agreements || true
    add_gh_to_path || true
  else
    say "winget is not available on this machine."
  fi
fi

command -v gh >/dev/null 2>&1 || bad "GitHub CLI still not found.
     Install it yourself from https://cli.github.com
     (or open PowerShell and run:  winget install --id GitHub.cli )
     then double-click RUN-SETUP.bat again."
good "git, gh, curl, python3 found"

gh auth status >/dev/null 2>&1 || { say "Logging in to GitHub ..."; gh auth login; }
GH_USER="$(gh api user --jq .login)"
good "Logged in to GitHub as $GH_USER"

step "Checking your Instagram token"
# Windows consoles truncate a pasted line at ~254 characters, and Meta tokens
# are longer than that - so a file is the reliable way in.
IG_TOKEN="${IG_TOKEN:-}"

if [[ -z "$IG_TOKEN" && -f token.txt ]]; then
  IG_TOKEN="$(tr -d " \t\r\n" < token.txt)"
  good "Read the token from token.txt"
fi

if [[ -z "$IG_TOKEN" ]]; then
  say "Best way: put the token in a file, because Windows cuts off long pastes."
  say "  1. In this folder, create a file called  token.txt"
  say "  2. Paste the token into it and save"
  say "  3. Run RUN-SETUP.bat again"
  say ""
  read -rp "  Or paste it here and press Enter (may get cut off): " IG_TOKEN
fi

IG_TOKEN="$(printf "%s" "$IG_TOKEN" | tr -d " \t\r\n")"
[[ -n "$IG_TOKEN" ]] || bad "No token given."

say "Token received: ${#IG_TOKEN} characters."
if [[ ${#IG_TOKEN} -lt 100 ]]; then
  bad "That is too short for a Meta access token - it was cut off in transit.
     Put the full token in a file called token.txt in this folder, then run
     RUN-SETUP.bat again. That path has no length limit."
fi

IG_USER_ID="${IG_USER_ID:-}"
FB_LINE=""

say "Looking up your account ..."
ACCOUNTS="$(curl -sS -G "$GRAPH/me/accounts" \
    --data-urlencode "fields=id,name,access_token,instagram_business_account{id,username}" \
    --data-urlencode "access_token=$IG_TOKEN")"
SELF="$(curl -sS -G "$GRAPH/me" \
    --data-urlencode "fields=id,name,category,instagram_business_account{id,username}" \
    --data-urlencode "access_token=$IG_TOKEN")"

# Works with either a long-lived USER token or a never-expiring PAGE token.
RESOLVED="$(python3 -c '
import json, sys

accounts = json.loads(sys.argv[1])
me = json.loads(sys.argv[2])
want = sys.argv[3]

pages = [p for p in accounts.get("data", []) if isinstance(p, dict)]
if not pages and "category" in me and me.get("id"):
    # A Page access token: /me IS the Page.
    pages = [{"id": me["id"], "name": me.get("name", ""),
              "access_token": "self",
              "instagram_business_account": me.get("instagram_business_account")}]

linked = [p for p in pages if p.get("instagram_business_account")]
if not linked:
    err = accounts.get("error") or me.get("error") or {}
    sys.exit("TOKEN_ERROR: " + err.get("message", "no Instagram Business account on this token"))

if want:
    pick = next((p for p in linked if str(p["instagram_business_account"]["id"]) == want), linked[0])
elif len(linked) > 1:
    for i, p in enumerate(linked):
        print("  [%d] @%s  (page: %s)" % (i, p["instagram_business_account"]["username"], p.get("name","")),
              file=sys.stderr)
    pick = linked[int(input("Which account should post the stories? Enter the number: "))]
else:
    pick = linked[0]

ig = pick["instagram_business_account"]
print("\t".join([ig["id"], ig.get("username",""), str(pick.get("id","")), pick.get("name",""),
                 "yes" if pick.get("access_token") else "no"]))
' "$ACCOUNTS" "$SELF" "$IG_USER_ID" 2>&1)" || bad "Instagram rejected the token, or no Business account is linked to a Facebook Page.
     Generate a fresh long-lived token with instagram_basic, instagram_content_publish,
     pages_show_list, pages_manage_posts and pages_read_engagement.
     Details: $RESOLVED"

IG_USER_ID="$(echo "$RESOLVED" | cut -f1)"
IG_HANDLE="$(echo "$RESOLVED" | cut -f2)"
FB_PAGE_ID="$(echo "$RESOLVED" | cut -f3)"
FB_PAGE_NAME="$(echo "$RESOLVED" | cut -f4)"
FB_OK="$(echo "$RESOLVED" | cut -f5)"

good "Instagram: @$IG_HANDLE  (id $IG_USER_ID)"
if [[ "$FB_OK" == "yes" && -n "$FB_PAGE_ID" ]]; then
  good "Facebook Page: $FB_PAGE_NAME  (id $FB_PAGE_ID)"
else
  say "WARNING: no usable Facebook Page on this token - Facebook stories will be skipped."
  say "         The token needs pages_show_list, pages_manage_posts and pages_read_engagement."
fi

say "Checking the token can actually read your posts ..."
MEDIA="$(curl -sS -G "$GRAPH/$IG_USER_ID/media" \
    --data-urlencode "fields=id,media_type" --data-urlencode "limit=25" \
    --data-urlencode "access_token=$IG_TOKEN")"
VIDEOS="$(python3 -c "
import json,sys
d=json.loads(sys.argv[1])
if 'error' in d: sys.exit('ERR')
print(sum(1 for m in d.get('data',[]) if m.get('media_type')=='VIDEO'))
" "$MEDIA")" || bad "Could not read your media with that token."
if [[ "$VIDEOS" == "0" ]]; then
  say "WARNING: no videos in the 25 most recent posts. The bot only shares videos."
else
  good "$VIDEOS video(s) in the newest 25 posts - plenty to rotate"
fi

step "Creating the repository"
SLUG="$GH_USER/$REPO_NAME"
[[ -d .git ]] || { git init -q; git checkout -q -B main; }
git add -A
git -c user.name="$GH_USER" -c user.email="$GH_USER@users.noreply.github.com" \
    commit -q -m "MM Story Bot" --allow-empty

if gh repo view "$SLUG" >/dev/null 2>&1; then
  say "$SLUG already exists - pushing to it."
  git remote remove origin 2>/dev/null || true
  git remote add origin "https://github.com/$SLUG.git"
  git push -q -u origin main --force
else
  gh repo create "$REPO_NAME" --public --source . --remote origin --push
fi
good "Code is on https://github.com/$SLUG"

# Must be public or Pages will not serve the video to Instagram.
if [[ "$(gh api "repos/$SLUG" --jq .private)" == "true" ]]; then
  say "Making the repo public so Instagram can fetch the files ..."
  gh repo edit "$SLUG" --visibility public --accept-visibility-change-consequences
fi

step "Setting up GitHub Pages"
if ! gh api "repos/$SLUG/branches/gh-pages" >/dev/null 2>&1; then
  TMP="$(mktemp -d)"
  ( cd "$TMP"
    git init -q
    : > .nojekyll
    echo "mm-story-bot media host" > index.html
    echo '{}' > manifest.json
    git add -A
    git -c user.name="mm-story-bot" -c user.email="mm-story-bot@users.noreply.github.com" \
        commit -q -m "init media host"
    git push -q --force "https://github.com/$SLUG.git" HEAD:gh-pages )
  rm -rf "$TMP"
  good "gh-pages branch created"
else
  good "gh-pages branch already there"
fi

PAGES_BODY='{"source":{"branch":"gh-pages","path":"/"}}'
echo "$PAGES_BODY" | gh api "repos/$SLUG/pages" --method POST --input - >/dev/null 2>&1 \
  || echo "$PAGES_BODY" | gh api "repos/$SLUG/pages" --method PUT --input - >/dev/null 2>&1 || true
PAGES_URL="$(gh api "repos/$SLUG/pages" --jq .html_url 2>/dev/null || true)"
[[ -n "$PAGES_URL" ]] && good "Pages: $PAGES_URL" \
  || say "Could not confirm Pages - check Settings > Pages (branch gh-pages, root)."

step "Storing your secrets"
printf '%s' "$IG_USER_ID" | gh secret set IG_USER_ID      --repo "$SLUG"
printf '%s' "$IG_TOKEN"   | gh secret set IG_ACCESS_TOKEN --repo "$SLUG"
good "IG_USER_ID and IG_ACCESS_TOKEN stored (write-only - not readable again)"

read -rp "  Discord webhook for failure alerts (optional, Enter to skip): " HOOK || HOOK=""
if [[ -n "${HOOK:-}" ]]; then
  printf '%s' "$HOOK" | gh secret set DISCORD_WEBHOOK --repo "$SLUG"
  good "DISCORD_WEBHOOK stored"
fi

step "Running a dry run (nothing gets posted)"
say "Pages can take a minute to go live on a brand new repo - that is normal."
if gh workflow run story.yml --repo "$SLUG" -f dry_run=true; then
  sleep 10
  RUN_ID="$(gh run list --repo "$SLUG" --workflow story.yml --limit 1 \
              --json databaseId --jq '.[0].databaseId' 2>/dev/null || true)"
  if [[ -n "$RUN_ID" ]]; then
    if gh run watch "$RUN_ID" --repo "$SLUG" --exit-status; then
      good "Dry run passed - the pipeline works end to end."
    else
      say "Dry run failed. Open the log:  gh run view $RUN_ID --repo $SLUG --log-failed"
    fi
  else
    say "Run started. Watch it at https://github.com/$SLUG/actions"
  fi
else
  say "Could not start the run. Go to the Actions tab and run it by hand."
fi

printf '\n\033[33m===========================================================\033[0m\n'
printf '\033[33m Setup done.\033[0m\n'
printf '\033[33m===========================================================\033[0m\n'
say "Repo:    https://github.com/$SLUG"
say "Actions: https://github.com/$SLUG/actions"
say ""
say "One real story now (so you can see it on your profile):"
say "    gh workflow run story.yml --repo $SLUG -f dry_run=false"
say ""
say "After that it runs on its own: 12 stories a day,"
say "every 2 hours around the clock (IST)."

#!/usr/bin/env bash
# Push an updated mm-story-bot to GitHub and kick off a dry run.
# Use this after Claude sends you fixed files.
set -euo pipefail

say()  { echo "  $*"; }
step() { printf '\n\033[36m> %s\033[0m\n' "$*"; }
good() { printf '\033[32m  OK  %s\033[0m\n' "$*"; }
bad()  { printf '\n\033[31mX  %s\033[0m\n' "$*"; exit 1; }

[[ -d .git ]] || bad "This folder is not a git repo yet - run RUN-SETUP.bat first."
command -v gh >/dev/null 2>&1 || {
  for d in "/c/Program Files/GitHub CLI" "/c/Program Files (x86)/GitHub CLI" \
           "$HOME/AppData/Local/Programs/GitHub CLI"; do
    [[ -x "$d/gh.exe" ]] && { PATH="$d:$PATH"; break; }
  done
}
command -v gh >/dev/null 2>&1 || bad "GitHub CLI not found. Run RUN-SETUP.bat first."

SLUG="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
[[ -n "$SLUG" ]] || bad "Could not work out which GitHub repo this is."

step "Putting the workflow in place"
# .github/workflows cannot always be written by file-sync tools, so the
# current workflow always ships at workflow/story.yml.
if [[ -f workflow/story.yml ]]; then
  mkdir -p .github/workflows
  cp workflow/story.yml .github/workflows/story.yml
  good ".github/workflows/story.yml refreshed"
fi

step "Pushing to $SLUG"
git add -A
if git diff --cached --quiet; then
  say "Nothing changed - the repo is already up to date."
else
  git -c user.name="mm-story-bot" -c user.email="mm-story-bot@users.noreply.github.com" \
      commit -q -m "update $(date -u '+%Y-%m-%d %H:%M UTC')"
  git push -q origin HEAD:main
  good "Pushed"
fi

step "Running a dry run (nothing gets posted)"
gh workflow run story.yml --repo "$SLUG" -f dry_run=true
sleep 10
RUN_ID="$(gh run list --repo "$SLUG" --workflow story.yml --limit 1 \
            --json databaseId --jq '.[0].databaseId' 2>/dev/null || true)"
if [[ -n "$RUN_ID" ]]; then
  gh run watch "$RUN_ID" --repo "$SLUG" --exit-status \
    && good "Dry run passed - the pipeline works end to end." \
    || { say "Dry run failed. The log:"; gh run view "$RUN_ID" --repo "$SLUG" --log-failed | tail -40; }
else
  say "Watch it at https://github.com/$SLUG/actions"
fi

printf '\n'
say "Repo:    https://github.com/$SLUG"
say "Actions: https://github.com/$SLUG/actions"
say ""
say "One real story now:"
say "    gh workflow run story.yml --repo $SLUG -f dry_run=false"

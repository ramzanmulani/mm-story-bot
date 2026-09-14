# MM Story Bot

Re-shares your own Instagram videos to your **Instagram Story and your
Facebook Page story** — 7 a day, spread roughly two hours apart,
automatically, from GitHub Actions. Your PC does not need to be on.

---

## What it actually does

Every slot, one run:

1. Pulls your post library from the Instagram Graph API (newest 300).
2. Picks the next clip from a **shuffle bag** — every video gets posted once
   before any video repeats, and the last 10 posted are held back when the
   bag refills, so you never see the same clip twice in a row.
3. Downloads it and re-encodes it **once** into a file both platforms accept:
   1080×1920, 3–59 s, H.264 + AAC 48 kHz, `faststart`. Anything that is not
   9:16 is centred over a blurred copy of itself (the way Instagram does it).
   A clip under 3 s is looped up to 3 s, because Facebook rejects anything
   shorter.
4. **Instagram:** pushes the file to the `gh-pages` branch, waits for GitHub
   Pages to serve it (Instagram will not accept an upload — it fetches a
   public URL), creates the story container, polls until `FINISHED`,
   publishes.
5. **Facebook story:** uploads the same file straight to your Page's story
   in Facebook's three-phase upload. No public URL needed there.
6. **Facebook Page feed:** once a day, the same clip also goes up as a Page
   **Reel** — permanent, unlike the story. Rationed separately
   (`facebook.feed_posts_per_day`) because feed posts do not disappear.
6. Commits the updated rotation state back to `main`.

The two platforms are independent. If Facebook fails, the Instagram story
still goes out and you get told which one broke; the rotation only advances
when at least one of them actually published, so a dead slot retries the
same clip instead of burning it.

Your Facebook Page is found automatically — it is the Page your Instagram
account is already linked to. No second secret, no second token: the Page
token is minted at runtime from the same long-lived token.

New reels you post are picked up automatically. Deleted ones drop out of the
rotation on their own.

### One thing the APIs cannot do

Both official APIs publish a story as a **video**. Neither can attach the
tappable "post" sticker, a link sticker, or an @mention sticker — those
exist only in the phone apps. So the story shows your clip full-screen and
looks native, but tapping it does not open the original post. There is no
API-legal way around this; the only tools that fake it drive a logged-in
session and get accounts restricted.

---

## Setup — one double-click

**Easiest:** open the `mm-story-bot` folder and double-click **`RUN-SETUP.bat`**.
It finds Git Bash (or falls back to PowerShell), runs the setup, and keeps the
window open so you can read what happened.

Or from a terminal inside the folder:

```bash
bash setup.sh
```

or on Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

It asks for exactly one thing — your long-lived Instagram access token — and
does the rest:

- checks `git` and `gh` are installed and logs you in to GitHub if needed
- validates the token and **finds your Instagram Business account id for you**
- confirms the token can actually read your posts, and counts your videos
- creates the public repo and pushes the code
- creates the `gh-pages` branch and turns on GitHub Pages
- stores `IG_USER_ID` and `IG_ACCESS_TOKEN` as repository secrets
- offers to store a Discord webhook for failure alerts
- fires a **dry run** and waits for it, so you know the pipeline works

Everything is validated before anything is created, so a bad token fails in
five seconds instead of leaving you with a half-built repo. Re-running it is
safe — it pushes to the existing repo instead of making a second one.

### Then post one for real

```bash
gh workflow run story.yml --repo <you>/mm-story-bot -f dry_run=false
```

Check your profile, and if it looks right, leave it alone — the schedule
takes over from there.

### The token — get one that never expires

The bot accepts **either** kind of Meta token:

| Token | Lifetime | When to use |
| --- | --- | --- |
| Long-lived **user** token | 60 days, then it dies | Quick start |
| **Page** access token | **Never expires** | What you want — set it once, forget it |

Facebook does not offer a never-expiring *user* token; 60 days is the hard
maximum. But a **Page** access token minted from a long-lived user token has
no expiry at all, and Instagram's publishing API accepts it too. So one
permanent token covers both platforms.

**How to get the permanent one:**

1. [Graph API Explorer](https://developers.facebook.com/tools/explorer/) →
   pick your app → add these permissions:
   `instagram_basic`, `instagram_content_publish`, `pages_show_list`,
   `pages_manage_posts`, `pages_read_engagement` → **Generate Access Token**.
2. [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/)
   → paste it → **Debug** → **Extend Access Token** at the bottom.
   You now have a 60-day *user* token.
3. Back in the Graph API Explorer, paste that extended token into the Access
   Token box and run:

   ```
   GET /me/accounts?fields=id,name,access_token
   ```

   The `access_token` in the response, for your Page, is the permanent one.
4. Give **that** to `setup.sh`.

Check it any time by pasting it into the Debugger — a Page token shows
**Expires: Never**.

The Instagram account must be Business or Creator and linked to that Page,
and you need CREATE_CONTENT rights on the Page.

If the token only has the Instagram permissions, the bot still works — it
posts to Instagram and logs that Facebook was skipped.

### If you would rather do it by hand

Create a **public** repo, push this folder, set Pages to branch `gh-pages`
/ root, and add `IG_USER_ID` and `IG_ACCESS_TOKEN` under Settings → Secrets
and variables → Actions. `GITHUB_TOKEN` is automatic; do not add it. The
Pages URL is worked out from the repo name, so there is nothing to fill in.

---

## Tuning

Everything lives in `config.yaml`, no code changes needed.

| Setting | Meaning |
| --- | --- |
| `targets.instagram` / `targets.facebook` / `targets.facebook_feed` | turn any of the three off |
| `facebook.feed_posts_per_day` | `1` — set `7` to post a Page Reel every slot |
| `facebook.feed_caption` / `feed_caption_footer` | Page Reel caption; `{caption}` and `{permalink}` are filled in |
| `facebook.page_id` | blank = the Page linked to your Instagram. Only set it if you manage several Pages |
| `video.min_duration_sec` | `3` — Facebook's floor; shorter clips are looped |
| `source.media_types` | `["VIDEO"]` — add `"IMAGE"` if you ever want photos too |
| `source.product_types` | `["REELS", "FEED"]` — set to `["REELS"]` for reels only |
| `source.exclude_media_ids` | Clips you never want auto-shared |
| `source.max_age_days` | `0` = your whole back catalogue is eligible |
| `rotation.no_repeat_window` | How many recent clips are held back on refill |
| `video.fit` | `blur` (default), `pad`, or `crop` |
| `video.trim_from` | `start` or `middle`, for clips over 59 s |
| `publish.enabled` | `false` turns every run into a dry run |

### Changing the times

Edit the `cron:` lines in `.github/workflows/story.yml`. They are **UTC** —
subtract 5 h 30 m from IST. Actions cron can fire a few minutes late when
GitHub is busy; that is normal and harmless here.

### Changing how many per day

Add or remove `cron:` lines. Each line = one story.

---

## When something goes wrong

The run fails loudly with a readable reason and the rotation state is **not**
advanced, so nothing is skipped — the next slot retries the same clip.

| Message | Fix |
| --- | --- |
| `token has expired or been invalidated` | Generate a new long-lived token, update `IG_ACCESS_TOKEN`. Long-lived tokens last 60 days and must be refreshed |
| `missing the instagram_content_publish permission` | Re-issue the token with that scope; the account must be Business or Creator and linked to a Facebook Page |
| `GitHub Pages did not serve ...` | Pages source is not set to branch `gh-pages` / root, or the repo is private |
| `No eligible videos found` | The token belongs to a different account, or `source.product_types` excludes everything you have |
| `quota is nearly exhausted` | Instagram allows 100 API posts per rolling 24 h. Harmless — the slot is skipped |
| `Instagram could not process the video` | That specific source file is bad. Add its id to `source.exclude_media_ids` |
| `No Facebook Page is available on this token` | Token is missing `pages_show_list`. Instagram still posts |
| `missing pages_manage_posts` | Re-issue the token with it, or set `targets.facebook: false` |
| `More than one Facebook Page ... none is linked` | Set `facebook.page_id` in `config.yaml` |

`state/history.json` is the rotation log — `posted` lists what went out and
when, with the Instagram story id and Facebook post id for each. Delete the
file to reset the rotation from scratch.

## Repo size

The `gh-pages` branch is force-pushed as a **single orphan commit** every run
and files older than 24 h are dropped, so the repository stays a few MB
forever no matter how long this runs.

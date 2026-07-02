# MLB Dashboard — deploy to a public URL (free)

This folder is a ready-to-deploy copy of the dashboard. Follow these steps once
and you'll have a permanent `https://...onrender.com` URL you can send to anyone.
It runs 24/7 in the cloud — your PC does **not** need to be on.

**What you'll need:** a free GitHub account and a free Render account. ~10 minutes.

---

## Step 1 — Put this code on GitHub

**Easiest (with the GitHub CLI, `gh`, which is now installed):**
```powershell
cd "$env:USERPROFILE\OneDrive\Desktop\mlb-dashboard"
gh auth login          # choose GitHub.com > HTTPS > login with a browser
gh repo create mlb-dashboard --public --source . --push
```
That creates the repo and pushes the code in one go.

**Or manually:** create a new **empty** repo at https://github.com/new named
`mlb-dashboard`, then:
```powershell
cd "$env:USERPROFILE\OneDrive\Desktop\mlb-dashboard"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/mlb-dashboard.git
git push -u origin main
```

## Step 2 — Deploy on Render

1. Go to https://render.com and sign up (free — no credit card for free web services).
2. Click **New +** → **Blueprint**.
3. Connect your GitHub and pick the **mlb-dashboard** repo. Render reads
   `render.yaml` automatically.
4. When prompted, set the environment variable **`BALLDONTLIE_API_KEY`** to your
   key (the one in your local `bdl_config.json`). *(This keeps your key out of
   the public code.)*
5. Click **Apply / Create**. First build takes ~2–3 minutes.

Render gives you a public URL like **`https://mlb-dashboard-xxxx.onrender.com`** —
that's the link to share. 🎉

---

## Good to know

- **Free tier sleeps** after ~15 min of no visitors. The next visit "wakes" it,
  which can take ~30–60s (plus a few seconds to load the first slate). After that
  it's fast. To keep it always-warm, upgrade to Render's paid tier (~$7/mo) or
  ping the URL every 10 min with a free uptime monitor (e.g. UptimeRobot).
- **Shared traffic uses your BALLDONTLIE rate limit** (ALL-STAR = 60 req/min).
  The app caches aggressively, so many visitors within a few minutes share cached
  data — but a big simultaneous crowd could briefly rate-limit the injuries feed.
- **Updating the app:** this folder is a snapshot. When the model changes, re-copy
  `mlb_app.py` here and `git commit -am "update" && git push` — Render auto-deploys.
- **Your API key is never in the code** — it lives only in Render's env var and
  your local `bdl_config.json` (which `.gitignore` keeps out of git).

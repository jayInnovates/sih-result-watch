# SIH 2026 result watch

Runs every ~10 minutes on GitHub Actions (free for public repos) and sends a push notification via [ntfy](https://ntfy.sh) when SIH 2026 results appear.

**What it checks**
1. **News**: Google News + Bing News for "Smart India Hackathon 2026". New articles about results / shortlist / grand finale trigger an **urgent** alert (college "internal hackathon" and pre-qualifier news is ignored).
2. **sih.gov.in**: rendered in a headless browser and diffed. The site's firewall currently returns *403 Forbidden* to GitHub's servers, so this part only works if that changes; blocked responses are never treated as a change.

**Use**
- Notifications: the ntfy app subscribed to the topic stored in the `NTFY_TOPIC` repo secret
- Test: Actions → *SIH 2026 result watch* → **Run workflow**
- Stop: Actions → *SIH 2026 result watch* → ⋯ → **Disable workflow**

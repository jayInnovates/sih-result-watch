# SIH 2026 result watch

Checks https://sih.gov.in every ~10 minutes with GitHub Actions (free for public repos) and sends a push notification via [ntfy](https://ntfy.sh) when the page changes. Changes mentioning result / shortlist / finale are sent as **urgent**.

- Notifications: subscribe to the topic stored in the `NTFY_TOPIC` repo secret in the ntfy app (Mac / iPhone / Android) or at https://ntfy.sh/app
- Test: Actions → *SIH 2026 result watch* → **Run workflow**
- Only want result-related alerts: add repo variable `ONLY_KEYWORDS` = `true`
- Stop it: Actions → *SIH 2026 result watch* → ⋯ → **Disable workflow**

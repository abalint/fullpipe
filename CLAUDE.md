# fullPipe — instructions for Claude

## Phone / app data is user data — never destroy it

- **Never uninstall the mobile app, run `pm clear`, or otherwise wipe its app
  data without explicit, informed confirmation.** The app's local storage
  holds downloaded episode videos and pinned passive-listening files; some of
  those no longer exist on the PC (staged videos are purged server-side after
  watch + reconcile), so wiping the app destroys the only copy.
- "Reinstall the app" means `adb install -r` (replace APK, keep data). If a
  problem genuinely seems to require clearing app data, say exactly what will
  be lost (videos, pins, settings, outbox) and get a yes first — a stale
  cache is never worth user data.
- The same bar applies to anything under `~/immersion/` (episodes, videos,
  ledger): treat it as irreplaceable user data, not build output.

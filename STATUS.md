# STATUS — CapCal

*Last updated: 2026-04-23*

## What this is
Mobile-first PWA video editor for iPhone. Cal's alternative to CapCut — cleaner, focused on short-form content. Videos processed server-side via FFmpeg, captions via Deepgram Nova-3.

## What's live
- Live at https://web-production-7540a.up.railway.app
- PWA installable on iPhone via Safari → Share → Add to Home Screen
- Local dev runs on port 5001 (`Start CapCal.bat`)
- v1.x (MVP) features: upload, trim, split clips, auto captions with inline edit, silence removal, noise reduction, MP4 export

## What's broken / blocked
- Single-user only — in-memory job store (`_jobs` dict). Multi-user would need Redis.
- Music feature removed from Phase 1 (was in v1.0, pulled back)
- Phase 2+ features not started: background removal, auto zoom, animated caption styles, music library

## What's next
1. Phase 2 build — background removal (TensorFlow.js) + auto zoom on energy moments
2. Animated word-by-word caption highlighting
3. Royalty-free music library

## Key files
- `app.py` — Flask backend, all processing logic
- `static/index.html` — Full PWA, 5 screens (Home, Upload, Editor, Processing, Done)
- `static/sw.js` — Service worker (offline cache)
- `static/manifest.json` — PWA manifest
- `.env` — `DEEPGRAM_API_KEY`
- `CLAUDE.md` — full doc: pipeline, routes, decisions, phase roadmap

## Deploy notes
- GitHub: `AutoBuildCharlie/capcal` (mirrored to `Cal-Zentara/CapCal`)
- Hosting: Railway (auto-deploy on push)
- Railway account: aestheticcal22@gmail.com
- **Push as AutoBuildCharlie:** `gh auth switch -u AutoBuildCharlie` before push
- Port 5001 (avoids collision with Zentara Studio on 5000)

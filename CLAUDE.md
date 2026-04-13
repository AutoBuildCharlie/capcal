# CapCal — AI Video Editor

## Table of Contents
1. [What It Is](#what-it-is)
2. [How to Run](#how-to-run)
3. [Project Structure](#project-structure)
4. [Processing Pipeline](#processing-pipeline)
5. [Frontend Screens](#frontend-screens)
6. [API Routes](#api-routes)
7. [Phase Roadmap](#phase-roadmap)

---

## What It Is
A mobile-first PWA video editor for iPhone. Built for Cal to edit short-form content (TikTok/YouTube Shorts). Alternative to CapCut — cleaner, focused feature set. Premium white + dark purple design. Videos are processed server-side via FFmpeg. Captions transcribed via Deepgram Nova-3.

Colors: White background (#ffffff), Dark purple (#3b0764), Purple (#5b21b6), Light purple (#ede9fe), Black text (#111111).

---

## How to Run
1. Double-click `Start CapCal.bat`
2. Open `http://localhost:5001` in Safari on iPhone
3. Tap Share → Add to Home Screen → done

Or manually:
```
pip install -r requirements.txt
python app.py
```

Runs on port **5001**.

---

## Project Structure
```
CapCal/
  app.py                  — Flask backend, all processing logic
  requirements.txt        — flask, python-dotenv, httpx
  .env                    — DEEPGRAM_API_KEY
  Start CapCal.bat        — One-click launcher
  static/
    index.html            — Full PWA (single-page, 5 screens)
    manifest.json         — PWA manifest
    sw.js                 — Service worker (offline cache)
  uploads/{job_id}/       — Temp: original video, audio, segments
  outputs/{job_id}/       — Temp: final exported video
```

---

## Processing Pipeline
Each export runs these steps in order (some are optional):

| Step | Function | Enabled By |
|---|---|---|
| 1. Trim | FFmpeg `-ss -to` | Always |
| 2. Remove silences | `remove_silences()` + FFmpeg concat | Toggle |
| 3. Noise reduction | FFmpeg `afftdn` filter | Toggle |
| 4. Add music | FFmpeg `amix` filter | Toggle + music uploaded |
| 5. Burn captions | `generate_ass()` + FFmpeg `ass` filter | Toggle |
| 6. Final encode | FFmpeg `libx264 + aac` | Always |

All job state is in-memory (`_jobs` dict). Files are deleted after download via `/api/cleanup/<job_id>`.

---

## Frontend Screens
All 5 screens live in `static/index.html`. Screen switching via CSS opacity + transform.

| Screen | ID | Description |
|---|---|---|
| Home | `screen-home` | Logo, stats, Start button. Floating particles canvas. |
| Upload | `screen-upload` | Animated gradient border drop zone. File select → upload. |
| Editor | `screen-editor` | Video preview, play controls, 4 tabs: Trim/Captions/Audio/Export. |
| Processing | `screen-processing` | Animated waveform, step-by-step checklist, progress bar. |
| Done | `screen-done` | Confetti canvas, Download button, Edit Another. |

Stats (videos this week / all time) saved to `localStorage`.

---

## API Routes

| Method | Route | What It Does |
|---|---|---|
| GET | `/` | Serve index.html |
| POST | `/api/upload` | Upload video → start transcription → return job_id |
| GET | `/api/job/<id>` | Get job status, progress, words |
| POST | `/api/upload-music/<id>` | Upload music file for the job |
| POST | `/api/process/<id>` | Start processing pipeline with options JSON |
| GET | `/api/download/<id>` | Download finished video |
| DELETE | `/api/cleanup/<id>` | Delete all files for this job |

---

## Phase Roadmap

### Phase 1 — Current (MVP)
- Upload video
- Timeline with filmstrip + waveform + drag handles to trim
- Split clips at playhead — multi-clip editing
- Auto captions (Deepgram Nova-3) — inline word editing (tap to edit/delete)
- Remove silences
- Noise reduction
- Export MP4
- White + dark purple premium theme
- Bottom toolbar navigation (Captions / Audio / Export)
- No music (removed)

### Phase 2
- Background removal (TensorFlow.js)
- Auto zoom on energy moments
- More caption styles (animated word-by-word highlighting)
- Music library (royalty-free presets)

### Phase 3
- Eye contact correction (server-side)
- AI dubbing
- Speed ramping
- Multiple clips / timeline

---

## Deployment
- **Live URL:** `web-production-7540a.up.railway.app`
- **Hosting:** Railway (auto-deploys on push to AutoBuildCharlie/capcal)
- **GitHub repo:** `AutoBuildCharlie/capcal` (also mirrored at `Cal-Zentara/CapCal`)
- **Railway account:** aestheticcal22@gmail.com
- **gh CLI active account:** AutoBuildCharlie (needed for pushes)

---

## Known Decisions
- Server-side FFmpeg instead of FFmpeg.wasm — more reliable on iPhone Safari
- Deepgram Nova-3 for transcription — best accuracy + word timestamps by default
- Single HTML file — keeps deployment simple, no build step
- In-memory job store — fine for single-user. Would need Redis for multi-user scale.
- Port 5001 — avoids conflict with Zentara Studio (5000)
- All three aspect ratios (9:16, 1:1, 16:9) now always apply a scale+pad filter — this guarantees the output resolution matches the ASS PlayResX/PlayResY coordinate space used for caption positioning. Without this, a 9:16 export of a non-1080x1920 source would misplace captions.
- The `ass` filter path escape (`\:` for Windows drive colon) works correctly even inside a multi-filter `-vf` chain. FFmpeg's filter parser handles `\:` inside quoted strings as an escape for `:`, yielding the correct path.
- The concat list file path (`str(concat)`) uses backslashes on Windows, which is fine — FFmpeg accepts backslash paths when passed directly via subprocess args. The content inside the file uses `.as_posix()` (forward slashes), which is what the concat demuxer requires.

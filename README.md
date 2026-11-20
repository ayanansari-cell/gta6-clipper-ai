# GTA 6 CLIPPER AI

A Streamlit web app that turns a live stream (or recorded video) into **captioned, fully-credited vertical Shorts**,
with a hard **daily clip cap** (default 10). Dark gaming UI: black background, red accents, works on phone and desktop.

It is built on your original clipper: the same audio-highlight detection, FFmpeg rendering (1080x1920, blurred
background, burned-in captions and streamer credit), daily cap and credit files. `clipper.py` still works from the
command line exactly as before.

## Quick start

1. Install **FFmpeg** (must be on your PATH): `sudo apt install ffmpeg` / `brew install ffmpeg` / `winget install ffmpeg`
2. Install the Python packages (Python 3.9+):
   ```bash
   pip install -r requirements.txt
   ```
3. Optional - automatic speech captions: `pip install faster-whisper`
4. Run:
   ```bash
   streamlit run app.py
   ```
   Open the address it prints (usually http://localhost:8501). The sidebar shows which tools were found.

## Using the app

| Section | What it does |
|---|---|
| **Dashboard** | Total clips ever generated, today's counter (e.g. 6 / 10), clips left today, and live processing status (IDLE / PROCESSING / COMPLETE / ERROR). |
| **01 Video input** | Upload one or more videos (mp4, mov, mkv, webm) **or** enter a live stream URL (it records for the minutes you choose). Enter the streamer's name and link - they are burned into every clip. |
| **02 Clipping settings** | Clip duration (15 / 30 / 45 / 60 s), max clips per day, audio highlight detection on/off, captions on/off. *Advanced:* sensitivity threshold, title prefix, `.srt` transcript, Whisper model, language, timezone for the daily reset. |
| **03 Processing** | START CLIPPING, a progress bar and a live log. |
| **04 Results** | Video previews, per-clip download, and one-click ZIP of everything (clips + descriptions + `credits.csv`). |

**Audio highlight detection** scores loudness and sudden jumps (screaming, explosions, crashes) and takes the best
moments. Turn it off to cut evenly spaced clips instead (also the way to clip videos with no sound).

**Captions**: with `faster-whisper` installed they are generated automatically from the speech; otherwise upload an
`.srt` in *Advanced*. With neither, clips still get the title and credit overlays, and the app tells you so.

**Credit on every clip**: "CLIP FROM <STREAMER> LIVE" badge for the whole clip, "FULL STREAM: <link>" and
"ALL CREDIT TO <STREAMER>" at the bottom, streamer/source in the file metadata, plus a ready-to-paste
title / description / pinned comment (`short_NN.txt`) and a `credits.csv` per day.

Output lives in `shorts_out/<date>/`; the counters live in `shorts_out/state.json`.

## Project structure

```
gta6_clipper_ai/
  app.py              Streamlit UI (widgets and display only)
  jobs.py             UI-free job layer: validation, run_job(), stats, ZIP building
  clipper.py          the clipping engine (your original code, refactored) + the original CLI
  theme.py            dark / red CSS and HTML helpers
  .streamlit/config.toml   theme colours and upload limit (2 GB)
  requirements.txt    Python dependencies
  packages.txt        system packages for Streamlit Community Cloud (ffmpeg, fonts)
  tests/test_pipeline.py   end-to-end tests (no browser needed)
```

## Command line (unchanged)

```bash
python clipper.py stream.mp4 --streamer "StreamerName" --source-url https://twitch.tv/streamername
python clipper.py https://twitch.tv/streamername --streamer "StreamerName" --watch
python clipper.py stream.mp4 --streamer "Name" --no-detect      # new: evenly spaced clips
```

## Tests

```bash
python -m unittest discover -s tests -v
```
Builds a synthetic stream with loud moments at known times and checks clip count, 1080x1920 output, captions, credits,
the daily cap, multi-file quota sharing, error handling, the ZIP, and that the CLI still behaves.

## Notes and limits

- **Don't click around while it's processing.** Streamlit re-runs the page on every interaction, which interrupts a
  running job. Wait for COMPLETE.
- Live URLs are **recorded for N minutes, then clipped** (not clipped continuously). For all-day unattended clipping
  use the CLI's `--watch` mode.
- The daily cap is per calendar day in the timezone you set (default Asia/Karachi). Raising "Maximum clips per day"
  allows more; numbering continues.
- Big uploads: the limit is 2 GB (`.streamlit/config.toml`). Rendering is CPU-heavy; a 45 s clip takes roughly
  10-60 s depending on your machine.
- Deploying to Streamlit Community Cloud works (`packages.txt` installs ffmpeg), but uploaded/rendered files are
  temporary there, and recording from live URLs may be blocked by the host.
- Only clip streams you have permission to clip, and keep the credit on. Credit alone does not stop copyright
  claims on reposted footage. Not affiliated with Rockstar Games or Take-Two Interactive.

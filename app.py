"""
GTA 6 CLIPPER AI - Streamlit web app.

Run:  streamlit run app.py

This file is only the user interface. The clipping itself is done by clipper.py
(via the UI-free job layer in jobs.py), so the original engine is untouched.
"""
from __future__ import annotations

import re
import time
import traceback
from pathlib import Path

import streamlit as st

import clipper
import jobs
import theme

st.set_page_config(page_title="GTA 6 CLIPPER AI", page_icon="🎬", layout="wide",
                   initial_sidebar_state="collapsed")

APP_DIR = Path(__file__).resolve().parent
OUT_DIR = str(APP_DIR / "shorts_out")
UPLOAD_DIR = APP_DIR / "uploads"
VIDEO_TYPES = ["mp4", "mov", "mkv", "webm"]

ss = st.session_state
for key, default in (("status", "IDLE"), ("logs", []), ("last_files", []), ("error", None), ("notice", None)):
    if key not in ss:
        ss[key] = default


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def save_uploads(files, folder: Path) -> list:
    """Write Streamlit UploadedFile objects to disk so FFmpeg can read them."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    saved = []
    for i, f in enumerate(files):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(f.name).name) or f"video_{i}.mp4"
        dest = folder / f"{stamp}_{i}_{safe}"
        with open(dest, "wb") as fh:
            fh.write(f.getbuffer())
        saved.append(dest)
    return saved


@st.cache_data(show_spinner=False)
def zip_cached(signature: tuple) -> bytes:
    """signature = ((path, mtime), ...) so the cache refreshes when files change."""
    return jobs.build_zip([p for p, _ in signature])


def fmt_time(sec) -> str:
    try:
        s = int(float(sec))
    except (TypeError, ValueError):
        return "?"
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------- #
# page frame
# --------------------------------------------------------------------------- #
st.markdown(theme.CSS, unsafe_allow_html=True)
st.markdown(theme.header_html(), unsafe_allow_html=True)
dash_box = st.empty()           # filled at the very end (and during processing)

with st.sidebar:
    st.markdown("### System check")
    sysinfo = jobs.system_status()
    rows = [("FFmpeg", sysinfo["ffmpeg"], "required"),
            ("streamlink", sysinfo["streamlink"], "live URLs"),
            ("yt-dlp", sysinfo["yt-dlp"], "live URLs (alternative)"),
            ("faster-whisper", sysinfo["faster-whisper"], "auto speech captions")]
    for name, ok, why in rows:
        st.markdown(f'<span class="dot {"ok" if ok else "no"}"></span><b>{name}</b> '
                    f'<span style="color:#9a9aa2">- {why}</span>', unsafe_allow_html=True)
    st.caption("Red dot = not installed. See the README for install commands.")

# --------------------------------------------------------------------------- #
# 01 - video input
# --------------------------------------------------------------------------- #
st.markdown(theme.section("01", "Video input"), unsafe_allow_html=True)
with st.container(border=True):
    mode = st.radio("Source", ["Upload video", "Stream URL"], horizontal=True, key="mode")
    uploads, stream_url, record_min = [], "", 10
    if mode == "Upload video":
        uploads = st.file_uploader("Upload MP4 videos (you can add several)", type=VIDEO_TYPES,
                                   accept_multiple_files=True, key="uploads") or []
    else:
        stream_url = st.text_input("Stream URL", placeholder="https://www.twitch.tv/streamername", key="stream_url")
        record_min = st.number_input("Minutes to record", min_value=1, max_value=240, value=10, step=1, key="record_min",
                                     help="The app records the live stream for this long, then clips the best moments.")
        st.caption("Live recording needs streamlink or yt-dlp. The page stays busy while it records - "
                   "don't click anything else until it finishes.")
    c1, c2 = st.columns(2)
    streamer = c1.text_input("Streamer name (credited on every clip)", placeholder="e.g. StreamerName", key="streamer")
    source_link = c2.text_input("Channel / stream link (credited)", placeholder="https://twitch.tv/streamername",
                                key="source_link", help="Shown on every clip and in the descriptions. "
                                                        "For a stream URL it defaults to that URL.")

# --------------------------------------------------------------------------- #
# 02 - clipping settings
# --------------------------------------------------------------------------- #
st.markdown(theme.section("02", "Clipping settings"), unsafe_allow_html=True)
with st.container(border=True):
    s1, s2 = st.columns(2)
    clip_len = s1.radio("Clip duration", [15, 30, 45, 60], index=2, horizontal=True,
                        format_func=lambda v: f"{v} sec", key="clip_len")
    max_day = s2.number_input("Maximum clips per day", min_value=1, max_value=50, value=10, step=1, key="max_day")
    t1, t2 = st.columns(2)
    detect = t1.toggle("Audio highlight detection", value=True, key="detect",
                       help="On: picks the loudest / most hyped moments. Off: cuts evenly spaced clips.")
    captions = t2.toggle("Captions", value=True, key="captions",
                         help="Burns big word-by-word captions into each clip.")
    with st.expander("Advanced"):
        min_score = st.slider("Highlight sensitivity threshold (lower = more clips)", 1.0, 10.0, 3.0, 0.5, key="min_score",
                              disabled=not detect)
        title_prefix = st.text_input("Title prefix", value="GTA 6 LIVE MOMENT", key="title_prefix")
        srt_file = st.file_uploader("Transcript (.srt) for captions - optional", type=["srt"], key="srt",
                                    help="Timestamps must match the uploaded video. Overrides automatic captions.")
        a1, a2 = st.columns(2)
        whisper_model = a1.selectbox("Whisper model", ["tiny", "base", "small"], index=1, key="whisper_model")
        lang = a2.text_input("Caption language code (blank = auto)", placeholder="en", key="lang")
        tz = st.text_input("Timezone for the daily reset", value="Asia/Karachi", key="tz")

    if not captions:
        st.caption("Captions are off - clips will still carry the title and streamer credit.")
    elif srt_file is not None:
        st.caption("Captions: from your .srt transcript.")
    elif sysinfo["faster-whisper"]:
        st.caption("Captions: automatic speech recognition (Whisper).")
    else:
        st.warning("No speech-caption engine found, so clips get the title + credit overlays only. "
                   "Install faster-whisper (`pip install faster-whisper`) or upload an .srt in Advanced.")

# --------------------------------------------------------------------------- #
# 03 - processing
# --------------------------------------------------------------------------- #
st.markdown(theme.section("03", "Processing"), unsafe_allow_html=True)
start = st.button("START CLIPPING", type="primary", key="start")
progress_slot = st.empty()
st.caption("Processing logs")
log_slot = st.empty()

if start:
    cfg = jobs.JobConfig(
        streamer=streamer, source_url=(source_link or stream_url or "").strip(), clip_len=int(clip_len),
        max_per_day=int(max_day), detect_audio=bool(detect), captions=bool(captions), min_score=float(min_score),
        title_prefix=title_prefix, whisper_model=whisper_model, lang=(lang.strip() or None), tz=(tz.strip() or "Asia/Karachi"),
        out_dir=OUT_DIR, record_minutes=int(record_min))
    problems = jobs.validate(cfg, len(uploads), stream_url)
    ss["notice"] = None
    ss["error"] = None
    if problems:
        ss["error"] = "  \n".join(problems)
    else:
        ss["status"] = "PROCESSING"
        ss["logs"] = []
        stats = jobs.get_stats(OUT_DIR, cfg.tz)
        dash_box.markdown(theme.dashboard_html(stats["total"], stats["today"], cfg.max_per_day, "PROCESSING", stats["date"]),
                          unsafe_allow_html=True)
        bar = progress_slot.progress(0.0, text="Starting...")

        def log_fn(msg: str) -> None:
            ss["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            log_slot.code("\n".join(ss["logs"][-300:]), language=None)

        def progress_fn(frac: float, msg: str) -> None:
            bar.progress(float(frac), text=msg)

        saved = []
        try:
            saved = save_uploads(uploads, UPLOAD_DIR) if mode == "Upload video" else []
            if srt_file is not None:
                srt_saved = save_uploads([srt_file], UPLOAD_DIR)[0]
                cfg.srt_path = str(srt_saved)
                saved.append(srt_saved)
            result = jobs.run_job(cfg, files=[p for p in saved if p.suffix.lower() != ".srt"],
                                  stream_url=stream_url if mode == "Stream URL" else "",
                                  log=log_fn, progress=progress_fn)
            ss["last_files"] = [str(p) for p in result.created]
            ss["results_scope"] = "Last run"
            if result.created:
                ss["status"] = "COMPLETE"
                ss["notice"] = f"Generated {len(result.created)} clip(s). Today: {result.count}/{cfg.max_per_day}."
            else:
                ss["status"] = "IDLE"
                ss["error"] = ("No clips were generated. Try lowering the sensitivity threshold, "
                               "turning off audio highlight detection, or using a longer video.")
        except clipper.ClipperError as exc:
            ss["status"] = "ERROR"
            ss["error"] = str(exc)
            log_fn(f"ERROR: {exc}")
        except Exception as exc:  # noqa: BLE001 - show anything unexpected instead of a blank crash
            ss["status"] = "ERROR"
            ss["error"] = f"Unexpected error: {exc}"
            for line in traceback.format_exc().strip().splitlines()[-6:]:
                log_fn(line)
        finally:
            for p in saved:
                p.unlink(missing_ok=True)
            progress_slot.empty()
elif ss["logs"]:
    log_slot.code("\n".join(ss["logs"][-300:]), language=None)
else:
    log_slot.code("Waiting for a job...", language=None)

if ss["error"]:
    st.error(ss["error"])
elif ss["notice"]:
    st.success(ss["notice"])

# --------------------------------------------------------------------------- #
# 04 - results
# --------------------------------------------------------------------------- #
st.markdown(theme.section("04", "Results"), unsafe_allow_html=True)
last_paths = [Path(p) for p in ss["last_files"] if Path(p).exists()]
if "results_scope" not in ss:
    ss["results_scope"] = "Last run" if last_paths else "Today (all)"
scope = st.radio("Show", ["Last run", "Today (all)"], horizontal=True, key="results_scope")

today_rows = jobs.list_day_clips(OUT_DIR, (ss.get("tz") or "Asia/Karachi").strip())
meta = {r["path"].name: r for r in today_rows}
paths = last_paths if scope == "Last run" else [r["path"] for r in today_rows]

if not paths:
    st.info("No clips to show yet. Add a video above and press START CLIPPING.")
else:
    st.markdown(f"**{len(paths)} clip(s)** ready")
    sig = tuple((str(p), p.stat().st_mtime) for p in paths)
    st.download_button("DOWNLOAD ALL CLIPS (ZIP)", data=zip_cached(sig), file_name="gta6_clips.zip",
                       mime="application/zip", key=f"zip_{scope}")
    for row_start in range(0, len(paths), 3):
        cols = st.columns(3)
        for col, p in zip(cols, paths[row_start:row_start + 3]):
            with col:
                st.video(p.read_bytes(), format="video/mp4")
                info = meta.get(p.name, {})
                st.markdown(f'<div class="clip-title">{p.stem.replace("_", " ").upper()}</div>'
                            f'<div class="clip-meta">at {fmt_time(info.get("source_time"))} in the source'
                            f' · captions: {info.get("captions", "n/a")}</div>', unsafe_allow_html=True)
                st.download_button("Download clip", data=p.read_bytes(), file_name=p.name, mime="video/mp4",
                                   key=f"dl_{scope}_{p.name}")

st.markdown('<div class="footnote">Only clip streams you have permission to clip, and keep the credit on. '
            'Credit alone does not stop copyright claims on reposted footage. Not affiliated with Rockstar Games '
            'or Take-Two Interactive.</div>', unsafe_allow_html=True)

# --------------------------------------------------------------------------- #
# dashboard (rendered last so it reflects this run)
# --------------------------------------------------------------------------- #
_tz = (ss.get("tz") or "Asia/Karachi").strip()
_stats = jobs.get_stats(OUT_DIR, _tz)
dash_box.markdown(theme.dashboard_html(_stats["total"], _stats["today"], int(max_day), ss["status"], _stats["date"]),
                  unsafe_allow_html=True)

"""
UI-independent job layer for GTA 6 CLIPPER AI.

app.py (Streamlit) only handles widgets and display; everything that actually
does work lives here so it can be tested without a browser.
"""
from __future__ import annotations

import csv
import io
import math
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional, Sequence

import clipper
from clipper import ClipperError

LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]   # (fraction 0..1, message)


@dataclass
class JobConfig:
    streamer: str = ""
    source_url: str = ""            # channel / stream link that is credited on every clip
    clip_len: int = 45              # seconds
    max_per_day: int = 10
    detect_audio: bool = True       # audio highlight detection (off = evenly spaced clips)
    captions: bool = True
    srt_path: Optional[str] = None
    min_score: float = 3.0
    title_prefix: str = "GTA 6 LIVE MOMENT"
    whisper_model: str = "base"
    lang: Optional[str] = None
    crf: int = 20
    tz: str = "Asia/Karachi"
    out_dir: str = "shorts_out"
    record_minutes: int = 10        # only used for live URLs


@dataclass
class JobResult:
    created: list = field(default_factory=list)   # list[Path] of clips made in this run
    day_dir: Optional[Path] = None
    count: int = 0                                 # clips made today (after this run)
    total: int = 0                                 # lifetime clips


# --------------------------------------------------------------------------- #
def system_status() -> dict:
    """What is installed on this machine (shown in the app sidebar)."""
    return {
        "ffmpeg": clipper.have("ffmpeg") and clipper.have("ffprobe"),
        "streamlink": clipper.have("streamlink"),
        "yt-dlp": clipper.have("yt-dlp"),
        "faster-whisper": clipper.whisper_available(),
    }


def build_args(cfg: JobConfig) -> SimpleNamespace:
    """Translate a JobConfig into the argparse-style namespace clipper.process() expects."""
    return SimpleNamespace(
        streamer=cfg.streamer.strip(), source_url=cfg.source_url.strip(), out=cfg.out_dir,
        max_per_day=int(cfg.max_per_day), clip_len=float(cfg.clip_len), min_score=float(cfg.min_score),
        no_detect=not cfg.detect_audio, no_captions=not cfg.captions, srt=cfg.srt_path, chat_csv=None,
        whisper_model=cfg.whisper_model, lang=(cfg.lang or None), title_prefix=cfg.title_prefix.strip() or "GTA 6 LIVE MOMENT",
        crf=int(cfg.crf), tz=cfg.tz, offset=0.0, dry_run=False,
    )


def validate(cfg: JobConfig, n_files: int, stream_url: str) -> list:
    """Return a list of human-readable problems (empty list = good to go)."""
    problems = []
    st = system_status()
    if not st["ffmpeg"]:
        problems.append("FFmpeg was not found. Install ffmpeg and make sure it is on your PATH.")
    if not cfg.streamer.strip():
        problems.append("Enter the streamer's name - it is credited on every clip.")
    if n_files == 0 and not stream_url.strip():
        problems.append("Upload at least one video, or enter a stream URL.")
    if n_files == 0 and stream_url.strip():
        if not re.match(r"^https?://\S+$", stream_url.strip()):
            problems.append("The stream URL must start with http:// or https://")
        if not (st["streamlink"] or st["yt-dlp"]):
            problems.append("Recording a stream URL needs streamlink or yt-dlp (pip install streamlink yt-dlp).")
    if cfg.max_per_day < 1:
        problems.append("Maximum clips per day must be at least 1.")
    if cfg.clip_len < 5:
        problems.append("Clip duration is too short.")
    return problems


def get_stats(out_dir: str, tz: str) -> dict:
    state = clipper.load_state(Path(out_dir) / "state.json", clipper.today_str(tz))
    return {"date": state["date"], "today": state["count"], "total": state.get("total", 0)}


# --------------------------------------------------------------------------- #
def run_job(cfg: JobConfig, files: Sequence = (), stream_url: str = "",
            log: Optional[LogFn] = None, progress: Optional[ProgressFn] = None) -> JobResult:
    """Run the clipper on uploaded files (or a live stream URL) and return what was created."""
    problems = validate(cfg, len(files), stream_url)
    if problems:
        raise ClipperError(" ".join(problems))

    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "state.json"
    state = clipper.load_state(state_path, clipper.today_str(cfg.tz))
    if state["count"] >= cfg.max_per_day:
        raise ClipperError(f"Daily cap reached ({state['count']}/{cfg.max_per_day}) for {state['date']}. "
                           "Raise 'Maximum clips per day' or come back tomorrow.")

    before = set(state["clips"])
    args = build_args(cfg)

    def emit(frac: float, msg: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, frac)), msg)

    clipper.set_log_sink(log)
    chunk_path: Optional[Path] = None
    try:
        if files:
            n = len(files)
            for i, f in enumerate(files):
                remaining = cfg.max_per_day - state["count"]
                if remaining <= 0:
                    clipper.log("Daily cap reached - skipping the remaining videos.")
                    break
                limit = math.ceil(remaining / (n - i))     # share the remaining quota between videos

                def cb(done: int, total: int, msg: str, i: int = i) -> None:
                    emit((i + (done / total if total else 0.0)) / n, msg)

                clipper.process(Path(f), args, state, out_dir, state_path, cfg.source_url.strip(),
                                offset=0.0, limit=limit, progress_cb=cb)
        else:
            work = out_dir / "_chunks"
            work.mkdir(exist_ok=True)
            secs = int(cfg.record_minutes) * 60

            def tick(elapsed: float, total: float) -> None:
                emit(0.5 * elapsed / total, f"Recording live stream  {int(elapsed // 60)}:{int(elapsed % 60):02d} / "
                                            f"{int(total // 60)}:{int(total % 60):02d}")

            chunk_path = clipper.record_chunk(stream_url.strip(), secs, work / f"chunk_{int(time.time())}.ts", tick=tick)
            if chunk_path is None:
                raise ClipperError("Nothing could be recorded - the stream may be offline, private, or the URL is wrong.")

            def cb2(done: int, total: int, msg: str) -> None:
                emit(0.5 + 0.5 * (done / total if total else 0.0), msg)

            clipper.process(chunk_path, args, state, out_dir, state_path,
                            cfg.source_url.strip() or stream_url.strip(), offset=0.0, limit=None, progress_cb=cb2)
        emit(1.0, "Done")
    finally:
        clipper.set_log_sink(None)
        if chunk_path is not None:
            chunk_path.unlink(missing_ok=True)

    day_dir = out_dir / state["date"]
    created = [day_dir / name for name in state["clips"] if name not in before]
    return JobResult(created=created, day_dir=day_dir, count=state["count"], total=state.get("total", 0))


# --------------------------------------------------------------------------- #
def list_day_clips(out_dir: str, tz: str) -> list:
    """All clips made today (newest numbering last), with their credits.csv info."""
    day_dir = Path(out_dir) / clipper.today_str(tz)
    csv_path = day_dir / "credits.csv"
    if not csv_path.exists():
        return []
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = day_dir / row.get("file", "")
            if p.exists():
                rows.append({"path": p, "title": row.get("title", p.stem), "source_time": row.get("source_time_sec", ""),
                             "captions": row.get("captions", "")})
    return rows


def build_zip(paths: Sequence) -> bytes:
    """ZIP with the clips, their title/description files and the day's credits.csv."""
    buf = io.BytesIO()
    seen_csv = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:     # mp4 is already compressed
        for p in map(Path, paths):
            if not p.exists():
                continue
            z.write(p, p.name)
            txt = p.with_suffix(".txt")
            if txt.exists():
                z.write(txt, txt.name)
            c = p.parent / "credits.csv"
            if c.exists() and c not in seen_csv:
                z.write(c, "credits.csv")
                seen_csv.add(c)
    return buf.getvalue()

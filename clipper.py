#!/usr/bin/env python3
"""
GTA 6 CLIPPER AI - clipping engine (also a standalone CLI)
==========================================================
Finds the most exciting moments in a stream (loud / hype audio spikes, plus
optional chat-activity spikes), cuts them into vertical 1080x1920 Shorts,
burns in captions, and credits the original streamer as prominently as possible.
Hard-capped at N clips per day (default 10).

This module is used by the Streamlit app (app.py via jobs.py) and still works
exactly as before from the command line.

Requirements: Python 3.9+, numpy, ffmpeg (with libass + freetype).
Optional:     faster-whisper  -> automatic speech captions
              streamlink / yt-dlp -> record from a live URL

CLI examples
------------
  python clipper.py stream.mp4 --streamer "StreamerName" --source-url https://twitch.tv/streamername
  python clipper.py https://twitch.tv/streamername --streamer "StreamerName" --watch
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import json
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

SR = 16000          # analysis sample rate
HOP = 0.5           # seconds per analysis window
HOP_N = int(SR * HOP)


class ClipperError(Exception):
    """A problem the user can understand and act on (bad input, missing tool, cap reached)."""


# --------------------------------------------------------------------------- #
# Logging (prints to the console and, if set, to a UI sink such as Streamlit)
# --------------------------------------------------------------------------- #
_LOG_SINK: Optional[Callable[[str], None]] = None


def set_log_sink(sink: Optional[Callable[[str], None]]) -> None:
    global _LOG_SINK
    _LOG_SINK = sink


def log(msg: str) -> None:
    print(msg, flush=True)
    if _LOG_SINK is not None:
        try:
            _LOG_SINK(msg)
        except Exception:  # a broken UI sink must never break clipping
            pass


def have(name: str) -> bool:
    return shutil.which(name) is not None


# --------------------------------------------------------------------------- #
# Dates / state (daily cap)
# --------------------------------------------------------------------------- #
def today_str(tzname: str) -> str:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(tzname)).date().isoformat()
    except Exception:
        return dt.date.today().isoformat()


def load_state(path: Path, today: str) -> dict:
    """Daily state. 'count' resets every day; 'total' is the lifetime number of clips."""
    old: dict = {}
    if path.exists():
        try:
            old = json.loads(path.read_text())
        except Exception:
            old = {}
    total = int(old.get("total", old.get("count", 0)) or 0)
    if old.get("date") != today:
        return {"date": today, "count": 0, "clips": [], "total": total}
    return {"date": today, "count": int(old.get("count", 0) or 0),
            "clips": list(old.get("clips", [])), "total": total}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
# Audio analysis -> highlight scores
# --------------------------------------------------------------------------- #
def load_audio(path: Path) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
           "-ar", str(SR), "-f", "s16le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if not r.stdout:
        raise ClipperError(
            f"Could not read audio from {path.name}. If the video has no sound, turn off "
            "audio highlight detection (--no-detect) to cut evenly spaced clips instead.")
    return np.frombuffer(r.stdout[: len(r.stdout) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0


def load_chat_counts(csv_path: str, offset: float, n_windows: int) -> np.ndarray:
    """CSV with a 'time' column (seconds from stream start), one row per chat message."""
    counts = np.zeros(n_windows, dtype=np.float32)
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                t = float(row["time"]) - offset
            except (KeyError, ValueError):
                continue
            i = int(t / HOP)
            if 0 <= i < n_windows:
                counts[i] += 1
    return counts


def zscore_robust(x: np.ndarray, floor: float) -> np.ndarray:
    med = np.median(x)
    mad = max(np.median(np.abs(x - med)) * 1.4826, floor)
    return np.clip((x - med) / mad, -3, 12)


def score_windows(audio: np.ndarray, chat: np.ndarray | None) -> np.ndarray:
    n = len(audio) // HOP_N
    frames = audio[: n * HOP_N].reshape(n, HOP_N)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-10)
    db = 20 * np.log10(rms)
    kernel = np.ones(3) / 3
    # pad with the edge value first: zero-padding would make the first/last window look falsely
    # loud (dB values are negative), producing bogus highlights at the very start/end of a video
    smooth = np.convolve(np.pad(db, 1, mode="edge"), kernel, mode="valid")

    loud = zscore_robust(smooth, floor=0.75)               # sustained loudness (yelling, explosions)
    onset = np.maximum(0, np.diff(smooth, prepend=smooth[0]))
    sudden = (onset - onset.mean()) / (onset.std() + 1e-6)  # sudden jumps (screams, crashes)
    score = loud + 0.5 * np.clip(sudden, -2, 8)

    if chat is not None and chat.max() > 0:
        chat_s = np.convolve(chat, np.ones(6) / 6, mode="same")
        score += 0.8 * zscore_robust(chat_s, floor=0.5)
    return score


def pick_moments(scores: np.ndarray, how_many: int, min_gap: float, min_score: float):
    chosen: list[tuple[float, float]] = []
    for i in np.argsort(scores)[::-1]:
        if scores[i] < min_score or len(chosen) >= how_many:
            break
        t = i * HOP + HOP / 2
        if any(abs(t - c[0]) < min_gap for c in chosen):
            continue
        chosen.append((t, float(scores[i])))
    return sorted(chosen)


def pick_even(dur: float, how_many: int, clip_len: float):
    """Used when audio highlight detection is off: evenly spaced, non-overlapping clips."""
    n = max(1, min(how_many, int(dur // max(clip_len, 1.0))))
    step = dur / n
    # peak position is placed so that render start == i * step (see start calc in process())
    return [(i * step + 0.6 * clip_len, 0.0) for i in range(n)]


# --------------------------------------------------------------------------- #
# Captions
# --------------------------------------------------------------------------- #
def parse_srt(path: str):
    text = Path(path).read_text(encoding="utf-8-sig", errors="ignore").replace("\r", "")
    pat = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")

    def ts(h, m, s, f):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(f) / (10 ** len(f))

    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        for k, line in enumerate(lines):
            m = pat.search(line)
            if m:
                g = m.groups()
                body = " ".join(lines[k + 1:]).strip()
                body = re.sub(r"<[^>]+>", "", body)
                if body:
                    cues.append((ts(*g[:4]), ts(*g[4:]), body))
                break
    return cues


def cues_to_words(cues, win_start: float, win_end: float):
    """Cues (stream timeline) -> word list relative to the clip start."""
    words = []
    for s, e, body in cues:
        if e <= win_start or s >= win_end:
            continue
        toks = body.split()
        if not toks:
            continue
        step = (e - s) / len(toks)
        for i, tok in enumerate(toks):
            ws, we = s + i * step, s + (i + 1) * step
            if we <= win_start or ws >= win_end:
                continue
            words.append((max(0.0, ws - win_start), min(win_end, we) - win_start, tok))
    return words


_whisper = None
_whisper_name = None


def whisper_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def transcribe_words(wav: Path, model_name: str, lang: str | None):
    global _whisper, _whisper_name
    from faster_whisper import WhisperModel  # type: ignore
    if _whisper is None or _whisper_name != model_name:
        log(f"  loading whisper model '{model_name}' (first time only)...")
        _whisper = WhisperModel(model_name, device="auto", compute_type="int8")
        _whisper_name = model_name
    segments, _ = _whisper.transcribe(str(wav), language=lang or None, word_timestamps=True, vad_filter=True)
    words = []
    for seg in segments:
        for w in (seg.words or []):
            tok = w.word.strip()
            if tok:
                words.append((w.start, w.end, tok))
    return words


def ass_time(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def build_ass(words, path: Path) -> bool:
    """Short, punchy 1-3 word caption chunks, big white text with black outline."""
    events, cur = [], []
    for w in words:
        cur.append(w)
        txt = " ".join(x[2] for x in cur)
        if len(cur) >= 3 or len(txt) >= 18 or re.search(r"[.!?,]$", w[2]):
            events.append([cur[0][0], cur[-1][1], txt])
            cur = []
    if cur:
        events.append([cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)])
    if not events:
        return False
    for i, ev in enumerate(events):           # no flicker: bridge small gaps, min length
        nxt = events[i + 1][0] if i + 1 < len(events) else ev[1] + 0.6
        if nxt - ev[1] < 0.35:
            ev[1] = nxt
        ev[1] = max(ev[1], ev[0] + 0.35)

    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Cap,DejaVu Sans,84,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,470,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for s, e, txt in events:
        clean = txt.upper().replace("{", "(").replace("}", ")").replace("\n", " ")
        lines.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{clean}")
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def find_font() -> str | None:
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "C:/Windows/Fonts/arialbd.ttf"]:
        if Path(p).exists():
            return p
    if have("fc-match"):
        out = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"], capture_output=True, text=True).stdout
        if out and Path(out).exists():
            return out
    return None


def short_url(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")


def drawtext(txtfile: str, size: int, y: str, box_alpha: float, pad: int) -> str:
    return (f"drawtext=fontfile=font.ttf:textfile={txtfile}:fontcolor=white:fontsize={size}:"
            f"x=(w-text_w)/2:y={y}:box=1:boxcolor=black@{box_alpha}:boxborderw={pad}")


def render_clip(video: Path, start: float, length: float, ass: Path | None, texts: dict,
                out_path: Path, tmp: Path, font: str | None, crf: int, meta: dict) -> None:
    if font:
        shutil.copy(font, tmp / "font.ttf")
    for name, val in texts.items():
        (tmp / f"{name}.txt").write_text(val, encoding="utf-8")

    chain = [
        "[0:v]split=2[bg][fg]",
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=40:6,eq=brightness=-0.12[b]",
        "[fg]scale=1080:-2[f]",
        "[b][f]overlay=(W-w)/2:(H-h)/2[v0]",
    ]
    last = "v0"
    if font:
        layers = [
            ("title",   drawtext("title.txt", 66, "300", 0.0, 0)),           # hook / title caption
            ("credit",  drawtext("credit.txt", 46, "560", 0.65, 20)),         # always above the gameplay
            ("source",  drawtext("source.txt", 34, "h-300", 0.6, 14)),
            ("rights",  drawtext("rights.txt", 30, "h-235", 0.6, 12)),
        ]
        for i, (_, filt) in enumerate(layers):
            chain.append(f"[{last}]{filt}[d{i}]")
            last = f"d{i}"
    if ass is not None:
        chain.append(f"[{last}]ass={ass.name}[vout]")
    else:
        chain.append(f"[{last}]null[vout]")

    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(video),
           "-filter_complex", ";".join(chain), "-map", "[vout]", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p", "-r", "30",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
    for k, v in meta.items():
        cmd += ["-metadata", f"{k}={v}"]
    cmd.append(str(out_path))
    subprocess.run(cmd, cwd=tmp, check=True, capture_output=True, text=True)


def write_description(path: Path, title: str, args, source_url: str, src_time: float) -> None:
    hh, mm, ss = int(src_time // 3600), int(src_time % 3600 // 60), int(src_time % 60)
    credit_lines = [
        f"Full credit to {args.streamer} - all footage, gameplay and commentary belong to them.",
        f"Watch the full stream: {source_url}" if source_url else f"Watch {args.streamer}'s channel for the full stream.",
        f"(Moment at about {hh}:{mm:02d}:{ss:02d} into the stream)",
        "Go follow and support the streamer!",
        "Not affiliated with Rockstar Games or Take-Two Interactive. Clipped with credit; "
        "if you're the creator and want this taken down, just ask and it's gone.",
    ]
    tags = ["#GTA6", "#GTAVI", "#GTA", "#Shorts", "#Gaming", "#" + re.sub(r"\W+", "", args.streamer)]
    path.write_text(f"TITLE:\n{title} | {args.streamer}\n\nDESCRIPTION:\n" + "\n".join(credit_lines)
                    + "\n\n" + " ".join(tags) + "\n\nPINNED COMMENT:\n"
                    f"Clip from {args.streamer}'s live stream - full credit to them! {source_url}\n",
                    encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #
ProgressCb = Callable[[int, int, str], None]   # (done, total, message); total == 0 -> indeterminate


def process(video: Path, args, state: dict, outdir: Path, state_path: Path, source_url: str,
            offset: float = 0.0, limit: int | None = None,
            progress_cb: Optional[ProgressCb] = None) -> int:
    """Cut highlights out of one video. Returns the number of clips rendered.

    Raises ClipperError for problems the user should see (unreadable file, ...).
    """
    def prog(done: int, total: int, msg: str) -> None:
        if progress_cb:
            progress_cb(done, total, msg)

    log(f"Analyzing {video.name} ...")
    prog(0, 0, f"Analyzing {video.name}")

    remaining = args.max_per_day - state["count"]
    if remaining <= 0:
        log(f"  daily cap reached ({state['count']}/{args.max_per_day}).")
        return 0

    detect = not getattr(args, "no_detect", False)
    audio = None
    if detect:
        audio = load_audio(video)
        if audio.size < SR * 5:
            log("  no usable audio found (turn off audio highlight detection to cut evenly spaced clips).")
            return 0
        dur = len(audio) / SR
    else:
        dur = probe_duration(video)
        if dur < 1:
            raise ClipperError(f"Could not read the duration of {video.name}. Is it a valid video file?")

    clip_len = min(args.clip_len, dur)
    want = min(remaining, limit) if limit else remaining

    if detect:
        chat = load_chat_counts(args.chat_csv, offset, len(audio) // HOP_N) if args.chat_csv else None
        scores = score_windows(audio, chat)
        picks = pick_moments(scores, want, min_gap=clip_len, min_score=args.min_score)
        if not picks:
            log("  no moments above the score threshold (try lowering the sensitivity threshold).")
            return 0
    else:
        picks = pick_even(dur, want, clip_len)
        log("  audio highlight detection is off: cutting evenly spaced clips.")
    log(f"  found {len(picks)} highlight(s)")
    if getattr(args, "dry_run", False):
        for t, sc in picks:
            log(f"   - {offset + t:8.1f}s  score {sc:.1f}")
        return 0

    srt_cues = parse_srt(args.srt) if getattr(args, "srt", None) else None
    use_whisper = (not args.no_captions) and srt_cues is None and whisper_available()
    if not args.no_captions and srt_cues is None and not use_whisper:
        log("  note: no speech-caption source. Install faster-whisper (pip install faster-whisper) "
            "or provide an .srt. Using title + credit captions only.")

    font = find_font()
    if not font:
        log("  warning: no font found, text overlays will be skipped.")

    day_dir = outdir / state["date"]
    day_dir.mkdir(parents=True, exist_ok=True)
    tmp = day_dir / "_tmp"
    tmp.mkdir(exist_ok=True)
    csv_path = day_dir / "credits.csv"
    new_file = not csv_path.exists()

    made = 0
    try:
        for idx, (t, sc) in enumerate(picks):
            start = min(max(0.0, t - 0.6 * clip_len), max(0.0, dur - clip_len))
            n = state["count"] + 1
            name = f"short_{n:02d}"
            title = f"{args.title_prefix} #{n}"
            src_time = offset + start
            log(f"  [{n}/{args.max_per_day}] {name}: {src_time:.0f}s  score {sc:.1f}")
            prog(idx, len(picks), f"Rendering {name} ({idx + 1}/{len(picks)})")

            words, mode = [], "title-only"
            if srt_cues is not None:
                words = cues_to_words(srt_cues, offset + start, offset + start + clip_len)
                mode = "srt"
            elif use_whisper:
                wav = tmp / "clip.wav"
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{clip_len:.2f}",
                                "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
                               check=True, capture_output=True)
                try:
                    words = transcribe_words(wav, args.whisper_model, args.lang)
                    mode = "whisper"
                except Exception as exc:  # noqa: BLE001
                    log(f"    whisper failed ({exc}); continuing without speech captions")
            ass_path = None
            if words:
                ass_path = tmp / f"{name}.ass"
                if not build_ass(words, ass_path):
                    ass_path = None
                    mode = "title-only"
            elif mode in ("srt", "whisper"):
                mode = "title-only (no speech)"

            texts = {
                "title": title.upper(),
                "credit": f"CLIP FROM {args.streamer.upper()} LIVE",
                "source": f"FULL STREAM: {short_url(source_url)}" if source_url else f"FOLLOW {args.streamer.upper()}",
                "rights": f"ALL CREDIT TO {args.streamer.upper()}",
            }
            out_path = day_dir / f"{name}.mp4"
            meta = {"title": f"{title} | {args.streamer}", "artist": args.streamer,
                    "comment": f"Clipped from {args.streamer}'s live stream. Source: {source_url}. Full credit to the streamer."}
            try:
                render_clip(video, start, clip_len, ass_path, texts, out_path, tmp, font, args.crf, meta)
            except subprocess.CalledProcessError as exc:
                tail = " | ".join((exc.stderr or "").strip().splitlines()[-3:])
                log(f"    render failed for {name}: {tail or exc}")
                continue
            write_description(day_dir / f"{name}.txt", title, args, source_url, src_time)

            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(["file", "title", "streamer", "source_url", "source_time_sec", "score", "captions"])
                    new_file = False
                w.writerow([out_path.name, title, args.streamer, source_url, round(src_time, 1), round(sc, 2), mode])

            state["count"] += 1
            state["total"] = state.get("total", 0) + 1
            state["clips"].append(out_path.name)
            save_state(state_path, state)
            made += 1
            prog(idx + 1, len(picks), f"Finished {name}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return made


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _stop_process(p: subprocess.Popen) -> None:
    try:
        p.send_signal(signal.SIGINT)
    except Exception:  # noqa: BLE001  (e.g. Windows)
        p.terminate()
    try:
        p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()


def record_chunk(url: str, seconds: int, out: Path,
                 tick: Optional[Callable[[float, float], None]] = None) -> Path | None:
    """Record `seconds` of a live stream (or until it ends). tick(elapsed, total) is called ~1/sec."""
    if have("streamlink"):
        cmd = ["streamlink", "--quiet", "-o", str(out), url, "best"]
    elif have("yt-dlp"):
        cmd = ["yt-dlp", "-q", "--no-part", "-f", "best[height<=720]/best", "-o", str(out), url]
    else:
        raise ClipperError("To record a live URL, install streamlink or yt-dlp "
                           "(or upload a recorded video instead).")
    log(f"Recording up to {seconds // 60} min from the live stream ...")
    rec_log = out.parent / f"rec_{out.stem}.log"
    with open(rec_log, "w") as errf:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errf)
        t0 = time.time()
        try:
            while p.poll() is None:
                elapsed = time.time() - t0
                if elapsed >= seconds:
                    _stop_process(p)
                    break
                if tick:
                    tick(elapsed, seconds)
                time.sleep(1)
        except BaseException:
            _stop_process(p)
            raise
    found = [f for f in sorted(out.parent.glob(out.stem + "*")) if f.suffix != ".log"]
    if found and found[0].stat().st_size > 100_000:
        return found[0]
    try:
        tail = " | ".join(rec_log.read_text(errors="ignore").strip().splitlines()[-2:])
    except Exception:  # noqa: BLE001
        tail = ""
    if tail:
        log(f"  recorder said: {tail}")
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="GTA 6 live stream -> 10 captioned, fully-credited Shorts a day.")
    ap.add_argument("source", help="video file, or a live/VOD URL (needs streamlink or yt-dlp)")
    ap.add_argument("--streamer", required=True, help="streamer name to credit (burned into every clip)")
    ap.add_argument("--source-url", default="", help="link to the streamer's channel/stream (credited in clip + description)")
    ap.add_argument("--out", default="shorts_out")
    ap.add_argument("--max-per-day", type=int, default=10)
    ap.add_argument("--clip-len", type=float, default=45.0, help="seconds per Short")
    ap.add_argument("--min-score", type=float, default=3.0, help="lower = more (and weaker) clips")
    ap.add_argument("--no-detect", action="store_true", help="skip audio highlight detection, cut evenly spaced clips")
    ap.add_argument("--srt", help="transcript of the full stream (.srt) to use for captions")
    ap.add_argument("--chat-csv", help="chat log CSV with a 'time' column (seconds) to boost hype detection")
    ap.add_argument("--whisper-model", default="base")
    ap.add_argument("--lang", default=None, help="caption language code, e.g. en (default: auto)")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--title-prefix", default="GTA 6 LIVE MOMENT")
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--tz", default="Asia/Karachi", help="timezone that defines the 'day' for the daily cap")
    ap.add_argument("--offset", type=float, default=0.0, help="seconds into the stream where this file starts")
    ap.add_argument("--watch", action="store_true", help="live mode: keep recording chunks until the daily cap is hit")
    ap.add_argument("--chunk-minutes", type=int, default=20)
    ap.add_argument("--per-chunk", type=int, default=None, help="max clips per chunk (watch default 1)")
    ap.add_argument("--dry-run", action="store_true", help="only list the moments, render nothing")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not have(tool):
            sys.exit(f"{tool} not found. Install ffmpeg first.")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    state_path = outdir / "state.json"
    state = load_state(state_path, today_str(args.tz))
    if state["count"] >= args.max_per_day:
        sys.exit(f"Daily cap reached ({state['count']}/{args.max_per_day}) for {state['date']}. Come back tomorrow.")

    is_url = bool(re.match(r"^https?://", args.source))
    source_url = args.source_url or (args.source if is_url else "")

    try:
        if not is_url:
            video = Path(args.source)
            if not video.exists():
                sys.exit(f"File not found: {video}")
            process(video, args, state, outdir, state_path, source_url, offset=args.offset, limit=args.per_chunk)
        else:
            work = outdir / "_chunks"
            work.mkdir(exist_ok=True)
            offset, idx, misses = args.offset, 0, 0
            per_chunk = args.per_chunk or (1 if args.watch else None)
            while True:
                chunk = record_chunk(args.source, args.chunk_minutes * 60, work / f"chunk_{idx:04d}.ts")
                if chunk is None:
                    misses += 1
                    log("  nothing recorded (stream offline?).")
                    if not args.watch or misses >= 6:
                        break
                    time.sleep(60)
                    continue
                misses = 0
                process(chunk, args, state, outdir, state_path, source_url, offset=offset, limit=per_chunk)
                offset += probe_duration(chunk)
                chunk.unlink(missing_ok=True)
                idx += 1
                state = load_state(state_path, today_str(args.tz))
                if not args.watch or state["count"] >= args.max_per_day:
                    break
    except KeyboardInterrupt:
        log("\nStopped.")
    except ClipperError as exc:
        sys.exit(f"Error: {exc}")

    log(f"\nDone. Today: {state['count']}/{args.max_per_day} Shorts in {outdir / state['date']}")


if __name__ == "__main__":
    main()

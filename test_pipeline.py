"""
End-to-end tests for the clipping engine and job layer (no browser / Streamlit needed).

Run from the project folder:   python -m unittest discover -s tests -v
Needs ffmpeg and numpy (scipy is NOT required).
"""
import csv
import subprocess
import sys
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import clipper  # noqa: E402
import jobs  # noqa: E402


def make_video(path: Path, seconds: int = 150, bursts=(15, 45, 75, 105, 130)) -> None:
    """Synthetic 'stream': test pattern + quiet noise with loud bursts at known times."""
    sr = 16000
    rng = np.random.default_rng(1)
    a = 0.02 * rng.standard_normal(sr * seconds)
    for t in bursts:
        n = int(sr * 2.5)
        i = int(t * sr)
        a[i:i + n] += (0.6 * np.sin(2 * np.pi * 400 * np.arange(n) / sr) + 0.3 * rng.standard_normal(n)) * np.hanning(n)
    pcm = (np.clip(a, -1, 1) * 32767).astype(np.int16)
    wav = path.with_suffix(".wav")
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=15:duration={seconds}",
                    "-i", str(wav), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)], check=True)


def read_rows(path: Path) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def probe(path: Path) -> str:
    return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.video = cls.dir / "stream.mp4"
        make_video(cls.video)
        cls.srt = cls.dir / "t.srt"
        # one cue per loud burst, so whichever moment is picked has speech to caption
        cls.srt.write_text("".join(
            f"{i}\n00:{t // 60:02d}:{t % 60:02d},000 --> 00:{(t + 3) // 60:02d}:{(t + 3) % 60:02d},000\noh my god did you see that\n\n"
            for i, t in enumerate((15, 45, 75, 105, 130), 1)), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def cfg(self, name, **kw):
        base = dict(streamer="TestStreamer", source_url="https://twitch.tv/teststreamer", clip_len=6,
                    out_dir=str(self.dir / name), max_per_day=10)
        base.update(kw)
        return jobs.JobConfig(**base)

    # -- job layer --------------------------------------------------------- #
    def test_highlight_clips_are_vertical_credited_and_counted(self):
        logs, fracs = [], []
        res = jobs.run_job(self.cfg("a", captions=True), files=[self.video], log=logs.append,
                           progress=lambda f, m: fracs.append(f))
        self.assertEqual(len(res.created), 5)                      # 5 bursts -> 5 clips
        for p in res.created:
            self.assertEqual(probe(p), "1080,1920")
            self.assertTrue(p.with_suffix(".txt").exists())
        rows = read_rows(res.day_dir / "credits.csv")
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["streamer"] == "TestStreamer" for r in rows))
        self.assertEqual(res.count, 5)
        self.assertEqual(res.total, 5)
        self.assertTrue(any("found 5 highlight" in l for l in logs))
        self.assertEqual(fracs[-1], 1.0)
        self.assertEqual(fracs, sorted(fracs))                     # progress never goes backwards

    def test_daily_cap_is_enforced_across_runs(self):
        cfg = self.cfg("cap", max_per_day=3)
        res = jobs.run_job(cfg, files=[self.video])
        self.assertEqual(len(res.created), 3)
        with self.assertRaises(clipper.ClipperError) as ctx:
            jobs.run_job(cfg, files=[self.video])
        self.assertIn("Daily cap reached", str(ctx.exception))
        # raising the cap allows more, numbering continues, lifetime total keeps growing
        res2 = jobs.run_job(self.cfg("cap", max_per_day=5), files=[self.video])
        self.assertEqual(len(res2.created), 2)
        self.assertEqual(res2.total, 5)
        self.assertEqual([p.name for p in res2.created], ["short_04.mp4", "short_05.mp4"])

    def test_detection_off_cuts_even_clips(self):
        res = jobs.run_job(self.cfg("even", detect_audio=False, max_per_day=4), files=[self.video])
        self.assertEqual(len(res.created), 4)
        for p in res.created:
            self.assertEqual(probe(p), "1080,1920")

    def test_srt_captions_are_used(self):
        cfg = self.cfg("srt", max_per_day=1, srt_path=str(self.srt))
        res = jobs.run_job(cfg, files=[self.video])
        rows = read_rows(res.day_dir / "credits.csv")
        self.assertEqual(rows[0]["captions"], "srt")

    def test_captions_off(self):
        res = jobs.run_job(self.cfg("nocap", captions=False, max_per_day=1), files=[self.video])
        rows = read_rows(res.day_dir / "credits.csv")
        self.assertEqual(rows[0]["captions"], "title-only")

    def test_multiple_files_share_the_quota(self):
        v2 = self.dir / "second.mp4"
        make_video(v2, bursts=(20, 60, 100))
        res = jobs.run_job(self.cfg("multi", max_per_day=4), files=[self.video, v2])
        self.assertEqual(len(res.created), 4)

    def test_validation_errors(self):
        self.assertTrue(jobs.validate(jobs.JobConfig(streamer=""), 1, ""))             # no streamer
        self.assertTrue(jobs.validate(jobs.JobConfig(streamer="x"), 0, ""))            # no input
        self.assertTrue(jobs.validate(jobs.JobConfig(streamer="x"), 0, "not a url"))   # bad url
        self.assertFalse(jobs.validate(jobs.JobConfig(streamer="x"), 1, ""))           # fine
        with self.assertRaises(clipper.ClipperError):
            jobs.run_job(jobs.JobConfig(streamer="", out_dir=str(self.dir / "v")), files=[self.video])

    def test_not_a_video_gives_a_clear_error(self):
        bad = self.dir / "bad.mp4"
        bad.write_bytes(b"this is not a video")
        with self.assertRaises(clipper.ClipperError):
            jobs.run_job(self.cfg("bad"), files=[bad])
        with self.assertRaises(clipper.ClipperError):
            jobs.run_job(self.cfg("bad2", detect_audio=False), files=[bad])

    def test_zip_contains_clips_descriptions_and_credits(self):
        res = jobs.run_job(self.cfg("zip", max_per_day=2), files=[self.video])
        data = jobs.build_zip(res.created)
        names = zipfile.ZipFile(__import__("io").BytesIO(data)).namelist()
        self.assertIn("short_01.mp4", names)
        self.assertIn("short_01.txt", names)
        self.assertIn("credits.csv", names)

    def test_stats_and_listing(self):
        cfg = self.cfg("stats", max_per_day=2)
        self.assertEqual(jobs.get_stats(cfg.out_dir, cfg.tz)["total"], 0)
        jobs.run_job(cfg, files=[self.video])
        st = jobs.get_stats(cfg.out_dir, cfg.tz)
        self.assertEqual((st["today"], st["total"]), (2, 2))
        self.assertEqual(len(jobs.list_day_clips(cfg.out_dir, cfg.tz)), 2)

    # -- the original CLI must still work ---------------------------------- #
    def test_cli_still_works(self):
        out = self.dir / "cli"
        cmd = [sys.executable, str(ROOT / "clipper.py"), str(self.video), "--streamer", "CliStreamer",
               "--clip-len", "6", "--max-per-day", "2", "--out", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Today: 2/2", r.stdout)
        self.assertEqual(len(list(out.glob("*/short_*.mp4"))), 2)
        r2 = subprocess.run(cmd, capture_output=True, text=True)       # cap reached -> clean refusal
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("Daily cap reached", r2.stderr)

    def test_cli_dry_run_and_no_detect(self):
        base = [sys.executable, str(ROOT / "clipper.py"), str(self.video), "--streamer", "S", "--clip-len", "6"]
        r = subprocess.run(base + ["--dry-run", "--out", str(self.dir / "dry")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("found 5 highlight", r.stdout)
        self.assertFalse(list((self.dir / "dry").glob("*/short_*.mp4")))
        r = subprocess.run(base + ["--no-detect", "--dry-run", "--max-per-day", "3", "--out", str(self.dir / "dry2")],
                           capture_output=True, text=True)
        self.assertIn("found 3 highlight", r.stdout)


if __name__ == "__main__":
    unittest.main()

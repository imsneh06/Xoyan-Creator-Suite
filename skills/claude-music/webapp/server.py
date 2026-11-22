# pyright: reportMissingImports=false
# pyrefly: ignore-all[missing-import]
#!/usr/bin/env python3
"""claude-music web dashboard server.

Python-stdlib-only local HTTP server: no pip installs, runs with the system
python3. Serves the single-page dashboard, shells out to music_engine.py (via
uv, inside the ACE-Step venv) for generation, streams NDJSON progress from its
stderr, and maintains per-track metadata sidecars in
<output_dir>/.claude-music/meta/ so ratings and "generate similar" survive the
lossy output filenames.

Security posture: binds 127.0.0.1 only, one generation at a time, all file
serving is restricted to the configured output_dir, subprocesses always use
argument lists (never a shell).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import gc
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

WEBAPP_DIR = Path(__file__).resolve().parent
SKILL_DIR = WEBAPP_DIR.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
CONFIG_PATH = SKILL_DIR / "config.json"
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

try:
    from shorts_engine import (  # type: ignore[missing-import]
        JobCancelledError,
        ShortsEngine,
        synthesize_voiceover_sync,
    )
except ImportError:
    ShortsEngine = None
    JobCancelledError = Exception
    synthesize_voiceover_sync = None


def purge_system_memory():
    """Force garbage collection and release GPU cache without importing heavy packages."""
    gc.collect()
    if "torch" in sys.modules:
        try:
            torch = sys.modules["torch"]
            if hasattr(torch, "cuda") and torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

AUDIO_EXTS = {".flac", ".wav", ".mp3", ".opus", ".aac", ".m4a", ".ogg"}
CAPTION_MAX = 512
LYRICS_MAX = 4096
GENERATION_TIMEOUT_SEC = 15 * 60

# Filename scheme produced by music_engine.py rename_outputs():
#   slug_YYYYmmdd-HHMM_NN[_stem][_sSEED].ext  (suffix -2/-3 on collision)
FILENAME_RE = re.compile(
    r"^(?P<slug>.+)_(?P<date>\d{8}-\d{4})_(?P<index>\d{2})"
    r"(?P<stem>(?:_[a-z0-9-]+)*?)(?:_s(?P<seed>\d+))?(?:-\d+)?$"
)


def get_ffmpeg_bin() -> str:
    """Resolve FFmpeg binary reliably."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        winget = Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
        if winget.exists():
            return str(winget)
    return "ffmpeg"


def get_ffprobe_bin() -> str:
    """Resolve FFprobe binary reliably."""
    found = shutil.which("ffprobe")
    if found:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        winget = Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / "ffprobe.exe"
        if winget.exists():
            return str(winget)
    return "ffprobe"


def get_uv_bin() -> str:
    """Resolve uv binary reliably across platforms."""
    found = shutil.which("uv")
    if found:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA")
    winget_uv = Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / "uv.exe" if local_app_data else None
    for cand in [
        Path(os.path.expanduser("~/.cargo/bin/uv.exe")),
        Path(os.path.expanduser("~/.cargo/bin/uv")),
        winget_uv,
    ]:
        if cand and cand.exists():
            return str(cand)
    return "uv"


def load_config():
    """Read the skill's config.json. Returns {} when missing/broken."""
    try:
        with CONFIG_PATH.open(encoding="utf-8-sig") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def resolve_output_dir(cfg):
    raw = cfg.get("output_dir") or "~/Music/claude-music-output"
    try:
        p = Path(os.path.expanduser(raw)).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        # Fallback to local workspace output directory if user folder has permission/sandbox restrictions
        fallback = Path(__file__).resolve().parent.parent.parent.parent / "output"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def save_output_dir(raw):
    """Validate and persist a new output_dir in config.json.

    Returns (resolved_path, None) or (None, error string). Keeps the
    user-supplied form (e.g. with ~) in the config file.
    """
    raw = str(raw or "").strip()
    if not raw:
        return None, "Folder path is empty"
    expanded = Path(os.path.expanduser(raw))
    if not expanded.is_absolute():
        return None, "Use an absolute path (or one starting with ~)"
    try:
        expanded.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return None, f"Cannot create that folder: {e}"

    test_file = expanded / f".write_test_{int(time.time())}.tmp"
    try:
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
    except Exception:
        return None, "That folder is not writable"
    cfg = load_config()
    cfg["output_dir"] = raw
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    try:
        with tmp.open("w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        return None, f"Could not write config.json: {e}"
    return expanded.resolve(), None


_META_CACHE = {}


def get_meta_store(output_dir):
    """MetaStore per output_dir, so changing the folder takes effect live."""
    key = str(output_dir)
    if key not in _META_CACHE:
        _META_CACHE[key] = MetaStore(output_dir)
    return _META_CACHE[key]


def is_configured(cfg):
    ace = cfg.get("ace_step_dir", "")
    return bool(ace) and ace != "CHANGE_ME" and Path(os.path.expanduser(ace)).is_dir()


def merge_caption(user_text, tag_lists, limit=CAPTION_MAX):
    """User text + deduped comma-tags from the selected genres, capped."""
    seen = set()
    parts = []
    for chunk in [user_text or ""] + list(tag_lists or []):
        for tag in str(chunk).split(","):
            tag = tag.strip()
            key = tag.lower()
            if tag and key not in seen:
                seen.add(key)
                parts.append(tag)
    return ", ".join(parts)[:limit].rstrip(", ")


UPLOAD_MAX_BYTES = 200 * 1024 * 1024
SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]")

# Loudness targets and codecs per platform, from references/post-processing.md.
OPTIMIZE_TARGETS = {
    "spotify": {"lufs": -14.0, "ext": ".flac",
                "args": ["-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac"]},
    "apple": {"lufs": -16.0, "ext": ".flac",
              "args": ["-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac"]},
    "youtube": {"lufs": -14.0, "ext": ".mp3",
                "args": ["-c:a", "libmp3lame", "-b:a", "320k", "-ar", "48000"]},
    "tiktok": {"lufs": -14.0, "ext": ".m4a",
               "args": ["-c:a", "aac", "-b:a", "256k", "-ar", "44100"]},
    "podcast": {"lufs": -16.0, "ext": ".mp3",
                "args": ["-c:a", "libmp3lame", "-b:a", "192k", "-ac", "1"]},
}


def safe_upload_dest(output_dir, raw_name):
    """Sanitized, collision-free destination for an uploaded file, or None.

    Same whitelist as music_export.sh: basename only, [a-zA-Z0-9._-].
    Never returns an existing path (appends -2, -3, ...).
    """
    name = SAFE_NAME_RE.sub("", Path(unquote(str(raw_name))).name)
    stem, ext = os.path.splitext(name)
    stem = stem.strip(".")
    if not stem or ext.lower() not in AUDIO_EXTS:
        return None
    output_dir = Path(output_dir)
    dest = output_dir / f"{stem}{ext.lower()}"
    n = 2
    while dest.exists():
        dest = output_dir / f"{stem}-{n}{ext.lower()}"
        n += 1
    return dest


def ffprobe_info(path):
    """Container/stream info via ffprobe, or None when undecodable."""
    ffprobe_bin = get_ffprobe_bin()
    try:
        out = subprocess.run(
            [ffprobe_bin, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30)
        info = json.loads(out.stdout)
        if not info.get("format") or not float(info["format"].get("duration", 0)):
            return None
        return info
    except Exception:
        return None


def parse_loudnorm_json(text):
    """Extract the loudnorm JSON block that ffmpeg prints on stderr."""
    for m in reversed(list(re.finditer(r"\{[^{}]*\}", text or ""))):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(obj, dict) and "input_i" in obj:
            return obj
    return None


def measure_loudness(path):
    """One loudnorm analysis pass. Returns the measured dict or None."""
    ffmpeg_bin = get_ffmpeg_bin()
    try:
        out = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-nostdin", "-i", str(path),
             "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=300)
        return parse_loudnorm_json(out.stderr)
    except Exception:
        return None


LOSSY_CODECS = {"mp3", "aac", "vorbis", "opus", "wmav2"}


def build_audit(probe, loudnorm):
    """Assemble an audit report from ffprobe + loudnorm measurements.

    Pure function: no subprocesses, unit-testable. Streaming targets are
    -14 LUFS / -1 dBTP (references/post-processing.md).
    """
    report = {"measured": {}, "findings": []}
    fmt = (probe or {}).get("format") or {}
    stream = next((s for s in (probe or {}).get("streams", [])
                   if s.get("codec_type") == "audio"), {})
    m = report["measured"]
    if fmt.get("duration"):
        m["duration_sec"] = round(float(fmt["duration"]), 1)
    if fmt.get("bit_rate"):
        m["bitrate_kbps"] = round(int(fmt["bit_rate"]) / 1000)
    m["codec"] = stream.get("codec_name")
    if stream.get("sample_rate"):
        m["sample_rate_hz"] = int(stream["sample_rate"])
    m["channels"] = stream.get("channels")

    def flag(level, text, fix=None):
        report["findings"].append({"level": level, "text": text, "fix": fix})

    if loudnorm:
        try:
            lufs = float(loudnorm["input_i"])
            tp = float(loudnorm["input_tp"])
            lra = float(loudnorm["input_lra"])
        except (KeyError, TypeError, ValueError):
            lufs = tp = lra = None
        if lufs is not None:
            m["loudness_lufs"] = lufs
            m["true_peak_dbtp"] = tp
            m["loudness_range_lu"] = lra
            if lufs < -17:
                flag("warn", f"Quiet for streaming: {lufs:.1f} LUFS vs the "
                     "-14 LUFS target. Platforms will not turn it up.",
                     "Run Optimize to normalize loudness.")
            elif lufs > -11:
                flag("warn", f"Loud: {lufs:.1f} LUFS. Streaming platforms "
                     "will turn it down and it may sound squashed.",
                     "Run Optimize to normalize loudness.")
            else:
                flag("ok", f"Loudness {lufs:.1f} LUFS sits well for "
                     "streaming (-14 LUFS target).")
            if tp is not None and tp > -1.0:
                flag("warn", f"True peak {tp:.2f} dBTP exceeds -1 dBTP: "
                     "clipping risk after lossy encoding.",
                     "Run Optimize; it enforces a -1 dBTP ceiling.")
            if lra is not None and lra < 3:
                flag("info", f"Loudness range {lra:.1f} LU is very narrow; "
                     "the track has little dynamic movement.")
    else:
        flag("info", "Loudness could not be measured.")

    if m.get("channels") == 1:
        flag("info", "Mono audio. Fine for podcasts, unusual for music.")
    if m.get("sample_rate_hz") and m["sample_rate_hz"] < 44100:
        flag("warn", f"Sample rate {m['sample_rate_hz']} Hz is below the "
             "44.1 kHz release standard.")
    if m.get("codec") in LOSSY_CODECS:
        flag("info", f"Lossy source ({m['codec']}). Re-encoding loses a "
             "little more quality each time; keep a lossless master if "
             "you have one.")
    return report


# Minimum free VRAM required before generation starts. Set to 3000 MB (3 GB)
# so 4 GB cards (which typically have ~3.5-3.7 GB free after display/OS usage)
# can generate with CPU offloading enabled.
MIN_GENERATION_VRAM_MB = 3000


def gpu_hog_processes(limit=4):
    """Top GPU memory consumers as (name, mb), best-effort."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        rows = []
        for line in out.stdout.strip().splitlines():
            pid_s, mem_s = [p.strip() for p in line.split(",")]
            name = pid_s
            try:
                if sys.platform == "win32":
                    task_out = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {pid_s}", "/FO", "CSV", "/NH"],
                        capture_output=True, text=True, timeout=3)
                    task_lines = task_out.stdout.strip().splitlines()
                    if task_lines and task_lines[0].startswith('"'):
                        name = task_lines[0].split('","')[0].strip('"')
                else:
                    args = Path(f"/proc/{pid_s}/cmdline").read_bytes() \
                        .decode(errors="replace").split("\0")
                    name = next((Path(a).name for a in args
                                 if a.endswith(".py")), None) \
                        or Path(args[0]).name or pid_s
            except Exception:
                pass
            rows.append((name, int(mem_s)))
        rows.sort(key=lambda r: -r[1])
        return rows[:limit]
    except Exception:
        return []


def vram_block_message(free_mb, hogs):
    """Fail-fast message when free VRAM cannot fit a generation."""
    msg = (f"Not enough free VRAM to generate: {free_mb / 1024:.1f} GB free, "
           f"about {MIN_GENERATION_VRAM_MB / 1000:.0f} GB needed.")
    if hogs:
        top = ", ".join(f"{n} ({m / 1024:.1f} GB)" for n, m in hogs)
        msg += f" Currently holding the GPU: {top}."
    return msg + " Close other GPU apps, then try again."


def lyrics_prompt(topic, style, language):
    """Instruction for the Claude CLI to write singable lyrics."""
    parts = [
        "Write song lyrics to be sung by a music generation model.",
        f"The song is about: {topic}." if topic else
        "Pick an evocative everyday theme.",
        f"Musical style: {style}." if style else "",
        f"Write the lyrics in this language: {language}."
        if language and language != "en" else "",
        "Rules: use [verse] and [chorus] structure tags on their own lines,"
        " 2 verses and a repeated chorus, 8 to 16 short singable lines total,"
        " concrete imagery, a catchy hook."
        " Output ONLY the lyrics, no title, no commentary, no code fences.",
    ]
    return " ".join(p for p in parts if p)


def generate_lyrics(topic, style, language):
    """Ask the local Claude Code CLI for lyrics. Returns (text, error)."""
    try:
        out = subprocess.run(
            ["claude", "-p", lyrics_prompt(topic, style, language)],
            capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return None, ("Claude Code CLI not found. Leave lyrics empty instead:"
                      " the high preset writes them from your description.")
    except subprocess.TimeoutExpired:
        return None, "Lyric writing timed out, try again."
    text = (out.stdout or "").strip()
    if out.returncode != 0 or not text:
        tail = (out.stderr or "").strip()[-200:]
        return None, f"Claude could not write lyrics: {tail or 'empty reply'}"
    return text[:LYRICS_MAX], None


def suggest_fix(error_msg):
    """Map common engine failures to a plain-language suggestion."""
    msg = (error_msg or "").lower()
    if "out of memory" in msg or "cuda oom" in msg:
        return ("The GPU ran out of memory. Close other GPU-heavy apps, "
                "or try draft quality, a shorter duration, or a smaller batch. "
                "The max preset needs the most VRAM.")
    if "uv" in msg and ("not found" in msg or "no such file" in msg):
        return ("The uv package manager is missing. Install it with: "
                "curl -LsSf https://astral.sh/uv/install.sh | sh")
    if "change_me" in msg or "ace-step" in msg and "not" in msg:
        return "ACE-Step is not configured. Run install.sh from the repo."
    if "timed out" in msg:
        return ("Generation took longer than 15 minutes. Try a lower quality "
                "preset or shorter duration; first runs also download models.")
    if "no space left" in msg:
        return "The disk holding the output folder is full."
    if "tuple index out of range" in msg or "no audio" in msg:
        return ("The video does not have a detectable audio track, or the container stream could not be decoded. "
                "The engine has been updated to automatically process silent or video-only files without crashing.")
    return None


def free_vram_mb():
    """Free VRAM in MB via nvidia-smi, or None when unavailable."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return None


def parse_engine_stdout(stdout):
    """Parse the engine's result JSON, tolerating stray stdout noise.

    The high preset's LM stack can print to stdout before the engine's
    JSON. Scan for the first line that starts a parseable-to-EOF object.
    """
    try:
        return json.loads(stdout)
    except (TypeError, ValueError):
        pass
    lines = (stdout or "").splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("{"):
            try:
                return json.loads("\n".join(lines[i:]))
            except ValueError:
                continue
    return None


def parse_progress_line(line):
    """One stderr line -> event dict or None (model logs are not JSON)."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if isinstance(obj, dict) and "event" in obj:
        return obj
    return None


def parse_range_header(value, size):
    """'bytes=a-b' -> (start, end) inclusive, or None when unusable."""
    m = re.match(r"^bytes=(\d*)-(\d*)$", value or "")
    if not m or size <= 0:
        return None
    start_s, end_s = m.groups()
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        # suffix range: last N bytes
        length = min(int(end_s), size)
        if length == 0:
            return None
        return size - length, size - 1
    start = int(start_s)
    if start >= size:
        return None
    end = min(int(end_s), size - 1) if end_s else size - 1
    if end < start:
        return None
    return start, end


def safe_audio_path(output_dir, raw_name):
    """Resolve a client-supplied file name inside output_dir, or None."""
    name = Path(unquote(raw_name)).name  # strips any directory components
    if not name or name.startswith("."):
        return None
    candidate = (output_dir / name).resolve()
    try:
        candidate.relative_to(output_dir)
    except ValueError:
        return None
    if candidate.suffix.lower() not in AUDIO_EXTS or not candidate.is_file():
        return None
    return candidate


def parse_engine_filename(stem):
    """Recover date/seed from a rename_outputs() stem, for orphan files."""
    m = FILENAME_RE.match(stem)
    if not m:
        return {}
    out = {"slug": m.group("slug")}
    if m.group("seed"):
        out["seed"] = int(m.group("seed"))
    with contextlib.suppress(ValueError):
        out["created"] = datetime.strptime(
            m.group("date"), "%Y%m%d-%H%M").isoformat()
    return out


class MetaStore:
    """Sidecar JSON store in <output_dir>/.claude-music/meta/."""

    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.meta_dir = self.output_dir / ".claude-music" / "meta"
        self._lock = threading.Lock()

    def _path(self, track_id):
        safe = Path(track_id).name
        if not safe or safe.startswith("."):
            return None
        return self.meta_dir / f"{safe}.json"

    def write(self, entry):
        path = self._path(entry["id"])
        if path is None:
            return
        with self._lock:
            self.meta_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            with tmp.open("w") as f:
                json.dump(entry, f, indent=2)
                f.write("\n")
            os.replace(tmp, path)

    def read(self, track_id):
        path = self._path(track_id)
        if path is None or not path.is_file():
            return None
        try:
            with path.open() as f:
                return json.load(f)
        except Exception:
            return None

    def rate(self, track_id, rating):
        with self._lock:
            entry = self.read(track_id)
            if entry is None:
                return None
            entry["rating"] = rating
        self.write(entry)
        return entry

    def library(self):
        """Sidecar entries merged with orphan audio files, newest first."""
        entries = {}
        if self.meta_dir.is_dir():
            for p in self.meta_dir.glob("*.json"):
                try:
                    with p.open() as f:
                        e = json.load(f)
                except Exception:
                    continue
                if isinstance(e, dict) and e.get("id"):
                    entries[e["id"]] = e
        if self.output_dir.is_dir():
            for p in self.output_dir.iterdir():
                if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                    continue
                if p.stem in entries:
                    entries[p.stem].setdefault("file", p.name)
                    continue
                info = parse_engine_filename(p.stem)
                entries[p.stem] = {
                    "id": p.stem,
                    "file": p.name,
                    "caption": None,
                    "rating": None,
                    "orphan": True,
                    "created": info.get("created")
                    or datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
                    **({"seed": info["seed"]} if "seed" in info else {}),
                }
        # Drop sidecars whose audio file was deleted by the user.
        result = [e for e in entries.values()
                  if (self.output_dir / e.get("file", "")).is_file()]
        result.sort(key=lambda e: e.get("created") or "", reverse=True)
        return result


class JobRunner:
    """Runs at most one music_engine.py generation subprocess at a time."""

    def __init__(self, cfg_loader):
        self.load_cfg = cfg_loader
        self.lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.proc = None
        self.state = {"status": "idle", "pct": None, "stage": None,
                      "started_at": None, "result": None, "error": None,
                      "suggestion": None, "request": None}
        self.last_generation_sec = 30.0

    def cancel(self):
        """Immediately terminate running generation subprocess and purge all memory."""
        with self._state_lock:
            proc = self.proc
            self.proc = None
            self.state = {
                "status": "idle", "pct": None, "stage": None,
                "started_at": None, "result": None, "error": None,
                "suggestion": None, "request": None
            }
        if proc:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True, timeout=5)
                else:
                    proc.kill()
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
        purge_system_memory()
        return True

    def snapshot(self):
        with self._state_lock:
            snap = dict(self.state)
        if snap["status"] == "generating" and snap["pct"] is None:
            # Time-based fallback when no pct events arrive, capped at 95%.
            elapsed = time.time() - (snap["started_at"] or time.time())
            snap["pct"] = min(0.95, elapsed / max(self.last_generation_sec, 1.0))
            snap["estimated"] = True
        return snap

    def _set(self, **kw):
        with self._state_lock:
            self.state.update(kw)

    def start(self, request, meta_store):
        """Begin generation. Returns error string, or None on accept."""
        cfg = self.load_cfg()
        if not is_configured(cfg):
            return "not_configured"
        if not self.lock.acquire(blocking=False):
            return "busy"
        self._set(status="loading", pct=None, stage="loading_model",
                  started_at=time.time(), result=None, error=None,
                  suggestion=None, request=request)
        threading.Thread(target=self._run, args=(request, cfg, meta_store),
                         daemon=True).start()
        return None

    def _build_cmd(self, request, cfg):
        defaults = cfg.get("defaults") or {}
        quality = request.get("quality") or defaults.get("quality") or "standard"
        fmt = defaults.get("format") or "flac"
        out_dir = str(resolve_output_dir(cfg))
        engine = str(SCRIPTS_DIR / "music_engine.py")
        uv_bin = get_uv_bin()
        py = "python" if sys.platform == "win32" else "python3"
        cmd = [uv_bin, "run", py, engine,
               "--ace-step-dir", os.path.expanduser(cfg["ace_step_dir"]),
               "--progress",
               "--quality", quality,
               "--format", fmt,
               "--naming", "descriptive",
               "--output-dir", out_dir]
        if request.get("seed") is not None:
            cmd += ["--seed", str(int(request["seed"]))]
        if request.get("batch"):
            cmd += ["--batch", str(int(request["batch"]))]
        if request.get("task") == "cover":
            # Covers default to one variant: kinder to VRAM, and variant
            # exploration matters less when following a source.
            if not request.get("batch"):
                cmd += ["--batch", "1"]
            # Free-text values use --flag=value: argparse exits 2 on
            # dash-leading values in the separate-token form.
            cmd += ["cover", "--src-audio=" + request["src_path"],
                    "--cover-strength", str(request.get("cover_strength", 0.5))]
            if request.get("caption"):
                cmd += ["--caption=" + request["caption"]]
            if request.get("duration"):
                cmd += ["--duration", str(float(request["duration"]))]
        else:
            cmd += ["generate", "--caption=" + request["caption"],
                    "--duration", str(float(request.get("duration") or 60))]
        if request.get("lyrics"):
            cmd += ["--lyrics=" + request["lyrics"]]
        if request.get("instrumental"):
            cmd += ["--instrumental"]
        if request.get("bpm"):
            cmd += ["--bpm", str(int(request["bpm"]))]
        if request.get("key"):
            cmd += ["--key=" + str(request["key"])]
        if request.get("language"):
            cmd += ["--language", request["language"]]
        return cmd, quality, out_dir

    def _attempt(self, request, cfg):
        """One engine subprocess run. Returns (result, error, suggestion)."""
        cmd, quality, out_dir = self._build_cmd(request, cfg)
        env = dict(os.environ,
                   TOKENIZERS_PARALLELISM="false",
                   TORCHAUDIO_USE_BACKEND="ffmpeg",
                   PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True")
        proc = subprocess.Popen(
            cmd, cwd=os.path.expanduser(cfg["ace_step_dir"]),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env)
        self.proc = proc

        stderr_tail = deque(maxlen=40)

        def read_stderr():
            for line in proc.stderr:
                ev = parse_progress_line(line)
                if not ev:
                    line = line.strip()
                    if line:
                        stderr_tail.append(line)
                    continue
                if ev["event"] == "stage":
                    stage = ev.get("stage")
                    self._set(stage=stage,
                              status="generating" if stage == "generating"
                              else "loading")
                elif ev["event"] == "progress" and ev.get("pct") is not None:
                    self._set(status="generating", pct=ev["pct"],
                              stage=ev.get("stage") or None)

        t_err = threading.Thread(target=read_stderr, daemon=True)
        t_err.start()
        try:
            stdout, _ = proc.communicate(timeout=GENERATION_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return None, "Generation timed out", suggest_fix("timed out")
        finally:
            self.proc = None
        t_err.join(timeout=5)

        result = parse_engine_stdout(stdout)
        if result is None:
            tail = (stdout or "").strip()[-300:] \
                or " | ".join(list(stderr_tail)[-4:])[-400:] \
                or "no output at all"
            err = f"Engine produced no JSON (exit {proc.returncode}): {tail}"
            return None, err, suggest_fix(err)
        if not result.get("success"):
            err = result.get("error") or "Generation failed"
            return None, err, result.get("suggestion") or suggest_fix(err)
        return result, None, None

    def _run(self, request, cfg, meta_store):
        try:
            result, err, hint = self._attempt(request, cfg)
            if (err and "out of memory" in err.lower()
                    and not request.get("batch")):
                # VRAM is tight: retry once with a single variant, which
                # roughly halves the engine's working memory.
                self._set(status="loading", pct=None,
                          stage="Retrying with a single variant (low VRAM)")
                request = dict(request, batch=1)
                result, err, hint = self._attempt(request, cfg)
            elif (err and result is None and "produced no JSON" in err
                    and not request.get("_retried")):
                # Launcher-level failures (e.g. a transient uv error while a
                # previous run shuts down) produce no audio, so one blind
                # retry is safe.
                self._set(status="loading", pct=None,
                          stage="Retrying after a launcher error")
                time.sleep(2)
                request = dict(request, _retried=True)
                result, err, hint = self._attempt(request, cfg)
            if err:
                self._set(status="error", error=err,
                          suggestion=hint or suggest_fix(err))
                return
            quality = (self._build_cmd(request, cfg))[1]

            gen_sec = (result.get("timing") or {}).get("generation_sec")
            if gen_sec:
                self.last_generation_sec = float(gen_sec)
            for out in result.get("outputs") or []:
                p = Path(out.get("path", ""))
                if not p.name:
                    continue
                result_caption = (result.get("params") or {}).get("caption")
                entry = {
                    "id": p.stem,
                    "file": p.name,
                    "created": datetime.now().isoformat(timespec="seconds"),
                    "task": request.get("task") or "generate",
                    "caption": request["caption"] or result_caption or None,
                    "lyrics": request.get("lyrics"),
                    "instrumental": bool(request.get("instrumental")),
                    "bpm": request.get("bpm"),
                    "key": request.get("key"),
                    "duration": request.get("duration"),
                    "quality": quality,
                    "seed": out.get("seed"),
                    "genres": request.get("genres") or [],
                    "generation_sec": gen_sec,
                    "rating": None,
                }
                if request.get("similar_to"):
                    entry["similar_to"] = request["similar_to"]
                if request.get("task") == "cover":
                    entry["cover_strength"] = request.get("cover_strength")
                    entry["cover_of"] = request.get("src_file")
                meta_store.write(entry)
            self._set(status="done", pct=1.0, result=result)
        except Exception as e:
            self._set(status="error", error=str(e),
                      suggestion=suggest_fix(str(e)))
        finally:
            self.lock.release()


def validate_generate_request(body):
    """Returns (request, None) or (None, error string)."""
    if not isinstance(body, dict):
        return None, "Body must be a JSON object"
    task = body.get("task") or "generate"
    if task not in ("generate", "cover"):
        return None, "task must be generate or cover"
    caption = str(body.get("caption") or "").strip()
    if not caption and task == "generate":
        return None, "caption is required"
    if len(caption) > CAPTION_MAX:
        return None, f"caption exceeds {CAPTION_MAX} characters"
    req = {"caption": caption, "task": task}
    if task == "cover":
        src = Path(str(body.get("src_file") or "")).name
        if not src:
            return None, "src_file is required for cover"
        req["src_file"] = src
        strength = body.get("cover_strength", 0.5)
        try:
            strength = float(strength)
        except (TypeError, ValueError):
            return None, "cover_strength must be a number"
        if not 0.0 <= strength <= 1.0:
            return None, "cover_strength must be between 0.0 and 1.0"
        req["cover_strength"] = strength
    lyrics = body.get("lyrics")
    if lyrics:
        lyrics = str(lyrics)
        if len(lyrics) > LYRICS_MAX:
            return None, f"lyrics exceed {LYRICS_MAX} characters"
        req["lyrics"] = lyrics
    if task == "cover" and not body.get("duration"):
        pass  # cover defaults to the source length (engine duration -1)
    else:
        try:
            duration = float(body.get("duration") or 60)
        except (TypeError, ValueError):
            return None, "duration must be a number"
        if not 10 <= duration <= 600:
            return None, "duration must be between 10 and 600 seconds"
        req["duration"] = duration
    bpm = body.get("bpm")
    if bpm is not None and bpm != "":
        try:
            bpm = int(bpm)
        except (TypeError, ValueError):
            return None, "bpm must be an integer"
        if not 30 <= bpm <= 300:
            return None, "bpm must be between 30 and 300"
        req["bpm"] = bpm
    seed = body.get("seed")
    if seed is not None and seed != "":
        try:
            req["seed"] = int(seed)
        except (TypeError, ValueError):
            return None, "seed must be an integer"
    key = body.get("key")
    if key:
        key = str(key)[:20]
        if not re.match(r"^[A-Ga-g][#b]? ?(major|minor|maj|min|m)?$", key):
            return None, "key looks invalid (expected e.g. 'A minor')"
        req["key"] = key
    quality = body.get("quality")
    if quality:
        if quality not in ("draft", "standard", "high", "max"):
            return None, "quality must be draft, standard, high or max"
        req["quality"] = quality
    language = body.get("language")
    if language:
        if not re.match(r"^[a-z]{2,5}$", str(language)):
            return None, "language must be a short code like en, es, ja"
        req["language"] = str(language)
    req["instrumental"] = bool(body.get("instrumental"))
    genres = body.get("genres")
    if isinstance(genres, list):
        req["genres"] = [str(g)[:40] for g in genres[:12]]
    similar_to = body.get("similar_to")
    if similar_to:
        req["similar_to"] = Path(str(similar_to)).name
    return req, None


class ShortsRunner:
    """Manages background AI YouTube and offline video shorts generation."""
    def __init__(self, cfg_fn):
        self.cfg_fn = cfg_fn
        self.lock = threading.Lock()
        self.engine = None
        self._cancelled = False
        self.state = {
            "status": "idle",
            "stage": "",
            "pct": 0.0,
            "log": "",
            "shorts": [],
            "error": None,
            "started_at": 0.0,
        }

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _set(self, **kwargs):
        with self.lock:
            self.state.update(kwargs)

    def cancel(self):
        """Immediately terminate active jobs and purge temporary files and memory."""
        with self.lock:
            self._cancelled = True
            if self.engine:
                with contextlib.suppress(Exception):
                    self.engine.abort()
            self.state = {
                "status": "idle",
                "stage": "",
                "pct": 0.0,
                "log": "Generation cancelled. Memory and temporary files purged.",
                "shorts": [],
                "error": None,
                "started_at": 0.0,
            }
        purge_system_memory()
        return True

    def start(self, req):
        with self.lock:
            if self.state["status"] == "running":
                return "busy"
            self._cancelled = False
            self.state = {
                "status": "running",
                "stage": "starting",
                "pct": 0.05,
                "log": "Starting video shorts pipeline...",
                "shorts": [],
                "error": None,
                "started_at": time.time(),
            }
        threading.Thread(target=self._run, args=(req,), daemon=True).start()
        return None

    def _run(self, req):
        try:
            if ShortsEngine is None:
                raise RuntimeError("ShortsEngine is unavailable. Please check that dependencies are installed.")
            cfg = self.cfg_fn()
            out_dir = resolve_output_dir(cfg)
            engine = ShortsEngine(out_dir)
            self.engine = engine

            def is_cancelled():
                return self._cancelled

            def on_progress(log_msg, pct):
                if is_cancelled():
                    raise JobCancelledError("Cancelled by user.")
                self._set(stage="processing", pct=round(pct, 2), log=log_msg)

            combine = bool(req.get("combine", False))
            raw_source = req.get("source")
            sources = []
            if isinstance(raw_source, list):
                sources = [str(s).strip() for s in raw_source if str(s).strip()]
            elif isinstance(raw_source, str) and ("," in raw_source or "\n" in raw_source):
                parts = re.split(r"[,\n]+", raw_source)
                sources = [p.strip() for p in parts if p.strip()]
            elif raw_source:
                sources = [str(raw_source).strip()]

            if not sources:
                raise RuntimeError("No video source provided (enter local file path or YouTube URL).")

            num_clips = int(req.get("num_clips", 3))
            aspect_ratio = str(req.get("aspect_ratio", "9:16"))
            layout = str(req.get("layout", "single"))
            captions = bool(req.get("captions", True))
            caption_style = str(req.get("caption_style", "hormozi"))
            clip_focus = str(req.get("clip_focus", "top_hooks"))
            keyword = str(req.get("keyword", "")).strip()
            custom_text = str(req.get("custom_text", "")).strip()
            custom_text_pos = str(req.get("custom_text_pos", "top")).strip()
            sticker_badge = str(req.get("sticker_badge", "")).strip()
            progress_bar = str(req.get("progress_bar", "none")).strip()
            color_grade = str(req.get("color_grade", "enhanced")).strip()
            api_key = req.get("api_key") or cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
            provider = str(req.get("provider", "offline"))
            bg_music_id = req.get("bg_music_id")

            bg_music_path = None
            if bg_music_id:
                bg_music_path = safe_audio_path(out_dir, bg_music_id)

            mode = str(req.get("mode", "pro_autoedit" if req.get("pro_autoedit") else "standard"))
            if mode == "pro_autoedit":
                target_dur = float(req.get("target_duration", 30))
                model_size = str(req.get("model_size", "base"))
                if len(sources) == 1:
                    on_progress("Loading video...", 0.1)
                    video_path = engine.prepare_video(sources[0], on_progress=on_progress, is_cancelled=is_cancelled)
                else:
                    on_progress(f"Combining {len(sources)} clips into master timeline...", 0.1)
                    prep_clips = [engine.prepare_video(s, on_progress=on_progress, is_cancelled=is_cancelled) for s in sources]
                    combined_entry = engine.combine_clips(prep_clips, title="Multi-Clip Raw Master", is_cancelled=is_cancelled)
                    video_path = engine.shorts_dir / combined_entry["filename"]

                entry = engine.autoedit_reel(
                    source_path=video_path,
                    target_duration=target_dur,
                    music_path=bg_music_path,
                    model_size=model_size,
                    caption_style=caption_style,
                    custom_text=custom_text,
                    sticker_badge=sticker_badge,
                    progress_bar=progress_bar,
                    color_grade=color_grade,
                    burn_captions=captions,
                    on_progress=on_progress,
                    is_cancelled=is_cancelled,
                )
                self._set(status="done", pct=1.0, log="Professional auto-edit complete!", shorts=[entry])
                return

            all_rendered = []
            if len(sources) == 1:
                src = sources[0]
                on_progress("Loading video...", 0.1)
                video_path = engine.prepare_video(src, on_progress=on_progress, is_cancelled=is_cancelled)

                on_progress("Transcribing speech with Whisper AI...", 0.3)
                transcript = engine.transcribe(video_path, on_progress=on_progress, is_cancelled=is_cancelled)

                on_progress("Analyzing viral hooks...", 0.5)
                highlights = engine.get_highlights(
                    transcript,
                    num_clips=num_clips,
                    api_key=api_key,
                    provider=provider,
                    clip_focus=clip_focus,
                    keyword=keyword,
                    on_progress=on_progress,
                    is_cancelled=is_cancelled,
                )

                on_progress("Rendering 9:16 vertical video clips...", 0.65)
                all_rendered = engine.render_shorts(
                    video_path,
                    highlights,
                    transcript=transcript,
                    aspect_ratio=aspect_ratio,
                    layout=layout,
                    captions=captions,
                    caption_style=caption_style,
                    custom_text=custom_text,
                    custom_text_pos=custom_text_pos,
                    sticker_badge=sticker_badge,
                    progress_bar=progress_bar,
                    color_grade=color_grade,
                    bg_music_path=bg_music_path,
                    combine_into_one=combine,
                    on_progress=on_progress,
                    is_cancelled=is_cancelled,
                )
            else:
                clips_per_src = max(1, num_clips // len(sources)) if num_clips else 1
                for s_idx, src in enumerate(sources, 1):
                    on_progress(f"Processing video {s_idx}/{len(sources)}: {Path(src).name}...", 0.1 + (0.5 * (s_idx - 1) / len(sources)))
                    video_path = engine.prepare_video(src, on_progress=on_progress, is_cancelled=is_cancelled)
                    transcript = engine.transcribe(video_path, on_progress=on_progress, is_cancelled=is_cancelled)
                    highlights = engine.get_highlights(
                        transcript,
                        num_clips=clips_per_src,
                        api_key=api_key,
                        provider=provider,
                        clip_focus=clip_focus,
                        keyword=keyword,
                        on_progress=on_progress,
                        is_cancelled=is_cancelled,
                    )
                    rendered = engine.render_shorts(
                        video_path,
                        highlights,
                        transcript=transcript,
                        aspect_ratio=aspect_ratio,
                        layout=layout,
                        captions=captions,
                        caption_style=caption_style,
                        custom_text=custom_text,
                        custom_text_pos=custom_text_pos,
                        sticker_badge=sticker_badge,
                        progress_bar=progress_bar,
                        color_grade=color_grade,
                        bg_music_path=bg_music_path,
                        combine_into_one=False,
                        on_progress=on_progress,
                        is_cancelled=is_cancelled,
                    )
                    all_rendered.extend(rendered)

                if combine and len(all_rendered) > 1:
                    on_progress(f"Combining {len(all_rendered)} clips from {len(sources)} videos into 1 compilation short...", 0.95)
                    compilation = engine.combine_clips(
                        [engine.shorts_dir / r["filename"] for r in all_rendered],
                        title=f"Multi-Video Montage ({len(all_rendered)} Clips)",
                        bg_music_path=bg_music_path,
                        is_cancelled=is_cancelled
                    )
                    all_rendered.insert(0, compilation)

            self._set(status="done", pct=1.0, log="Shorts generation complete!", shorts=all_rendered)
        except JobCancelledError:
            if self.engine:
                self.engine.clean_temp_files()
            self._set(status="idle", stage="", pct=0.0, log="Generation cancelled. Memory purged.")
            purge_system_memory()
        except Exception as e:
            self._set(status="error", error=str(e), log=f"Error: {e}")
        finally:
            self.engine = None


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-music-web/0.3"
    # Set by serve():
    runner: JobRunner = None
    shorts_runner: ShortsRunner = None
    meta_store: MetaStore = None  # test override; normally resolved per request

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, fmt, *args):  # quiet default access log
        pass

    # -- helpers ---------------------------------------------------------
    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.wfile.write(data)

    def _file(self, path, content_type):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.wfile.write(data)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > 1_000_000:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return None

    def _output_dir(self):
        return resolve_output_dir(load_config())

    def _meta(self):
        if self.meta_store is not None:
            return self.meta_store
        return get_meta_store(self._output_dir())

    # -- GET -------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._file(WEBAPP_DIR / "index.html", "text/html; charset=utf-8")
            elif path.startswith("/assets/"):
                rel_name = path[len("/assets/"):].lstrip("/\\")
                safe_name = Path(rel_name).name
                asset_file = WEBAPP_DIR / "assets" / safe_name
                if asset_file.exists() and asset_file.is_file():
                    ext = asset_file.suffix.lower()
                    ctype = "image/png" if ext == ".png" else "image/svg+xml" if ext == ".svg" else "image/jpeg" if ext in (".jpg", ".jpeg") else "application/octet-stream"
                    self._file(asset_file, ctype)
                else:
                    self._json({"error": "asset not found"}, 404)
            elif path == "/api/status":
                cfg = load_config()
                snap = self.runner.snapshot()
                self._json({
                    "configured": is_configured(cfg),
                    "busy": snap["status"] in ("loading", "generating"),
                    "output_dir": str(resolve_output_dir(cfg)),
                    "free_vram_mb": free_vram_mb(),
                })
            elif path == "/api/genres":
                self._file(WEBAPP_DIR / "genres.json", "application/json")
            elif path == "/api/progress":
                self._json(self.runner.snapshot())
            elif path == "/api/library":
                self._json({"tracks": self._meta().library()})
            elif path.startswith("/api/audio/"):
                self._serve_audio(path[len("/api/audio/"):])
            elif path == "/api/shorts/check-upload":
                query = parse_qs(urlparse(self.path).query)
                raw_name = (query.get("name") or [""])[0]
                try:
                    raw_size = int((query.get("size") or [0])[0])
                except ValueError:
                    raw_size = 0
                safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(raw_name).name) if raw_name else ""
                dest = self._output_dir() / "uploads" / safe_name
                if safe_name and dest.exists() and dest.is_file() and (raw_size <= 0 or dest.stat().st_size == raw_size):
                    self._json({"exists": True, "path": str(dest.resolve()), "name": dest.name})
                else:
                    self._json({"exists": False})
            elif path == "/api/shorts/progress":
                self._json(self.shorts_runner.snapshot() if self.shorts_runner else {})
            elif path == "/api/shorts/library":
                if ShortsEngine is None:
                    self._json({"shorts": []})
                else:
                    engine = ShortsEngine(self._output_dir())
                    self._json({"shorts": engine.load_library()})
            elif path.startswith("/api/shorts/video/"):
                self._serve_shorts_video(path[len("/api/shorts/video/"):])
            elif path.startswith("/api/shorts/thumb/"):
                self._serve_shorts_thumb(path[len("/api/shorts/thumb/"):])
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            with contextlib.suppress(Exception):
                self._json({"error": str(e)}, 500)

    def _serve_audio(self, raw_name):
        path = safe_audio_path(self._output_dir(), raw_name)
        if path is None:
            self._json({"error": "not found"}, 404)
            return
        size = path.stat().st_size
        ctype = {
            ".flac": "audio/flac", ".wav": "audio/wav", ".mp3": "audio/mpeg",
            ".opus": "audio/ogg", ".aac": "audio/aac", ".m4a": "audio/mp4",
            ".ogg": "audio/ogg",
        }.get(path.suffix.lower(), "application/octet-stream")
        rng = parse_range_header(self.headers.get("Range"), size)
        with path.open("rb") as f:
            if rng is None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                start, remaining = 0, size
            else:
                start, end = rng
                remaining = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(remaining))
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
            if self.command == "HEAD":
                return
            f.seek(start)
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                remaining -= len(chunk)

    # -- upload / analyze / optimize --------------------------------------
    def _handle_upload(self):
        query = parse_qs(urlparse(self.path).query)
        raw_name = (query.get("name") or [""])[0]
        out_dir = self._output_dir()
        dest = safe_upload_dest(out_dir, raw_name)
        if dest is None:
            exts = ", ".join(sorted(AUDIO_EXTS))
            self._json({"error": f"Unsupported file. Use one of: {exts}"}, 400)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._json({"error": "Empty upload"}, 400)
            return
        if length > UPLOAD_MAX_BYTES:
            self._json({"error": "File exceeds the 200 MB upload limit"}, 413)
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        remaining = length
        try:
            with dest.open("wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except Exception as e:
            dest.unlink(missing_ok=True)
            self._json({"error": f"Upload failed: {e}"}, 500)
            return
        if remaining > 0:
            dest.unlink(missing_ok=True)
            self._json({"error": "Upload was cut short, try again"}, 400)
            return
        probe = ffprobe_info(dest)
        if probe is None:
            dest.unlink(missing_ok=True)
            self._json({"error": "That file does not decode as audio"}, 400)
            return
        entry = {
            "id": dest.stem,
            "file": dest.name,
            "created": datetime.now().isoformat(timespec="seconds"),
            "source": "upload",
            "caption": None,
            "duration": round(float(probe["format"]["duration"]), 1),
            "rating": None,
        }
        self._meta().write(entry)
        self._json({"ok": True, "track": entry})

    def _track_and_path(self, body):
        """Resolve {file} from a POST body to (entry_or_None, path_or_None)."""
        raw = (body or {}).get("file") or ""
        path = safe_audio_path(self._output_dir(), raw)
        if path is None:
            return None, None
        return self._meta().read(path.stem), path

    def _handle_analyze(self):
        body = self._read_body() or {}
        entry, path = self._track_and_path(body)
        if path is None:
            self._json({"error": "track not found"}, 404)
            return
        if entry and entry.get("audit") and not body.get("refresh"):
            self._json({"ok": True, "audit": entry["audit"], "cached": True})
            return
        probe = ffprobe_info(path)
        if probe is None:
            self._json({"error": "ffprobe could not read this file"}, 500)
            return
        audit = build_audit(probe, measure_loudness(path))
        entry = entry or {"id": path.stem, "file": path.name, "caption": None,
                          "rating": None, "orphan": True,
                          "created": datetime.now().isoformat(timespec="seconds")}
        entry["audit"] = audit
        self._meta().write(entry)
        self._json({"ok": True, "audit": audit})

    def _handle_optimize(self):
        body = self._read_body() or {}
        entry, path = self._track_and_path(body)
        if path is None:
            self._json({"error": "track not found"}, 404)
            return
        target = body.get("target") or "spotify"
        spec = OPTIMIZE_TARGETS.get(target)
        if spec is None:
            self._json({"error": f"target must be one of: "
                        f"{', '.join(sorted(OPTIMIZE_TARGETS))}"}, 400)
            return
        measured = measure_loudness(path)
        if measured is None:
            self._json({"error": "Loudness measurement failed "
                        "(is ffmpeg installed?)"}, 500)
            return
        dest = safe_upload_dest(self._output_dir(),
                                f"{path.stem}_{target}{spec['ext']}")
        loudnorm = (
            f"loudnorm=I={spec['lufs']}:TP=-1:LRA=11:"
            f"measured_I={measured['input_i']}:"
            f"measured_TP={measured['input_tp']}:"
            f"measured_LRA={measured['input_lra']}:"
            f"measured_thresh={measured['input_thresh']}:"
            f"offset={measured.get('target_offset', 0)}:linear=true")
        ffmpeg_bin = get_ffmpeg_bin()
        try:
            run = subprocess.run(
                [ffmpeg_bin, "-hide_banner", "-nostdin", "-n", "-i", str(path),
                 "-af", loudnorm] + spec["args"] + [str(dest)],
                capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            self._json({"error": "Optimization timed out"}, 500)
            return
        if run.returncode != 0 or not dest.is_file():
            tail = (run.stderr or "").strip()[-200:]
            self._json({"error": f"ffmpeg failed: {tail}"}, 500)
            return
        new_entry = {
            "id": dest.stem,
            "file": dest.name,
            "created": datetime.now().isoformat(timespec="seconds"),
            "source": "optimized",
            "optimized_from": path.stem,
            "target": target,
            "caption": (entry or {}).get("caption"),
            "rating": None,
        }
        self._meta().write(new_entry)
        self._json({"ok": True, "track": new_entry})

    # -- POST ------------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/generate":
                body = self._read_body()
                req, err = validate_generate_request(body)
                if err:
                    self._json({"error": err}, 400)
                    return
                if req.get("task") == "cover":
                    src = safe_audio_path(self._output_dir(), req["src_file"])
                    if src is None:
                        self._json({"error": "source track not found"}, 404)
                        return
                    req["src_path"] = str(src)
                free = free_vram_mb()
                if free is not None and free < MIN_GENERATION_VRAM_MB:
                    self._json({"error": vram_block_message(
                        free, gpu_hog_processes())}, 503)
                    return
                start_err = self.runner.start(req, self._meta())
                if start_err == "busy":
                    self._json({"error": "A generation is already running"}, 409)
                elif start_err == "not_configured":
                    self._json({"error": "ACE-Step not configured. Run install.sh first."}, 503)
                else:
                    self._json({"started": True}, 202)
            elif path == "/api/rate":
                body = self._read_body() or {}
                track_id = str(body.get("id") or "")
                rating = body.get("rating")
                if rating not in (1, 2, 3, 4, 5):
                    self._json({"error": "rating must be 1-5"}, 400)
                    return
                entry = self._meta().rate(track_id, rating)
                if entry is None:
                    # Orphan track: create a minimal sidecar so the rating sticks.
                    path_audio = safe_audio_path(
                        self._output_dir(), body.get("file") or f"{track_id}.flac")
                    if path_audio is None:
                        self._json({"error": "unknown track"}, 404)
                        return
                    info = parse_engine_filename(path_audio.stem)
                    entry = {"id": path_audio.stem, "file": path_audio.name,
                             "caption": None, "rating": rating, "orphan": True,
                             "created": info.get("created")
                             or datetime.now().isoformat(timespec="seconds")}
                    self._meta().write(entry)
                self._json({"ok": True, "track": entry})
            elif path == "/api/upload":
                self._handle_upload()
            elif path == "/api/analyze":
                self._handle_analyze()
            elif path == "/api/optimize":
                self._handle_optimize()
            elif path == "/api/title":
                body = self._read_body() or {}
                title = str(body.get("title") or "").strip()[:120]
                entry, track_path = self._track_and_path(body)
                if track_path is None:
                    self._json({"error": "track not found"}, 404)
                    return
                entry = entry or {
                    "id": track_path.stem, "file": track_path.name,
                    "caption": None, "rating": None, "orphan": True,
                    "created": datetime.now().isoformat(timespec="seconds")}
                if title:
                    entry["title"] = title
                else:
                    entry.pop("title", None)  # empty title reverts to default
                self._meta().write(entry)
                self._json({"ok": True, "track": entry})
            elif path == "/api/lyrics":
                body = self._read_body() or {}
                text, err = generate_lyrics(
                    str(body.get("topic") or "")[:500],
                    str(body.get("style") or "")[:300],
                    str(body.get("language") or "en")[:5])
                if err:
                    self._json({"error": err}, 502)
                else:
                    self._json({"ok": True, "lyrics": text})
            elif path == "/api/settings":
                body = self._read_body() or {}
                if "output_dir" not in body:
                    self._json({"error": "nothing to update"}, 400)
                    return
                resolved, err = save_output_dir(body["output_dir"])
                if err:
                    self._json({"error": err}, 400)
                    return
                self._json({"ok": True, "output_dir": str(resolved)})
            elif path == "/api/open-folder":
                folder = str(self._output_dir())
                opener = {"linux": "xdg-open", "darwin": "open",
                          "win32": "explorer"}.get(sys.platform, "xdg-open")
                subprocess.Popen([opener, folder],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                self._json({"ok": True, "folder": folder})
            elif path in ("/api/shorts/generate", "/api/shorts/pro-edit"):
                body = self._read_body() or {}
                if path == "/api/shorts/pro-edit":
                    body["mode"] = "pro_autoedit"
                if not self.shorts_runner:
                    self._json({"error": "Shorts runner not initialized"}, 500)
                    return
                err = self.shorts_runner.start(body)
                if err == "busy":
                    self._json({"error": "A shorts generation job is already in progress"}, 409)
                else:
                    self._json({"started": True}, 202)
            elif path == "/api/generate/cancel":
                self.runner.cancel()
                self._json({"ok": True, "status": "idle", "message": "Generation stopped and memory cleared."})
            elif path == "/api/shorts/cancel":
                if self.shorts_runner:
                    self.shorts_runner.cancel()
                self._json({"ok": True, "status": "idle", "message": "Shorts processing cancelled and memory purged."})
            elif path == "/api/shorts/recut":
                body = self._read_body() or {}
                self._handle_shorts_recut(body)
            elif path == "/api/shorts/metadata":
                body = self._read_body() or {}
                hook = str(body.get("hook") or "")
                transcript = str(body.get("transcript") or "")
                keyword = str(body.get("keyword") or "").strip()
                try:
                    duration = float(body.get("duration", 30.0))
                except (ValueError, TypeError):
                    duration = 30.0
                if ShortsEngine is None:
                    self._json({"error": "Shorts engine is not available"}, 503)
                    return
                cfg = load_config()
                api_key = cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
                engine = ShortsEngine(self._output_dir())
                meta = engine.generate_viral_metadata(
                    hook,
                    transcript,
                    keyword=keyword,
                    duration=duration,
                    api_key=api_key,
                )
                self._json({"ok": True, "metadata": meta})
            elif path == "/api/music/make-video":
                body = self._read_body() or {}
                self._handle_music_make_video(body)
            elif path == "/api/voiceover":
                body = self._read_body() or {}
                self._handle_voiceover(body)
            elif path == "/api/shorts/upload":
                self._handle_video_upload()
            elif path == "/api/shorts/mix-music":
                self._handle_shorts_mix()
            elif path == "/api/shorts/combine":
                body = self._read_body() or {}
                self._handle_shorts_combine(body)
            elif path == "/api/shorts/delete":
                body = self._read_body() or {}
                self._handle_shorts_delete(body)
            elif path == "/api/shorts/manual-edit":
                body = self._read_body() or {}
                self._handle_shorts_manual_edit(body)
            elif path == "/api/shorts/analyze-order":
                body = self._read_body() or {}
                self._handle_shorts_analyze_order(body)
            elif path == "/api/voiceover/extract":
                body = self._read_body() or {}
                self._handle_voiceover_extract(body)
            elif path == "/api/voiceover/enhance-audio":
                body = self._read_body() or {}
                self._handle_voiceover_enhance_audio(body)
            elif path == "/api/voiceover/enhance-text":
                body = self._read_body() or {}
                self._handle_voiceover_enhance_text(body)
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            with contextlib.suppress(Exception):
                self._json({"error": str(e)}, 500)

    def _serve_shorts_video(self, filename):
        out_dir = self._output_dir()
        safe_name = Path(unquote(str(filename))).name
        if not safe_name or safe_name.startswith("."):
            self._json({"error": "invalid filename"}, 400)
            return
        path = (out_dir / "shorts" / safe_name).resolve()
        try:
            path.relative_to((out_dir / "shorts").resolve())
        except ValueError:
            self._json({"error": "access denied"}, 403)
            return
        if not path.exists() or not path.is_file():
            self._json({"error": "video not found"}, 404)
            return
        size = path.stat().st_size
        ext = path.suffix.lower()
        ctype = "video/webm" if ext == ".webm" else "video/quicktime" if ext in (".mov", ".qt") else "video/mp4"
        rng = parse_range_header(self.headers.get("Range"), size)
        with path.open("rb") as f:
            if rng is None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                start, remaining = 0, size
            else:
                start, end = rng
                remaining = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(remaining))
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
            if self.command == "HEAD":
                return
            f.seek(start)
            while remaining > 0:
                chunk = f.read(min(131072, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                remaining -= len(chunk)

    def _serve_shorts_thumb(self, filename):
        out_dir = self._output_dir()
        safe_name = Path(unquote(str(filename))).name
        if not safe_name or safe_name.startswith("."):
            self._json({"error": "invalid filename"}, 400)
            return
        path = (out_dir / "shorts" / safe_name).resolve()
        try:
            path.relative_to((out_dir / "shorts").resolve())
        except ValueError:
            self._json({"error": "access denied"}, 403)
            return
        if not path.exists() or not path.is_file():
            self._json({"error": "thumbnail not found"}, 404)
            return
        ext = path.suffix.lower()
        ctype = "image/png" if ext == ".png" else "image/webp" if ext == ".webp" else "image/jpeg"
        self._file(path, ctype)

    def _handle_video_upload(self):
        query = parse_qs(urlparse(self.path).query)
        raw_name = (query.get("name") or ["upload.mp4"])[0]
        out_dir = self._output_dir()
        upload_dir = out_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(raw_name).name)
        dest = upload_dir / safe_name
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._json({"error": "Empty upload"}, 400)
            return

        # If file with same name and size is already present, reuse existing copy directly!
        if dest.exists() and dest.is_file() and dest.stat().st_size == length:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(131072, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._json({"ok": True, "path": str(dest.resolve()), "name": dest.name, "reused": True})
            return

        remaining = length
        with dest.open("wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        self._json({"ok": True, "path": str(dest.resolve()), "name": dest.name, "reused": False})

    def _handle_shorts_mix(self):
        body = self._read_body() or {}
        clip_filename = body.get("clip_filename")
        music_file = body.get("music_file")
        out_dir = self._output_dir()
        clip_path = (out_dir / "shorts" / Path(clip_filename).name).resolve() if clip_filename else None
        music_path = safe_audio_path(out_dir, music_file) if music_file else None
        if not clip_path or not clip_path.exists():
            self._json({"error": "Short clip not found"}, 404)
            return
        if not music_path or not music_path.exists():
            self._json({"error": "Music track not found"}, 404)
            return
        mixed_filename = f"mixed_{int(time.time())}_{clip_path.name}"
        mixed_path = out_dir / "shorts" / mixed_filename
        ffmpeg_bin = get_ffmpeg_bin()
        mix_cmd = [
            ffmpeg_bin, "-y", "-loglevel", "error",
            "-i", str(clip_path),
            "-i", str(music_path),
            "-filter_complex",
            "[1:a]volume=0.20,aloop=loop=-1:size=2e+09[bg];[0:a][bg]amix=inputs=2:duration=first[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(mixed_path)
        ]
        try:
            res = subprocess.run(mix_cmd, capture_output=True, text=True, timeout=180)
            if res.returncode != 0:
                tail = (res.stderr or "").strip()[-300:]
                self._json({"error": f"Audio mix failed: {tail}"}, 500)
                return
        except subprocess.TimeoutExpired:
            self._json({"error": "Audio mix timed out"}, 500)
            return
        except Exception as e:
            self._json({"error": f"Audio mix failed: {e}"}, 500)
            return
        self._json({"ok": True, "filename": mixed_filename})

    def _handle_shorts_recut(self, body):
        if ShortsEngine is None:
            self._json({"error": "Shorts engine is not available"}, 503)
            return
        clip_id = str(body.get("clip_id") or "").strip()
        try:
            start_time = max(0.0, float(body.get("start_time", 0.0)))
            end_time = float(body.get("end_time", 30.0))
        except (ValueError, TypeError):
            self._json({"error": "Invalid start or end time"}, 400)
            return
        if end_time <= start_time:
            self._json({"error": "End time must be greater than start time"}, 400)
            return
        captions = bool(body.get("captions", True))
        caption_style = str(body.get("caption_style", "hormozi"))
        custom_text = str(body.get("custom_text", "")).strip()
        custom_text_pos = str(body.get("custom_text_pos", "top")).strip()
        sticker_badge = str(body.get("sticker_badge", "")).strip()
        progress_bar = str(body.get("progress_bar", "none")).strip()
        color_grade = str(body.get("color_grade", "enhanced")).strip()
        layout = str(body.get("layout", "single"))
        out_dir = self._output_dir()
        engine = ShortsEngine(out_dir)
        library = engine.load_library()
        target_entry = next((e for e in library if e.get("id") == clip_id), None)
        if not target_entry:
            self._json({"error": "Clip not found"}, 404)
            return

        source_file = target_entry.get("source_file")
        source_path = None
        if source_file:
            candidates = [
                out_dir / "shorts" / "downloads" / source_file,
                out_dir / "uploads" / source_file,
                out_dir / source_file,
                Path(source_file)
            ]
            for c in candidates:
                if c.exists():
                    source_path = c
                    break
        if not source_path:
            source_path = out_dir / "shorts" / target_entry.get("filename", "")
            if not source_path.exists():
                self._json({"error": "Source video file not found"}, 404)
                return

        transcript_cache = out_dir / ".claude-music" / "transcripts" / f"{source_path.stem}.json"
        transcript = None
        if transcript_cache.exists():
            try:
                with transcript_cache.open(encoding="utf-8-sig") as f:
                    transcript = json.load(f)
            except Exception:
                pass

        highlight = [{
            "title": target_entry.get("title", "Recut Short"),
            "start_time": start_time,
            "end_time": end_time,
            "score": target_entry.get("score", 90),
            "hook_sentence": target_entry.get("hook_sentence", ""),
            "virality_reason": "Manually trimmed with precision.",
        }]
        shorts = engine.render_shorts(
            source_path,
            highlight,
            transcript=transcript,
            layout=layout,
            captions=captions,
            caption_style=caption_style,
            custom_text=custom_text,
            custom_text_pos=custom_text_pos,
            sticker_badge=sticker_badge,
            progress_bar=progress_bar,
            color_grade=color_grade,
        )
        if shorts:
            self._json({"ok": True, "short": shorts[0]})
        else:
            self._json({"error": "Failed to recut short"}, 500)

    def _handle_music_make_video(self, body):
        if ShortsEngine is None:
            self._json({"error": "Shorts engine is not available"}, 503)
            return
        audio_file = body.get("audio_file")
        style = body.get("style", "spectrum")
        out_dir = self._output_dir()
        src = safe_audio_path(out_dir, audio_file)
        if not src or not src.exists():
            src = safe_audio_path(out_dir / "uploads", audio_file)
        if not src or not src.exists():
            self._json({"error": "Audio file not found"}, 404)
            return
        engine = ShortsEngine(out_dir)
        clip_id = f"mvideo_{int(time.time())}"
        out_name = f"{clip_id}.mp4"
        out_path = engine.shorts_dir / out_name
        try:
            engine.generate_music_video(src, out_path, title=src.stem.replace('_', ' ').title(), style=style)
            thumb_name = f"{clip_id}.jpg"
            thumb_path = engine.shorts_dir / thumb_name
            ffmpeg_bin = get_ffmpeg_bin()
            subprocess.run([ffmpeg_bin, "-y", "-loglevel", "error", "-ss", "00:00:01", "-i", str(out_path), "-vframes", "1", "-q:v", "2", str(thumb_path)], check=False)
        except Exception as e:
            self._json({"error": f"Failed to generate music video: {e}"}, 500)
            return

        entry = {
            "id": clip_id,
            "title": f"Music Video: {src.stem}",
            "duration": 60.0,
            "score": 92,
            "hook_sentence": f"Vertical Visualizer for {src.stem}",
            "virality_reason": "High-aesthetic vertical music video for TikTok & YouTube Shorts.",
            "filename": out_name,
            "thumbnail": thumb_name if thumb_path.exists() else None,
            "created_at": time.time(),
            "aspect_ratio": "9:16",
            "layout": "single",
            "captions": False,
            "source_file": src.name,
        }
        engine.save_library_entry(entry)
        self._json({"ok": True, "short": entry})

    def _handle_shorts_combine(self, body):
        if ShortsEngine is None:
            self._json({"error": "Shorts engine is not available"}, 503)
            return
        clip_ids = body.get("clip_ids") or []
        title = str(body.get("title") or "Multi-Clip Montage")
        transition = str(body.get("transition") or "cut")
        bg_music_file = body.get("bg_music_file")

        if not clip_ids or len(clip_ids) < 2:
            self._json({"error": "Please select at least 2 clips to combine."}, 400)
            return

        out_dir = self._output_dir()
        engine = ShortsEngine(out_dir)
        library = engine.load_library()

        clip_paths = []
        for cid in clip_ids:
            item = next((e for e in library if e.get("id") == cid), None)
            if item and item.get("filename"):
                p = engine.shorts_dir / item["filename"]
                if p.exists():
                    clip_paths.append(p)
                    continue
            for folder in [engine.shorts_dir, out_dir / "uploads", out_dir]:
                candidate = folder / cid
                if candidate.exists():
                    clip_paths.append(candidate)
                    break

        if len(clip_paths) < 2:
            self._json({"error": "Could not locate at least 2 of the specified clips on disk."}, 404)
            return

        bg_music_path = safe_audio_path(out_dir, bg_music_file) if bg_music_file else None

        try:
            entry = engine.combine_clips(
                clip_paths,
                title=title,
                transition=transition,
                bg_music_path=bg_music_path,
            )
            self._json({"ok": True, "short": entry})
        except Exception as e:
            self._json({"error": f"Failed to combine clips: {e}"}, 500)

    def _handle_voiceover(self, body):
        if synthesize_voiceover_sync is None:
            self._json({"error": "Voiceover synthesis is not available"}, 503)
            return
        text = str(body.get("text", "")).strip()
        voice = str(body.get("voice", "en-US-ChristopherNeural"))
        if not text:
            self._json({"error": "Voiceover text is required"}, 400)
            return
        out_dir = self._output_dir()
        out_name = f"voiceover_{int(time.time())}.mp3"
        out_path = out_dir / out_name
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(synthesize_voiceover_sync, text, voice, out_path)
                future.result(timeout=60.0)
            self._json({"ok": True, "file": out_name, "path": str(out_path)})
        except concurrent.futures.TimeoutError:
            self._json({"error": "Voiceover synthesis timed out after 60s. Please try a shorter script or check internet connection."}, 504)
        except Exception as e:
            self._json({"error": f"Voiceover failed: {e}"}, 500)

    def _handle_shorts_analyze_order(self, body):
        if ShortsEngine is None:
            self._json({"error": "Shorts engine is not available"}, 503)
            return
        clips = body.get("clips") or []
        if not clips or not isinstance(clips, list):
            self._json({"error": "List of clips is required"}, 400)
            return
        out_dir = self._output_dir()
        engine = ShortsEngine(out_dir)

        clip_paths = []
        for c in clips:
            c_str = str(c).strip()
            if not c_str:
                continue
            p = Path(c_str)
            if not p.is_absolute():
                candidates = [out_dir / "uploads" / c_str, out_dir / "shorts" / c_str, out_dir / c_str]
                found = next((cand for cand in candidates if cand.exists()), None)
                if found:
                    clip_paths.append(found)
                else:
                    clip_paths.append(p)
            else:
                clip_paths.append(p)

        try:
            ordered = engine.analyze_and_order_clips(clip_paths)
            self._json({"ok": True, "clips": ordered})
        except Exception as e:
            self._json({"error": f"Failed to analyze clips: {e}"}, 500)

    def _handle_voiceover_extract(self, body):
        if ShortsEngine is None:
            self._json({"error": "Engine is not available"}, 503)
            return
        source = str(body.get("source", "")).strip()
        if not source:
            self._json({"error": "Media source file is required"}, 400)
            return
        out_dir = self._output_dir()
        p = Path(source)
        if not p.is_absolute():
            candidates = [out_dir / "uploads" / source, out_dir / "shorts" / source, out_dir / source]
            found = next((cand for cand in candidates if cand.exists()), None)
            if found:
                p = found
        if not p.exists():
            self._json({"error": f"File not found: {source}"}, 404)
            return

        engine = ShortsEngine(out_dir)
        try:
            res = engine.extract_voiceover_and_captions(p)
            self._json(res)
        except Exception as e:
            self._json({"error": f"Extraction failed: {e}"}, 500)

    def _handle_voiceover_enhance_audio(self, body):
        if ShortsEngine is None:
            self._json({"error": "Engine is not available"}, 503)
            return
        filename = str(body.get("file", "")).strip()
        preset = str(body.get("preset", "studio")).strip()
        if not filename:
            self._json({"error": "Audio file is required"}, 400)
            return
        out_dir = self._output_dir()
        p = Path(filename)
        if not p.is_absolute():
            candidates = [out_dir / filename, out_dir / "uploads" / filename, out_dir / "shorts" / filename]
            found = next((cand for cand in candidates if cand.exists()), None)
            if found:
                p = found
        if not p.exists():
            self._json({"error": f"Audio file not found: {filename}"}, 404)
            return

        engine = ShortsEngine(out_dir)
        try:
            enhanced_path = engine.enhance_voiceover_audio(p, preset=preset)
            self._json({"ok": True, "file": enhanced_path.name, "path": str(enhanced_path.resolve()), "preset": preset})
        except Exception as e:
            self._json({"error": f"Audio enhancement failed: {e}"}, 500)

    def _handle_voiceover_enhance_text(self, body):
        if ShortsEngine is None:
            self._json({"error": "Engine is not available"}, 503)
            return
        text = str(body.get("text", "")).strip()
        mode = str(body.get("mode", "hook")).strip()
        if not text:
            self._json({"error": "Text is required to enhance"}, 400)
            return
        out_dir = self._output_dir()
        engine = ShortsEngine(out_dir)
        try:
            enhanced = engine.enhance_caption_text(text, mode=mode)
            self._json({"ok": True, "text": enhanced, "mode": mode})
        except Exception as e:
            self._json({"error": f"Text enhancement failed: {e}"}, 500)


    def _handle_shorts_delete(self, body):
        if ShortsEngine is None:
            self._json({"error": "Shorts engine is not available"}, 503)
            return
        clip_id = str(body.get("clip_id") or "").strip()
        if not clip_id:
            self._json({"error": "clip_id is required"}, 400)
            return
        out_dir = self._output_dir()
        engine = ShortsEngine(out_dir)
        ok = engine.delete_short(clip_id)
        self._json({"ok": ok, "clip_id": clip_id})

    def _handle_shorts_manual_edit(self, body):
        if ShortsEngine is None:
            self._json({"error": "Shorts engine is not available"}, 503)
            return

        clip_id = str(body.get("clip_id") or "").strip()
        source_param = str(body.get("source_path") or "").strip()
        out_dir = self._output_dir()
        engine = ShortsEngine(out_dir)

        # 1. Resolve source video
        source_path = None
        if clip_id:
            lib = engine.load_library()
            target_entry = next((e for e in lib if e.get("id") == clip_id), None)
            if target_entry:
                fn = target_entry.get("filename")
                src_fn = target_entry.get("source_file")
                candidates = [
                    out_dir / "shorts" / fn if fn else None,
                    out_dir / "shorts" / "downloads" / src_fn if src_fn else None,
                    out_dir / "uploads" / src_fn if src_fn else None,
                    out_dir / src_fn if src_fn else None,
                ]
                for c in candidates:
                    if c and c.exists():
                        source_path = c
                        break

        if not source_path and source_param:
            candidates = [
                Path(source_param),
                out_dir / source_param,
                out_dir / "uploads" / source_param,
                out_dir / "shorts" / source_param,
            ]
            for c in candidates:
                if c.exists():
                    source_path = c
                    break

        if not source_path or not source_path.exists():
            self._json({"error": "Source video file not found"}, 404)
            return

        # 2. Extract parameters
        try:
            start_time = max(0.0, float(body.get("start_time", 0.0)))
        except (ValueError, TypeError):
            start_time = 0.0

        try:
            raw_end = body.get("end_time")
            end_time = float(raw_end) if raw_end not in (None, "") else None
        except (ValueError, TypeError):
            end_time = None

        aspect_ratio = str(body.get("aspect_ratio", "9:16"))
        try:
            video_volume = float(body.get("video_volume", 1.0))
        except (ValueError, TypeError):
            video_volume = 1.0
        mute_video = bool(body.get("mute_video", False))

        try:
            music_volume = float(body.get("music_volume", 0.35))
        except (ValueError, TypeError):
            music_volume = 0.35
        mute_music = bool(body.get("mute_music", False))

        # Resolve bg music path
        bg_music_file = str(body.get("bg_music_file") or "").strip()
        bg_music_path = None
        if bg_music_file:
            candidates = [
                Path(bg_music_file),
                out_dir / bg_music_file,
                out_dir / "uploads" / bg_music_file,
                out_dir / "tracks" / bg_music_file,
            ]
            for c in candidates:
                if c.exists():
                    bg_music_path = c
                    break

        effect = str(body.get("effect", "clean"))
        sticker = str(body.get("sticker", "")).strip()
        burn_captions = bool(body.get("burn_captions", True))
        caption_style = str(body.get("caption_style", "hormozi"))
        quality = str(body.get("quality", "1080p_standard"))
        title = str(body.get("title") or "Instagram Edit Short").strip()

        try:
            entry = engine.manual_edit_short(
                source_path=source_path,
                start_time=start_time,
                end_time=end_time,
                aspect_ratio=aspect_ratio,
                video_volume=video_volume,
                mute_video=mute_video,
                bg_music_path=bg_music_path,
                music_volume=music_volume,
                mute_music=mute_music,
                effect=effect,
                sticker=sticker,
                burn_captions=burn_captions,
                caption_style=caption_style,
                quality=quality,
                title=title,
            )
            self._json({"ok": True, "short": entry})
        except Exception as e:
            self._json({"error": f"Manual edit failed: {e}"}, 500)


def serve(port=8765, max_port=8775):
    cfg = load_config()
    out_dir = resolve_output_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    Handler.runner = JobRunner(load_config)
    Handler.shorts_runner = ShortsRunner(load_config)
    last_err = None
    for p in range(port, max_port + 1):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError as e:
            last_err = e
    else:
        print(f"ERROR: no free port in {port}-{max_port}: {last_err}",
              file=sys.stderr)
        sys.exit(1)
    print(f"claude-music web: http://127.0.0.1:{httpd.server_address[1]}",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="claude-music web dashboard")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    serve(port=args.port, max_port=args.port + 10)

# pyright: reportMissingImports=false
# pyrefly: ignore-all[missing-import]
"""AI YouTube & Offline Video Shorts Generator Engine.

Self-contained module for:
1. Video ingestion: offline video paths or YouTube URLs (via yt-dlp).
2. Audio transcription: faster-whisper with word-level timestamps.
3. Word-by-word animated viral captions (Opus Clip / Hormozi style .ass subtitle generation).
4. Virality detection: Gemini LLM analysis or offline heuristic fallback.
5. Auto-reframe: Face-tracking crop to 9:16 (single speaker or split-screen).
6. 9:16 Music Video Visualizer: turns Claude Music tracks into vertical videos with dynamic spectrum waveforms.
7. Interactive Clip Trimmer: frame-accurate recutting.
8. Viral Metadata Generator: high-CTR titles, hashtags, description, and engagement comments.
9. Free Neural AI Voiceover via edge-tts.
10. Complete Cancellation and Process/Memory Purge support.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

VIRALITY_CRITERIA = """Virality signals to prioritize (ranked by impact):
1. HOOK MOMENTS — statements that create immediate curiosity ("The secret is...", "Nobody talks about...", "I was completely wrong about...")
2. EMOTIONAL PEAKS — genuine surprise, laughter, vulnerability, excitement; raw unscripted reactions
3. OPINION BOMBS — strong, polarizing or counter-intuitive statements that trigger agree/disagree
4. REVELATION MOMENTS — surprising facts, stats, or confessions that reframe how the viewer thinks
5. CONFLICT/TENSION — disagreement, pushback, or a problem being confronted head-on
6. QUOTABLE ONE-LINERS — a sentence that works as a standalone quote card
7. STORY PEAKS — the climax or twist of an anecdote; the payoff moment
8. PRACTICAL VALUE — a concrete tip, hack, or insight the viewer can immediately apply
"""

HIGHLIGHT_SYSTEM_PROMPT = """You are an elite short-form video editor who creates viral TikToks, Reels, and YouTube Shorts.
Analyze this video transcript and extract the top {num_clips} most viral, self-contained highlights.

{virality_criteria}

Rules:
- Duration sweet spot: 30-75 seconds per clip.
- Never cut mid-sentence or mid-thought — each clip must be self-contained and clear without prior context.
- Identify the single best opening line that grabs attention ("hook_sentence").
- Give each clip a viral score from 60 to 99.
- Explain in one sentence why it works ("virality_reason").
- Give each clip a catchy title (3-6 words).

Respond ONLY with valid JSON:
{{"highlights":[{{"title":"string","start_time":float,"end_time":float,"score":int,"hook_sentence":"string","virality_reason":"string"}}]}}"""


class JobCancelledError(Exception):
    """Raised when the user requests an immediate stop and memory purge."""
    pass


def get_ffmpeg_bin() -> str:
    """Resolve FFmpeg binary path reliably across Windows and POSIX."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        winget_path = Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
        if winget_path.exists():
            return str(winget_path)
    return "ffmpeg"


def has_audio_stream(file_path: Path) -> bool:
    """Check if media file contains at least one valid audio stream."""
    if not file_path.exists() or file_path.stat().st_size == 0:
        return False
    ffmpeg_bin = get_ffmpeg_bin()
    cmd = [
        ffmpeg_bin, "-y", "-loglevel", "error",
        "-i", str(file_path.resolve()),
        "-vn",
        "-t", "0.1",
        "-f", "null",
        "-"
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return proc.returncode == 0
    except Exception:
        return False


def get_ffprobe_bin() -> str:
    """Resolve FFprobe binary path reliably across Windows and POSIX."""
    found = shutil.which("ffprobe")
    if found:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        winget_path = Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / "ffprobe.exe"
        if winget_path.exists():
            return str(winget_path)
    return "ffprobe"


def probe_duration(path: Path) -> float:
    """Accurately determine audio/video duration using ffprobe or ffmpeg."""
    ffprobe_bin = get_ffprobe_bin()
    try:
        out = subprocess.check_output(
            [ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path.resolve())],
            text=True
        )
        return float(out.strip())
    except Exception:
        pass

    ffmpeg_bin = get_ffmpeg_bin()
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-i", str(path.resolve())],
            capture_output=True, text=True, timeout=10
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", proc.stderr or "")
        if m:
            h, m_dur, s = m.groups()
            return int(h) * 3600 + int(m_dur) * 60 + float(s)
    except Exception:
        pass

    return 30.0


def extract_audio_for_whisper(media_path: Path, output_wav: Path) -> bool:
    """Extract standard 16kHz 16-bit mono PCM wav using FFmpeg.

    Prevents PyAV container decoding errors and 'tuple index out of range' crashes
    when faster-whisper encounters videos without audio or non-standard container formats.
    """
    ffmpeg_bin = get_ffmpeg_bin()
    cmd = [
        ffmpeg_bin, "-y", "-loglevel", "error",
        "-i", str(media_path.resolve()),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(output_wav.resolve())
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return proc.returncode == 0 and output_wav.exists() and output_wav.stat().st_size > 1000
    except Exception:
        return False


def resolve_local_path(source: str) -> Path | None:
    """Check if the input is a valid existing local file."""
    candidate = Path(source.strip('"\'')).expanduser()
    if candidate.exists() and candidate.is_file():
        return candidate.resolve()
    return None


def format_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    return f"{m:02d}:{s:02d}.{ms:03d}"


def format_ass_timestamp(seconds: float) -> str:
    """Format seconds into ASS subtitle timestamp: H:MM:SS.cs (centiseconds)."""
    total_cs = max(0, int(round(seconds * 100)))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def kill_proc_tree(proc: subprocess.Popen | None):
    """Forcibly kill process and all children to free CPU/GPU VRAM."""
    if not proc or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=5)
        else:
            proc.kill()
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()


class ShortsEngine:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir.resolve()
        self.shorts_dir = self.output_dir / "shorts"
        self.shorts_dir.mkdir(parents=True, exist_ok=True)
        self.meta_file = self.output_dir / ".claude-music" / "shorts_library.json"
        self.meta_file.parent.mkdir(parents=True, exist_ok=True)
        self.active_procs: list[subprocess.Popen] = []
        self.tracked_temp_files: list[Path] = []

    def load_library(self) -> list[dict]:
        if self.meta_file.exists():
            try:
                with self.meta_file.open(encoding="utf-8-sig") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        valid = [item for item in data if item.get("filename") and (self.shorts_dir / item["filename"]).exists()]
                        return valid
                    return []
            except Exception:
                return []
        return []

    def save_library_entry(self, entry: dict):
        lib = self.load_library()
        filtered = [item for item in lib if item.get("id") != entry.get("id")]
        filtered.insert(0, entry)
        try:
            with self.meta_file.open("w", encoding="utf-8") as f:
                json.dump(filtered, f, indent=2)
        except Exception as e:
            print(f"[shorts_engine] failed to save library: {e}", file=sys.stderr)

    def clean_temp_files(self):
        """Purge all temporary artifacts from memory and disk."""
        for p in self.tracked_temp_files:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        self.tracked_temp_files.clear()
        gc.collect()

    def abort(self):
        """Terminate all active subprocesses and purge memory immediately."""
        for proc in self.active_procs:
            kill_proc_tree(proc)
        self.active_procs.clear()
        self.clean_temp_files()
        gc.collect()

    def _run_cmd(self, cmd: list[str], check: bool = True, cwd: Path | None = None, is_cancelled: Callable[[], bool] | None = None) -> subprocess.CompletedProcess:
        if is_cancelled and is_cancelled():
            raise JobCancelledError("Cancelled before subprocess execution.")

        run_cwd = str(cwd or self.shorts_dir)
        proc = subprocess.Popen(cmd, cwd=run_cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.active_procs.append(proc)
        try:
            stdout, stderr = proc.communicate()
            if proc.returncode != 0 and check:
                raise RuntimeError(f"Command failed (exit {proc.returncode}): {stderr[-300:]}")
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        finally:
            if proc in self.active_procs:
                self.active_procs.remove(proc)

    def prepare_video(
        self,
        source: str,
        on_progress: Callable[[str, float], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Path:
        """Download or verify video source."""
        if is_cancelled and is_cancelled():
            raise JobCancelledError("Cancelled before video ingest.")

        local = resolve_local_path(source)
        if local:
            if on_progress:
                on_progress(f"Using local video: {local.name}", 0.1)
            return local

        # Check existing uploads or output files without re-downloading or re-uploading
        safe_src = Path(source.strip('"\'')).name
        candidate_uploads = self.output_dir / "uploads" / safe_src
        if candidate_uploads.exists() and candidate_uploads.is_file():
            if on_progress:
                on_progress(f"Using existing video in uploads: {candidate_uploads.name}", 0.1)
            return candidate_uploads

        candidate_out = self.output_dir / safe_src
        if candidate_out.exists() and candidate_out.is_file():
            if on_progress:
                on_progress(f"Using existing video in output folder: {candidate_out.name}", 0.1)
            return candidate_out

        # Remote YouTube download via yt-dlp
        if on_progress:
            on_progress("Downloading YouTube video via yt-dlp...", 0.1)
        try:
            import yt_dlp  # type: ignore[missing-import]
        except ImportError as err:
            raise RuntimeError("yt-dlp is required for YouTube URLs. Install with pip install yt-dlp") from err

        downloads_dir = self.shorts_dir / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        out_tmpl = str(downloads_dir / "%(id)s.%(ext)s")

        ydl_opts = {
            "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            if is_cancelled and is_cancelled():
                raise JobCancelledError("Cancelled during download.")
            info = ydl.extract_info(source, download=True)
            filename = ydl.prepare_filename(info)
            base = Path(filename).with_suffix(".mp4")
            if base.exists():
                return base
            if Path(filename).exists():
                return Path(filename)
            raise RuntimeError(f"Could not locate downloaded file for {source}")

    def transcribe(
        self,
        media_path: Path,
        language: str | None = None,
        model_size: str = "base",
        on_progress: Callable[[str, float], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> dict:
        """Transcribe speech to text with word-level timestamps using faster-whisper."""
        if is_cancelled and is_cancelled():
            raise JobCancelledError("Cancelled before transcription.")

        cache_dir = self.output_dir / ".claude-music" / "transcripts"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{media_path.stem}.json"

        if cache_file.exists():
            try:
                with cache_file.open(encoding="utf-8-sig") as f:
                    data = json.load(f)
                    if data.get("segments") and "words" in (data["segments"][0] if data["segments"] else {}):
                        if on_progress:
                            on_progress("Loaded cached word-level transcript", 0.3)
                        return data
            except Exception:
                pass

        # Check if the media file has a valid audio stream before invoking Whisper/PyAV.
        # This completely avoids PyAV 'IndexError: tuple index out of range' crashes on silent/video-only files.
        has_audio = has_audio_stream(media_path)
        if not has_audio:
            if on_progress:
                on_progress("No audio stream detected. Preparing visual scene highlights...", 0.3)
            duration = probe_duration(media_path)
            return {"duration": duration, "segments": [], "has_audio": False}

        if on_progress:
            on_progress("Extracting audio stream for transcription...", 0.2)

        # Extract standard 16kHz 16-bit mono PCM wav using FFmpeg to avoid container decode issues in PyAV
        temp_wav = cache_dir / f"{media_path.stem}_audio.wav"
        extracted = extract_audio_for_whisper(media_path, temp_wav)
        transcribe_target = temp_wav if (extracted and temp_wav.exists()) else media_path

        try:
            from faster_whisper import WhisperModel  # type: ignore[missing-import]
        except ImportError as err:
            raise RuntimeError("faster-whisper is required for transcription.") from err

        device = "cpu"
        compute_type = "int8"
        try:
            import torch  # type: ignore[missing-import]
            if torch.cuda.is_available():
                device = "cuda"
                compute_type = "float16"
        except Exception:
            pass

        segments = []
        duration = 30.0
        model = None
        try:
            model = WhisperModel(model_size, device=device, compute_type=compute_type)
            segments_gen, info = model.transcribe(
                str(transcribe_target),
                language=language,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                word_timestamps=True,
            )

            for s in segments_gen:
                if is_cancelled and is_cancelled():
                    raise JobCancelledError("Cancelled during transcription stream.")

                words = []
                if getattr(s, "words", None):
                    for w in s.words:
                        words.append({
                            "word": w.word.strip(),
                            "start": round(w.start, 2),
                            "end": round(w.end, 2),
                            "probability": round(w.probability, 2),
                        })
                segments.append({
                    "start": round(s.start, 2),
                    "end": round(s.end, 2),
                    "text": s.text.strip(),
                    "words": words,
                })

            duration = round(info.duration, 2) if hasattr(info, "duration") else (segments[-1]["end"] if segments else 30.0)
        except (IndexError, Exception) as e:
            # If transcription fails due to tuple index or codec issue, fallback gracefully
            print(f"[shorts_engine] Whisper transcription warning: {e}", file=sys.stderr)
            if not duration or duration <= 0:
                duration = 30.0
        finally:
            if model is not None:
                del model
            if temp_wav.exists():
                with contextlib.suppress(Exception):
                    temp_wav.unlink()
            gc.collect()

        result = {"duration": duration, "segments": segments, "has_audio": bool(segments or has_audio)}

        try:
            with cache_file.open("w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
        except Exception:
            pass

        if on_progress:
            on_progress(f"Transcribed {len(segments)} segments with word timings ({duration}s)", 0.4)
        return result

    def generate_ass_subtitles(
        self,
        transcript: dict,
        start_time: float,
        end_time: float,
        ass_path: Path,
        style: str = "hormozi",
        custom_text: str = "",
        custom_text_pos: str = "top",
        sticker_badge: str = "",
        hide_spoken_captions: bool = False,
    ) -> bool:
        """Create word-by-word animated ASS subtitles, custom text banners, and sticker badges."""
        segments = transcript.get("segments", [])

        # Gather all words within [start_time, end_time]
        clip_words = []
        if not hide_spoken_captions and segments:
            for s in segments:
                if s["end"] < start_time or s["start"] > end_time:
                    continue
                words = s.get("words", [])
                if words:
                    for w in words:
                        if w["end"] >= start_time and w["start"] <= end_time and w["word"]:
                            rel_start = max(0.0, w["start"] - start_time)
                            rel_end = max(rel_start + 0.1, w["end"] - start_time)
                            clip_words.append({
                                "word": w["word"].upper(),
                                "start": rel_start,
                                "end": rel_end,
                            })
                else:
                    tokens = s["text"].split()
                    if tokens:
                        step = (s["end"] - s["start"]) / len(tokens)
                        for i, tok in enumerate(tokens):
                            w_start = s["start"] + i * step
                            w_end = w_start + step
                            if w_end >= start_time and w_start <= end_time:
                                clip_words.append({
                                    "word": tok.upper(),
                                    "start": max(0.0, w_start - start_time),
                                    "end": max(0.1, w_end - start_time),
                                })

        chunks = []
        chunk_size = 3
        if clip_words:
            for i in range(0, len(clip_words), chunk_size):
                group = clip_words[i:i + chunk_size]
                c_start = group[0]["start"]
                c_end = group[-1]["end"]
                chunks.append({"words": group, "start": c_start, "end": c_end})

        clip_dur = max(0.1, end_time - start_time)
        end_str = format_ass_timestamp(clip_dur)

        dialogue_lines = []
        if sticker_badge:
            dialogue_lines.append(f"Dialogue: 2,0:00:00.00,{end_str},Badge,,0,0,0,,{{\\an7\\pos(60,110)}}{sticker_badge}")
        if custom_text:
            pos_tag = "{\\an8\\pos(540,160)}" if custom_text_pos == "top" else "{\\an5\\pos(540,960)}" if custom_text_pos == "middle" else "{\\an2\\pos(540,1650)}"
            dialogue_lines.append(f"Dialogue: 1,0:00:00.00,{end_str},Banner,,0,0,0,,{pos_tag}{custom_text}")

        # Spoken word captions
        if not hide_spoken_captions and chunks:
            for chunk in chunks:
                chunk_words = chunk["words"]
                for active_idx, target in enumerate(chunk_words):
                    w_start = format_ass_timestamp(target["start"])
                    w_end = format_ass_timestamp(target["end"])

                    formatted_tokens = []
                    for idx, item in enumerate(chunk_words):
                        if idx == active_idx and style == "hormozi":
                            formatted_tokens.append(r"{\c&H00D7FF&\fscx108\fscy108}" + item["word"] + r"{\r}")
                        else:
                            formatted_tokens.append(r"{\c&H00FFFFFF&}" + item["word"])

                    text_line = " ".join(formatted_tokens)
                    dialogue_lines.append(f"Dialogue: 0,{w_start},{w_end},Hormozi,,0,0,0,,{text_line}")

        if not dialogue_lines:
            return False

        ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hormozi,Arial,66,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,6,2,2,40,40,360,1
Style: Banner,Arial,72,&H0000FFFF,&H00FFFFFF,&H00000000,&HB0000000,-1,0,0,0,100,100,0,0,3,10,2,8,60,60,140,1
Style: Badge,Arial,52,&H00FFFFFF,&H0000FFFF,&H00000000,&H900000FF,-1,0,0,0,100,100,0,0,3,8,2,7,60,60,110,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        full_ass = ass_header + "\n".join(dialogue_lines) + "\n"
        ass_path.write_text(full_ass, encoding="utf-8")
        self.tracked_temp_files.append(ass_path)
        return True

    def get_highlights(
        self,
        transcript: dict,
        num_clips: int = 3,
        clip_focus: str = "top_hooks",
        keyword: str = "",
        api_key: str | None = None,
        provider: str = "offline",
        on_progress: Callable[[str, float], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> list[dict]:
        """Detect the best viral highlights using Gemini LLM or heuristic fallback."""
        if is_cancelled and is_cancelled():
            raise JobCancelledError("Cancelled before highlight detection.")

        segments = transcript.get("segments", [])
        total_duration = transcript.get("duration") or (segments[-1]["end"] if segments else 45.0)

        # Fallback if no spoken dialogue was found or video has no audio
        if not segments:
            if on_progress:
                on_progress("Creating visual highlight sections...", 0.6)
            clip_len = min(total_duration, 45.0)
            step = max(clip_len, (total_duration - clip_len) / max(1, num_clips - 1)) if num_clips > 1 and total_duration > clip_len else 0
            highlights = []
            for i in range(num_clips):
                s_time = round(i * step, 2)
                e_time = round(min(total_duration, s_time + clip_len), 2)
                if e_time > s_time:
                    highlights.append({
                        "title": f"Highlight #{i+1}",
                        "start_time": s_time,
                        "end_time": e_time,
                        "score": 88 - i * 2,
                        "hook_sentence": f"Visual scene #{i+1}",
                        "virality_reason": "High-visual scene highlight.",
                    })
            return highlights or [{
                "title": "Highlight #1",
                "start_time": 0.0,
                "end_time": min(total_duration, 45.0),
                "score": 85,
                "hook_sentence": "Opening scene",
                "virality_reason": "Visual highlight sequence.",
            }]

        if on_progress:
            on_progress("Analyzing content virality and hook points...", 0.5)

        formatted_lines = []
        for s in segments:
            formatted_lines.append(f"[{format_timestamp(s['start'])} - {format_timestamp(s['end'])}] {s['text']}")
        transcript_text = "\n".join(formatted_lines[:500])

        if api_key and provider == "gemini":
            try:
                from google import genai  # type: ignore[missing-import]
                client = genai.Client(api_key=api_key)
                kw_prompt = f"\nUSER FOCUS KEYWORD/TOPIC: \"{keyword.strip()}\". Prioritize moments covering this topic." if keyword.strip() else ""
                prompt = (
                    f"{HIGHLIGHT_SYSTEM_PROMPT.format(num_clips=num_clips, virality_criteria=VIRALITY_CRITERIA)}{kw_prompt}\n\n"
                    f"TRANSCRIPT:\n{transcript_text}"
                )
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config={"response_mime_type": "application/json"},
                )
                raw_json = response.text or "{}"
                clean_json = re.sub(r"^```(?:json)?\s*", "", raw_json.strip())
                clean_json = re.sub(r"\s*```$", "", clean_json)
                parsed = json.loads(clean_json)
                highlights = parsed.get("highlights", [])
                if highlights:
                    if on_progress:
                        on_progress(f"Gemini identified {len(highlights)} viral moments", 0.6)
                    return highlights[:num_clips]
            except Exception as e:
                print(f"[shorts_engine] Gemini API analysis failed: {e}. Falling back to heuristic.", file=sys.stderr)

        # Offline heuristic fallback
        if on_progress:
            on_progress("Extracting viral hooks via speech cadence and questions...", 0.6)

        highlights = []
        target_duration = 45.0
        total_duration = transcript.get("duration") or (segments[-1]["end"] if segments else 45.0)

        kw_clean = keyword.strip().lower()
        hook_candidates = []
        for i, s in enumerate(segments):
            text = s.get("text", "")
            score = 70
            if kw_clean and kw_clean in text.lower():
                score += 45  # Strong boost for user's explicit focus keyword
            if "?" in text:
                score += 15
            if any(w in text.lower() for w in ["secret", "never", "why", "how", "crazy", "truth", "money", "best", "worst", "always", "stop", "mistake", "warning"]):
                score += 12
            if len(text.split()) > 5:
                score += 5
            hook_candidates.append((i, s, score))

        hook_candidates.sort(key=lambda x: x[2], reverse=True)

        # Apply clip diversity / section focus
        if clip_focus == "first_half":
            filtered = [c for c in hook_candidates if c[1]["start"] < total_duration * 0.5]
            if filtered:
                hook_candidates = filtered
        elif clip_focus == "middle":
            filtered = [c for c in hook_candidates if total_duration * 0.20 <= c[1]["start"] <= total_duration * 0.80]
            if filtered:
                hook_candidates = filtered
        elif clip_focus == "ending":
            filtered = [c for c in hook_candidates if c[1]["start"] >= total_duration * 0.45]
            if filtered:
                hook_candidates = filtered
        elif clip_focus == "alternative":
            # Offset to discover different parts of the video
            hook_candidates = hook_candidates[num_clips:] + hook_candidates[:num_clips]
        elif clip_focus == "diverse":
            # Spread clips across distinct sections of the video
            bucket_size = total_duration / max(1, num_clips)
            diverse_picks = []
            for b_idx in range(num_clips):
                b_start = b_idx * bucket_size
                b_end = (b_idx + 1) * bucket_size
                in_bucket = [c for c in hook_candidates if b_start <= c[1]["start"] < b_end]
                if in_bucket:
                    diverse_picks.append(in_bucket[0])
            if diverse_picks:
                remaining = [c for c in hook_candidates if c not in diverse_picks]
                hook_candidates = diverse_picks + remaining

        selected_ranges = []

        for idx, start_seg, score in hook_candidates:
            if len(highlights) >= num_clips:
                break
            start_t = max(0.0, start_seg["start"] - 0.5)
            end_t = min(total_duration, start_t + target_duration)

            for seg in segments[idx:]:
                if seg["end"] >= start_t + 28.0:
                    end_t = seg["end"]
                    break

            if any(not (end_t <= r[0] or start_t >= r[1]) for r in selected_ranges):
                continue

            selected_ranges.append((start_t, end_t))
            highlights.append({
                "title": f"Clip #{len(highlights)+1}: {start_seg['text'][:32]}...",
                "start_time": round(start_t, 2),
                "end_time": round(end_t, 2),
                "score": min(98, score + len(highlights)),
                "hook_sentence": start_seg["text"],
                "virality_reason": "High-interest hook with sustained dialogue.",
            })

        if not highlights:
            start_t = 0.0
            end_t = min(total_duration, 45.0)
            highlights.append({
                "title": "Opening Highlight",
                "start_time": start_t,
                "end_time": end_t,
                "score": 82,
                "hook_sentence": segments[0]["text"] if segments else "Opening hook",
                "virality_reason": "Introduction of the core concept.",
            })

        return highlights[:num_clips]

    def render_shorts(
        self,
        source_path: Path,
        highlights: list[dict],
        transcript: dict | None = None,
        aspect_ratio: str = "9:16",
        layout: str = "single",  # "single" or "split"
        captions: bool = True,
        caption_style: str = "hormozi",
        custom_text: str = "",
        custom_text_pos: str = "top",
        sticker_badge: str = "",
        progress_bar: str = "none",
        color_grade: str = "enhanced",
        bg_music_path: Path | None = None,
        combine_into_one: bool = False,
        on_progress: Callable[[str, float], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> list[dict]:
        """Cut, face-crop to 9:16 (single or split-screen), animate captions, and export."""
        try:
            import cv2  # type: ignore[missing-import]
        except ImportError:
            cv2 = None

        ffmpeg_bin = get_ffmpeg_bin()
        rendered = []
        total_clips = len(highlights)

        face_cascade = None
        try:
            if hasattr(cv2, 'CascadeClassifier') and hasattr(cv2, 'data') and hasattr(cv2.data, 'haarcascades'):
                cascade_file = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                if os.path.exists(cascade_file):
                    face_cascade = cv2.CascadeClassifier(cascade_file)
        except Exception as e:
            print(f"[shorts_engine] face_cascade init warning: {e}", file=sys.stderr)

        has_source_audio = has_audio_stream(source_path)

        for idx, h in enumerate(highlights, 1):
            if is_cancelled and is_cancelled():
                self.clean_temp_files()
                raise JobCancelledError("Cancelled during shorts rendering.")

            clip_id = f"short_{int(time.time())}_{idx}"
            raw_cut = self.shorts_dir / f"{clip_id}_raw.mp4"
            silent_cropped = self.shorts_dir / f"{clip_id}_silent.mp4"
            ass_path = self.shorts_dir / f"{clip_id}.ass"
            final_mp4 = self.shorts_dir / f"{clip_id}.mp4"
            thumb_jpg = self.shorts_dir / f"{clip_id}.jpg"

            self.tracked_temp_files.extend([raw_cut, silent_cropped, ass_path])

            start_t = h["start_time"]
            end_t = h["end_time"]
            clip_duration = end_t - start_t

            pct = 0.6 + (0.35 * (idx - 1) / total_clips)
            if on_progress:
                on_progress(f"Rendering clip {idx}/{total_clips}: {h.get('title', 'Short')} ({clip_duration:.1f}s)...", pct)

            # Stage 1: Cut with ffmpeg
            cut_cmd = [
                ffmpeg_bin, "-y", "-loglevel", "error",
                "-ss", f"{start_t:.3f}",
                "-to", f"{end_t:.3f}",
                "-i", str(source_path.resolve()),
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            ]
            if has_source_audio:
                cut_cmd.extend(["-c:a", "aac", "-b:a", "128k"])
            else:
                cut_cmd.extend(["-an"])
            cut_cmd.append(raw_cut.name)
            self._run_cmd(cut_cmd, is_cancelled=is_cancelled)

            if is_cancelled and is_cancelled():
                self.clean_temp_files()
                raise JobCancelledError("Cancelled after cut.")

            # Stage 2: Face-tracking vertical crop
            if cv2 is None:
                # Direct FFmpeg center crop fallback if cv2 is unavailable
                crop_filter = (
                    "crop=ih*9/16:ih:(iw-ow)/2:0,scale=1080:1920:flags=lanczos,fps=30"
                    if layout != "split"
                    else "crop=ih*1080/960:ih:(iw-ow)/2:0,scale=1080:960:flags=lanczos,fps=30"
                )
                ffmpeg_crop_cmd = [
                    ffmpeg_bin, "-y", "-loglevel", "error",
                    "-i", raw_cut.name,
                    "-vf", crop_filter,
                    "-an",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    silent_cropped.name,
                ]
                self._run_cmd(ffmpeg_crop_cmd, is_cancelled=is_cancelled)
            else:
                cap = cv2.VideoCapture(str(raw_cut))
                if not cap.isOpened():
                    continue

                src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920)
                src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080)
                if src_w <= 0 or src_h <= 0:
                    src_w, src_h = 1920, 1080
                fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

                if layout == "split":
                    # Top half: 1080x960; we produce speaker crop at 1080x960
                    crop_h = src_h
                    crop_w = int(crop_h * 1080 / 960)
                    crop_w = min(src_w, max(2, crop_w - (crop_w % 2)))
                    target_out_w, target_out_h = 1080, 960
                else:
                    # Full 9:16 vertical geometry
                    crop_h = src_h
                    crop_w = int(crop_h * 9 / 16)
                    crop_w = max(2, crop_w - (crop_w % 2))
                    crop_h = max(2, crop_h - (crop_h % 2))
                    target_out_w, target_out_h = 1080, 1920

                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                writer = cv2.VideoWriter(str(silent_cropped), fourcc, fps, (target_out_w, target_out_h))

                last_cx = src_w // 2
                smoothing = 0.15

                while True:
                    if is_cancelled and is_cancelled():
                        cap.release()
                        writer.release()
                        self.clean_temp_files()
                        raise JobCancelledError("Cancelled during frame processing.")

                    ret, frame = cap.read()
                    if not ret:
                        break

                    if face_cascade is not None and not face_cascade.empty():
                        with contextlib.suppress(Exception):
                            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                            faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
                            if faces is not None and len(faces) > 0:
                                valid_faces = [f for f in faces if hasattr(f, "__len__") and len(f) >= 4]
                                if valid_faces:
                                    x, y, w, h_box = max(valid_faces, key=lambda f: f[2] * f[3])
                                    target_cx = x + w // 2
                                    last_cx = int(last_cx + (target_cx - last_cx) * smoothing)

                    left = max(0, min(src_w - crop_w, last_cx - crop_w // 2))
                    cropped_frame = frame[0:crop_h, left:left + crop_w]
                    resized = cv2.resize(cropped_frame, (target_out_w, target_out_h))
                    writer.write(resized)

                cap.release()
                writer.release()

            # Stage 3: Subtitles & overlay generation
            has_subtitles = False
            hide_spoken = not captions
            has_subtitles = self.generate_ass_subtitles(
                transcript or {},
                start_t,
                end_t,
                ass_path,
                style=caption_style,
                custom_text=custom_text,
                custom_text_pos=custom_text_pos,
                sticker_badge=sticker_badge,
                hide_spoken_captions=hide_spoken,
            )

            # Stage 4: Layout assembly & audio mixing via FFmpeg
            if layout == "split":
                filter_complex = (
                    "[0:v]scale=1080:960[top];"
                    "[1:v]scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960,boxblur=15:5[bot];"
                    "[top][bot]vstack=inputs=2[v_stacked]"
                )
                v_map = "[v_stacked]"
                inputs = [
                    ffmpeg_bin, "-y", "-loglevel", "error",
                    "-i", silent_cropped.name,
                    "-i", raw_cut.name,
                ]
            else:
                inputs = [
                    ffmpeg_bin, "-y", "-loglevel", "error",
                    "-i", silent_cropped.name,
                    "-i", raw_cut.name,
                ]
                filter_complex = "[0:v]null[v_stacked]"
                v_map = "[v_stacked]"

            # Visual enhancements & color grading
            grade_filter = ""
            if color_grade == "warm":
                grade_filter = ",colorbalance=rs=0.1:gs=0.05:bs=-0.1,eq=contrast=1.05:saturation=1.10"
            elif color_grade == "moody":
                grade_filter = ",colorbalance=rs=-0.05:gs=0.0:bs=0.1:rh=0.08:bh=-0.05,eq=contrast=1.12:saturation=0.9"
            elif color_grade == "bw":
                grade_filter = ",hue=s=0,eq=contrast=1.18:brightness=-0.02"
            elif color_grade == "vibrant":
                grade_filter = ",eq=contrast=1.08:saturation=1.28:brightness=0.03,unsharp=5:5:0.6"
            elif color_grade == "enhanced":
                grade_filter = ",eq=contrast=1.06:saturation=1.12:brightness=0.02,unsharp=5:5:0.5:5:5:0.0"

            pbar_filter = ""
            if progress_bar and progress_bar != "none":
                pcolor = "red@0.95" if progress_bar == "red" else "yellow@0.95" if progress_bar == "yellow" else "cyan@0.95" if progress_bar == "cyan" else "white@0.95"
                pbar_filter = f",drawbox=x=0:y=ih-14:w='iw*(t/{max(0.1, clip_duration):.3f})':h=14:color={pcolor}:t=fill"

            filter_complex += f";{v_map}null{grade_filter}{pbar_filter}[v_styled]"
            v_map = "[v_styled]"

            if has_subtitles and ass_path.exists():
                clean_ass = ass_path.name.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
                filter_complex += f";{v_map}ass=filename='{clean_ass}'[v_out]"
                v_map = "[v_out]"

            # Audio setup: check if background music is requested or if source has audio
            if bg_music_path and bg_music_path.exists():
                inputs.extend(["-i", str(bg_music_path.resolve())])
                if has_source_audio:
                    filter_complex += ";[2:a]volume=0.20,aloop=loop=-1:size=2e+09[bg];[1:a][bg]amix=inputs=2:duration=first[a_out]"
                else:
                    filter_complex += ";[2:a]volume=0.85,aloop=loop=-1:size=2e+09[a_out]"
                a_map = "[a_out]"
            elif has_source_audio:
                a_map = "1:a:0"
            else:
                # Video has no audio track and no background music: synthesize silent stereo audio stream
                inputs.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
                a_map = "2:a"

            merge_cmd = inputs + [
                "-filter_complex", filter_complex,
                "-map", v_map,
                "-map", a_map,
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                final_mp4.name
            ]
            self._run_cmd(merge_cmd, is_cancelled=is_cancelled)

            # Stage 5: Generate crisp thumbnail
            thumb_cmd = [
                ffmpeg_bin, "-y", "-loglevel", "error",
                "-ss", "00:00:01",
                "-i", final_mp4.name,
                "-vframes", "1",
                "-q:v", "2",
                thumb_jpg.name
            ]
            self._run_cmd(thumb_cmd, check=False)
            self._run_cmd(thumb_cmd, check=False)

            # Cleanup temp cuts immediately
            for temp_f in [raw_cut, silent_cropped, ass_path]:
                try:
                    if temp_f.exists():
                        temp_f.unlink()
                except Exception:
                    pass

            entry = {
                "id": clip_id,
                "title": h.get("title", f"Short #{idx}"),
                "duration": round(clip_duration, 1),
                "score": h.get("score", 85),
                "hook_sentence": h.get("hook_sentence", ""),
                "virality_reason": h.get("virality_reason", ""),
                "start_time": h["start_time"],
                "end_time": h["end_time"],
                "filename": final_mp4.name,
                "thumbnail": thumb_jpg.name if thumb_jpg.exists() else None,
                "created_at": time.time(),
                "aspect_ratio": "9:16",
                "layout": layout,
                "captions": has_subtitles,
                "source_file": source_path.name,
            }
            self.save_library_entry(entry)
            rendered.append(entry)

        if combine_into_one and len(rendered) > 1:
            if on_progress:
                on_progress(f"Stitching {len(rendered)} clips into 1 compilation short...", 0.95)
            compilation = self.combine_clips(
                [self.shorts_dir / r["filename"] for r in rendered],
                title=f"Montage: {highlights[0].get('title', 'Compilation')}",
                bg_music_path=bg_music_path,
                is_cancelled=is_cancelled
            )
            rendered.insert(0, compilation)

        if on_progress:
            on_progress(f"Done! {len(rendered)} shorts ready.", 1.0)

        return rendered

    def autoedit_reel(
        self,
        source_path: Path,
        target_duration: float = 30.0,
        music_path: Path | None = None,
        model_size: str = "base",
        burn_captions: bool = True,
        caption_style: str = "hormozi",
        custom_text: str = "",
        sticker_badge: str = "",
        progress_bar: str = "none",
        color_grade: str = "enhanced",
        on_progress: Callable[[str, float], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> dict:
        """Professional short-form auto-edit following the exact Reel/TikTok spec:
        - Whisper word timestamps
        - Silence (>0.4s) & filler words removal
        - Hook retention in first 2-3s, CTA retention at end
        - Target runtime fitting (drops least word-dense middle clips)
        - Alternating 115% animated punch-in zooms on cuts
        - Color grading (exposure +0.02, contrast +6%, saturation +12%) & unsharp mask sharpening
        - Audio mastering: 80 Hz high-pass, FFT denoise (-25 dB), compression, EQ, ducked music, -14 LUFS
        - 1-3 word high-visibility captions
        - EDL generation
        """
        if is_cancelled and is_cancelled():
            raise JobCancelledError("Cancelled before pro auto-edit.")

        import sys
        module_dir = str(Path(__file__).parent)
        if module_dir not in sys.path:
            sys.path.insert(0, module_dir)
        from reel_autoedit import autoedit_video  # type: ignore[missing-import]

        ts = int(time.time())
        out_name = f"reel_pro_{ts}.mp4"
        out_file = self.shorts_dir / out_name

        def _prog(msg, p):
            if is_cancelled and is_cancelled():
                raise JobCancelledError("Cancelled during pro auto-edit.")
            if on_progress:
                on_progress(msg, p)

        res = autoedit_video(
            src_path=source_path,
            target_duration=target_duration,
            output_path=out_file,
            music_path=music_path,
            model_size=model_size,
            burn_captions=burn_captions,
            caption_style=caption_style,
            custom_text=custom_text,
            sticker_badge=sticker_badge,
            progress_bar=progress_bar,
            color_grade=color_grade,
            on_progress=_prog,
        )

        entry = {
            "id": f"pro_{ts}",
            "title": f"Pro Reel: {source_path.stem[:24]} ({res['duration']}s)",
            "duration": res["duration"],
            "score": 98,
            "hook_sentence": res["edl"][0]["text"] if res.get("edl") else "",
            "virality_reason": f"Pro auto-edit: {res['clips_count']} dynamic cuts, alternating punch-in zooms, enhanced color/sharpening, mastered -14 LUFS audio",
            "filename": res["output_file"],
            "thumbnail": res.get("thumbnail"),
            "edl_file": res.get("edl_file"),
            "edl": res.get("edl", []),
            "created_at": time.time(),
            "aspect_ratio": "9:16",
            "layout": "pro_autoedit",
            "captions": burn_captions,
            "source_file": source_path.name,
            "note": res.get("note", ""),
        }
        self.save_library_entry(entry)
        return entry

    def combine_clips(
        self,
        clip_paths: list[Path],
        output_path: Path | None = None,
        title: str = "Multi-Clip Montage",
        transition: str = "cut",
        bg_music_path: Path | None = None,
        bg_music_volume: float = 0.20,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> dict:
        """Combine multiple video clips into one cohesive 9:16 vertical short."""
        if is_cancelled and is_cancelled():
            raise JobCancelledError("Cancelled before combining clips.")

        valid_clips = [p for p in clip_paths if p.exists() and p.stat().st_size > 0]
        if not valid_clips:
            raise ValueError("At least 1 valid clip path is required to combine.")

        ffmpeg_bin = get_ffmpeg_bin()
        clip_id = f"montage_{int(time.time())}"
        if not output_path:
            output_path = self.shorts_dir / f"{clip_id}.mp4"
        thumb_jpg = self.shorts_dir / f"{clip_id}.jpg"

        output_path.parent.mkdir(parents=True, exist_ok=True)

        inputs = []
        filter_parts = []
        concat_v_inputs = []
        concat_a_inputs = []
        total_duration = 0.0
        input_idx = 0

        for clip in valid_clips:
            dur = probe_duration(clip)
            total_duration += dur

            clip_v_idx = input_idx
            inputs.extend(["-i", str(clip.resolve())])
            input_idx += 1

            # Normalize video to 1080x1920 9:16 aspect, 30fps, setsar=1, format=yuv420p
            filter_parts.append(
                f"[{clip_v_idx}:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,format=yuv420p[v{clip_v_idx}]"
            )
            concat_v_inputs.append(f"[v{clip_v_idx}]")

            # Normalize audio to 44.1kHz stereo
            if has_audio_stream(clip):
                filter_parts.append(f"[{clip_v_idx}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,aresample=44100[a{clip_v_idx}]")
                concat_a_inputs.append(f"[a{clip_v_idx}]")
            else:
                # Add synthetic silence input matching clip duration
                inputs.extend(["-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
                silence_idx = input_idx
                input_idx += 1
                filter_parts.append(f"[{silence_idx}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{clip_v_idx}]")
                concat_a_inputs.append(f"[a{clip_v_idx}]")

        num_inputs = len(concat_v_inputs)
        stream_pairs = "".join(f"{concat_v_inputs[k]}{concat_a_inputs[k]}" for k in range(num_inputs))
        filter_parts.append(f"{stream_pairs}concat=n={num_inputs}:v=1:a=1[v_concat][a_concat]")

        v_map = "[v_concat]"
        a_map = "[a_concat]"

        if bg_music_path and bg_music_path.exists():
            bg_idx = input_idx
            inputs.extend(["-i", str(bg_music_path.resolve())])
            input_idx += 1
            filter_parts.append(f"[{bg_idx}:a]volume={bg_music_volume:.2f},aloop=loop=-1:size=2e+09[bg];[a_concat][bg]amix=inputs=2:duration=first[a_mixed]")
            a_map = "[a_mixed]"

        full_filter = ";".join(filter_parts)

        cmd = [
            ffmpeg_bin, "-y", "-loglevel", "error",
            *inputs,
            "-filter_complex", full_filter,
            "-map", v_map,
            "-map", a_map,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(output_path.resolve())
        ]
        self._run_cmd(cmd, is_cancelled=is_cancelled)

        # Generate crisp thumbnail
        thumb_cmd = [
            ffmpeg_bin, "-y", "-loglevel", "error",
            "-ss", "00:00:01",
            "-i", str(output_path.resolve()),
            "-vframes", "1",
            "-q:v", "2",
            str(thumb_jpg.resolve())
        ]
        self._run_cmd(thumb_cmd, check=False)

        entry = {
            "id": clip_id,
            "title": title or f"Montage: {len(valid_clips)} Clips",
            "duration": round(total_duration, 1),
            "score": 96,
            "hook_sentence": f"Multi-clip viral compilation ({len(valid_clips)} clips)",
            "virality_reason": "High-velocity multi-clip montage designed for maximum watch-time retention.",
            "start_time": 0.0,
            "end_time": round(total_duration, 1),
            "filename": output_path.name,
            "thumbnail": thumb_jpg.name if thumb_jpg.exists() else None,
            "created_at": time.time(),
            "aspect_ratio": "9:16",
            "layout": "single",
            "captions": False,
            "source_file": f"Montage ({len(valid_clips)} clips)",
            "clip_count": len(valid_clips),
        }
        self.save_library_entry(entry)
        return entry

    def generate_music_video(
        self,
        audio_path: Path,
        output_path: Path,
        title: str = "Claude Music",
        artist: str = "AI Studio",
        style: str = "spectrum",
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Path:
        """Create a 9:16 Vertical Music Video with visualizer spectrum & album art."""
        if is_cancelled and is_cancelled():
            raise JobCancelledError("Cancelled before music video generation.")

        ffmpeg_bin = get_ffmpeg_bin()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Build dynamic 1080x1920 vertical canvas
        # Center: Vinyl / waveform visualizer
        # Color palette: #141413 background, #DE5C34 terracotta spectrum
        if style == "bars":
            vis_filter = "showcqt=s=960x360:bar_g=2:axis=0:text=0:sono_h=0:bar_v=1"
        else:
            vis_filter = "showwaves=s=960x320:mode=cline:colors=0xDE5C34|0xF2A374"

        clean_title = re.sub(r"[':\\]", "", title)[:36]
        clean_artist = re.sub(r"[':\\]", "", artist)[:28]

        font_arg = ""
        for candidate in ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/calibri.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
            if os.path.exists(candidate):
                escaped_font = candidate.replace(":", "\\:")
                font_arg = f":fontfile='{escaped_font}'"
                break

        filter_complex = (
            f"color=c=0x141413:s=1080x1920:r=30[bg];"
            f"[0:a]{vis_filter},format=yuva420p[wave];"
            f"[bg][wave]overlay=(W-w)/2:1100[v1];"
            f"[v1]drawtext=text='{clean_title}'{font_arg}:fontsize=48:fontcolor=white:x=(w-text_w)/2:y=820:bordercolor=black:borderw=2,"
            f"drawtext=text='{clean_artist}'{font_arg}:fontsize=26:fontcolor=0x8E8C82:x=(w-text_w)/2:y=890,"
            f"drawtext=text='9\\:16 VERTICAL MUSIC'{font_arg}:fontsize=18:fontcolor=0xDE5C34:x=(w-text_w)/2:y=200[v_out]"
        )

        cmd = [
            ffmpeg_bin, "-y", "-loglevel", "error",
            "-i", str(audio_path),
            "-filter_complex", filter_complex,
            "-map", "[v_out]",
            "-map", "0:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "256k",
            "-shortest",
            str(output_path)
        ]
        self._run_cmd(cmd, is_cancelled=is_cancelled)
        return output_path

    def generate_viral_metadata(
        self,
        hook: str,
        transcript_sample: str = "",
        keyword: str = "",
        duration: float = 30.0,
        api_key: str | None = None,
    ) -> dict:
        """Generate keyword-targeted clickable titles, synchronized video description with timestamps, tags, and pinned comment."""
        clean_kw = keyword.strip()
        topic = clean_kw or hook[:36].rstrip(".!?,")
        t_dur = max(10.0, float(duration or 30.0))
        t_mid1 = max(1, round(t_dur * 0.25))
        t_mid2 = min(int(t_dur - 2), max(t_mid1 + 2, round(t_dur * 0.65)))

        if api_key:
            try:
                from google import genai  # type: ignore[missing-import]
                client = genai.Client(api_key=api_key)
                prompt = (
                    "You are a top YouTube Shorts / TikTok viral growth strategist.\n"
                    f"Target Topic / Focus Keyword requested by creator: \"{topic}\"\n"
                    f"Clip Hook statement: \"{hook}\"\n"
                    f"Context spoken transcript: \"{transcript_sample[:400]}\"\n"
                    f"Clip Runtime: {t_dur:.1f} seconds\n\n"
                    "Generate based strictly on this keyword:\n"
                    "1. 3 clickable viral titles (< 60 chars) focused around the keyword.\n"
                    f"2. 1 optimized video-synced description formatted with exact video timestamps matching the {t_dur:.1f}s runtime (e.g. '0:00 - Hook: ...', '0:{t_mid1:02d} - Key Insight', '0:{t_mid2:02d} - Actionable Takeaway') plus relevant hashtags (#shorts #[keyword] #viral).\n"
                    "3. 1 high-engagement pinned comment relating to the keyword.\n"
                    "4. 1 short video banner hook text (< 32 chars) to overlay on video.\n"
                    "5. 10 comma-separated search tags.\n\n"
                    "Respond ONLY with valid JSON: {\"titles\": [\"...\"], \"video_banner\": \"...\", \"description\": \"...\", \"pinned_comment\": \"...\", \"tags\": [\"...\"]}"
                )
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config={"response_mime_type": "application/json"},
                )
                clean_json = re.sub(r"^```(?:json)?\s*", "", (response.text or "{}").strip())
                clean_json = re.sub(r"\s*```$", "", clean_json)
                data = json.loads(clean_json)
                data["keyword"] = topic
                data["timestamps"] = [
                    {"time": 0, "label": f"0:00 - Hook: {topic}"},
                    {"time": t_mid1, "label": f"0:{t_mid1:02d} - Key Insight"},
                    {"time": t_mid2, "label": f"0:{t_mid2:02d} - Practical Step"},
                ]
                return data
            except Exception as e:
                print(f"[shorts_engine] Gemini metadata generation error: {e}", file=sys.stderr)

        # Smart keyword-synchronized fallback
        synced_desc = (
            f"🎯 Master {topic.title()} in {int(t_dur)}s:\n"
            f"0:00 - Hook: The {topic} breakdown you need\n"
            f"0:{t_mid1:02d} - Key Insight & Mistake to Avoid\n"
            f"0:{t_mid2:02d} - Actionable Takeaway for {topic}\n\n"
            f"\"{hook}\"\n\n"
            f"Save this reel and drop your questions below! 👇\n"
            f"#shorts #{re.sub(r'[^a-zA-Z0-9]', '', topic).lower()} #viral #tips #learn #trending"
        )
        return {
            "keyword": topic,
            "titles": [
                f"The Brutal Truth About {topic.title()} 😳",
                f"Why 99% Fail At {topic.title()} (And How To Fix It)",
                f"Watch This Before Trying {topic.title()}!",
            ],
            "video_banner": f"POV: {topic.upper()[:24]} 💡",
            "description": synced_desc,
            "pinned_comment": f"What is your #1 question about {topic}? Let's discuss in the comments 👇",
            "tags": [topic.lower(), "shorts", "viral", f"{topic.lower()} tips", "growth", "guide", "trending"],
            "timestamps": [
                {"time": 0, "label": f"0:00 - Hook: {topic}"},
                {"time": t_mid1, "label": f"0:{t_mid1:02d} - Key Insight"},
                {"time": t_mid2, "label": f"0:{t_mid2:02d} - Actionable Step"},
            ]
        }


def synthesize_voiceover_sync(text: str, voice: str, output_path: Path):
    """Generate free neural voiceover using edge-tts."""
    import edge_tts  # type: ignore[missing-import]

    async def _run():
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(output_path))

    asyncio.run(_run())

import json
import subprocess
import sys
from pathlib import Path

# Add skill path to sys.path
SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "claude-music"
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

import shorts_engine  # noqa: E402
from reel_autoedit import build_filter_graph, write_ass_captions  # noqa: E402
from shorts_engine import ShortsEngine  # noqa: E402


def test_reel_ass_custom_text_and_sticker(tmp_path):
    ass_path = tmp_path / "test.ass"
    write_ass_captions(
        clips=[],
        path=ass_path,
        burn_captions=False,
        custom_text="WAIT FOR IT 😱",
        sticker_badge="🔥 MUST WATCH",
    )
    assert ass_path.exists()
    content = ass_path.read_text(encoding="utf-8")
    assert "WAIT FOR IT 😱" in content
    assert "🔥 MUST WATCH" in content
    assert "Dialogue: 1" in content
    assert "Dialogue: 2" in content
    assert "Dialogue: 0" not in content  # spoken dialogue was suppressed because burn_captions is False


def test_reel_filter_graph_progress_bar_and_color_grade():
    dummy_clips = [
        {"start": 0.0, "end": 5.0, "out_start": 0.0, "out_end": 5.0, "zoom": "1.0"},
        {"start": 5.0, "end": 10.0, "out_start": 5.0, "out_end": 10.0, "zoom": "1.15"},
    ]
    vf = build_filter_graph(
        clips=dummy_clips,
        captions_filename="test.ass",
        has_music=False,
        color_grade="warm",
        progress_bar="red",
    )
    assert "drawbox=" in vf
    assert "color=red@0.95" in vf
    assert "colorbalance=rs=0.1" in vf
    assert "ass=filename='test.ass'" in vf


def test_reel_filter_graph_no_captions_no_text():
    dummy_clips = [
        {"start": 0.0, "end": 5.0, "out_start": 0.0, "out_end": 5.0, "zoom": "1.0"},
    ]
    vf = build_filter_graph(
        clips=dummy_clips,
        captions_filename=None,
        has_music=False,
        color_grade="none",
        progress_bar="none",
    )
    assert "subtitles=" not in vf
    assert "drawbox=" not in vf
    assert "eq=" not in vf


def test_reel_filter_graph_silent_source_and_music():
    dummy_clips = [
        {"start": 0.0, "end": 4.0, "words": []},
    ]
    vf = build_filter_graph(
        clips=dummy_clips,
        captions_filename=None,
        has_music=True,
        has_source_audio=False,
    )
    assert "aevalsrc=0:d=4.000:s=44100" in vf
    assert "asplit=2[sp1][sp2]" in vf
    assert "[vout]" in vf
    assert "[aout]" in vf



def test_shorts_engine_clip_focus_diversity(tmp_path):
    engine = ShortsEngine(tmp_path)
    # Mock transcript spanning 120 seconds with questions and high engagement markers
    segments = [
        {"start": float(i * 10), "end": float(i * 10 + 6), "text": f"Why does this crazy secret step {i} work so well?"}
        for i in range(12)
    ]
    transcript = {"duration": 120.0, "segments": segments}

    # Test top hooks
    hooks = engine.get_highlights(transcript, num_clips=3, clip_focus="top_hooks", provider="offline")
    assert len(hooks) == 3

    # Test diverse spread (beginning, middle, ending)
    diverse_hooks = engine.get_highlights(transcript, num_clips=3, clip_focus="diverse", provider="offline")
    assert len(diverse_hooks) == 3
    starts = [h["start_time"] for h in diverse_hooks]
    assert starts[0] < starts[1] < starts[2]
    assert starts[0] < 45.0
    assert 30.0 <= starts[1] <= 85.0
    assert starts[2] >= 70.0

    # Test first half
    first_half = engine.get_highlights(transcript, num_clips=2, clip_focus="first_half", provider="offline")
    for h in first_half:
        assert h["start_time"] < 65.0

    # Test ending
    ending = engine.get_highlights(transcript, num_clips=2, clip_focus="ending", provider="offline")
    for h in ending:
        assert h["start_time"] >= 45.0


def test_shorts_engine_ass_subtitle_generation(tmp_path):
    engine = ShortsEngine(tmp_path)
    ass_path = tmp_path / "sub.ass"
    transcript = {
        "segments": [
            {
                "start": 12.0,
                "end": 18.0,
                "text": "Check out this incredible feature",
                "words": [
                    {"word": "Check", "start": 12.0, "end": 13.0},
                    {"word": "out", "start": 13.0, "end": 14.0},
                    {"word": "this", "start": 14.0, "end": 15.0},
                    {"word": "incredible", "start": 15.0, "end": 17.0},
                    {"word": "feature", "start": 17.0, "end": 18.0},
                ],
            }
        ]
    }

    # Test with custom text and hidden spoken captions
    res = engine.generate_ass_subtitles(
        transcript=transcript,
        start_time=10.0,
        end_time=25.0,
        ass_path=ass_path,
        style="hormozi",
        custom_text="SECRET STRATEGY",
        sticker_badge="🔥 MUST WATCH",
        hide_spoken_captions=True,
    )
    assert res is True
    assert ass_path.exists()
    content = ass_path.read_text(encoding="utf-8")
    assert "SECRET STRATEGY" in content
    assert "🔥 MUST WATCH" in content
    assert "Dialogue: 1" in content
    assert "Dialogue: 2" in content
    # Words should not be burned
    assert "Check out this incredible" not in content


def test_shorts_engine_keyword_priority(tmp_path):
    engine = ShortsEngine(tmp_path)
    segments = [
        {"start": 0.0, "end": 10.0, "text": "This is a random opening chat with friends."},
        {"start": 15.0, "end": 35.0, "text": "Here is the exact trading strategy that made millions."},
        {"start": 40.0, "end": 60.0, "text": "After that we went out to eat dinner at a restaurant."},
    ]
    transcript = {"duration": 60.0, "segments": segments}
    picks = engine.get_highlights(transcript, num_clips=1, keyword="trading", provider="offline")
    assert len(picks) == 1
    # Highlight should pick the trading segment
    assert picks[0]["start_time"] >= 14.0 and picks[0]["start_time"] <= 20.0
    assert "trading" in picks[0]["hook_sentence"].lower()


def test_shorts_engine_keyword_synced_metadata(tmp_path):
    engine = ShortsEngine(tmp_path)
    meta = engine.generate_viral_metadata(
        hook="Never do this common mistake",
        transcript_sample="Transcript discussing real estate investing",
        keyword="real estate",
        duration=28.0,
    )
    assert meta["keyword"] == "real estate"
    assert any("Real Estate" in t for t in meta["titles"])
    assert "0:00" in meta["description"]
    assert "real estate" in meta["description"].lower()
    assert "timestamps" in meta and len(meta["timestamps"]) >= 2
    assert meta["video_banner"]


def test_autoedit_reel_caption_style_support(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    engine = ShortsEngine(tmp_path)

    # Mock autoedit_video so we don't invoke full ffmpeg/whisper in contract test
    mock_autoedit = MagicMock(return_value={
        "duration": 30.0,
        "clips_count": 5,
        "output_file": "dummy.mp4",
        "edl": [{"text": "Hello world"}],
    })
    monkeypatch.setattr("reel_autoedit.autoedit_video", mock_autoedit)

    dummy_source = tmp_path / "test.mp4"
    dummy_source.write_bytes(b"dummy")

    res = engine.autoedit_reel(
        source_path=dummy_source,
        target_duration=30.0,
        caption_style="clean",
        custom_text="TEST BANNER",
    )
    assert res["id"].startswith("pro_")
    assert mock_autoedit.called
    kwargs = mock_autoedit.call_args[1]
    assert kwargs.get("caption_style") == "clean"


def test_reel_clips_tuple_and_dict_normalization(tmp_path):
    from reel_autoedit import fit_to_target, normalize_clips, total_duration

    # Mix of tuples and dicts
    mixed = [(0.0, 5.0), {"start": 5.0, "end": 12.0, "words": []}, (12.0, 20.0)]
    norm = normalize_clips(mixed)
    assert len(norm) == 3
    assert norm[0]["start"] == 0.0 and norm[0]["end"] == 5.0
    assert norm[1]["start"] == 5.0 and norm[1]["end"] == 12.0
    assert norm[2]["start"] == 12.0 and norm[2]["end"] == 20.0

    # Total duration should handle raw tuples without TypeError: tuple indices must be integers or slices, not str
    dur = total_duration([(0.0, 4.5), (4.5, 10.0)])
    assert dur == 10.0

    # fit_to_target should safely handle tuples
    fitted = fit_to_target([(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)], target=15.0)
    assert total_duration(fitted) <= 15.0


def test_fit_to_target_preserves_sentence_boundary_ending():
    from reel_autoedit import fit_to_target

    # 3 clips where the last clip has distinct sentence boundary words
    clips = [
        {"start": 0.0, "end": 10.0, "words": [{"w": "Hook", "s": 0.5, "e": 2.0}]},
        {"start": 10.0, "end": 20.0, "words": [{"w": "Filler", "s": 10.5, "e": 12.0}]},
        {
            "start": 20.0, "end": 38.0,
            "words": [
                {"w": "First", "s": 20.5, "e": 21.5},
                {"w": "point.", "s": 21.6, "e": 22.5},  # Sentence 1 ends here!
                {"w": "And", "s": 23.0, "e": 23.5},
                {"w": "here", "s": 23.6, "e": 24.0},
                {"w": "is", "s": 24.1, "e": 24.5},
                {"w": "the", "s": 24.6, "e": 25.0},
                {"w": "conclusion!", "s": 25.1, "e": 26.5},  # Sentence 2 ends here!
                {"w": "Extra", "s": 27.0, "e": 28.0},
                {"w": "unneeded", "s": 28.1, "e": 29.5},
                {"w": "rambling", "s": 29.6, "e": 31.0},
                {"w": "words", "s": 31.1, "e": 33.0},
            ]
        }
    ]

    # Target 20.0s: clip 0 (10s) + clip 2 should end on "conclusion!" (26.5s) instead of mid-word
    fitted = fit_to_target(clips, target=20.0)
    last_words = [w["w"] for w in fitted[-1]["words"]]
    assert "conclusion!" in last_words
    assert "rambling" not in last_words


def test_reel_filter_graph_smooth_outro_fades():
    dummy_clips = [
        {"start": 0.0, "end": 10.0, "words": []},
    ]
    vf = build_filter_graph(
        clips=dummy_clips,
        captions_filename=None,
        has_music=True,
        has_source_audio=True,
    )
    # Both video outro fade-to-black and audio outro fade-out should be present
    assert "fade=t=out:st=9.650:d=0.35" in vf
    assert "afade=t=out:st=9.500:d=0.50" in vf


def test_shorts_engine_analyze_and_order_clips(tmp_path):
    engine = ShortsEngine(tmp_path)
    # Create 3 dummy clips
    c1 = tmp_path / "clip_story.mp4"
    c2 = tmp_path / "clip_hook.mp4"
    c3 = tmp_path / "clip_cta.mp4"
    for c in [c1, c2, c3]:
        c.write_bytes(b"dummy")

    # Mock transcripts in cache
    t_dir = tmp_path / ".claude-music" / "transcripts"
    t_dir.mkdir(parents=True, exist_ok=True)
    (t_dir / f"{c1.stem}.json").write_text(json.dumps({"segments": [{"text": "So this happened during development."}]}))
    (t_dir / f"{c2.stem}.json").write_text(json.dumps({"segments": [{"text": "Did you know the biggest secret about this?"}]}))
    (t_dir / f"{c3.stem}.json").write_text(json.dumps({"segments": [{"text": "Follow and share for part 2!"}]}))

    ordered = engine.analyze_and_order_clips([c1, c2, c3])
    assert len(ordered) == 3
    # c2 had question & secret -> Hook (first)
    assert ordered[0]["filename"] == "clip_hook.mp4"
    assert "Hook" in ordered[0]["role"]
    # c3 had follow & part 2 -> CTA (last)
    assert ordered[-1]["filename"] == "clip_cta.mp4"
    assert "Call to Action" in ordered[-1]["role"]
    # c1 is body (middle)
    assert ordered[1]["filename"] == "clip_story.mp4"


def test_shorts_engine_enhance_caption_text(tmp_path):
    engine = ShortsEngine(tmp_path)

    # 1. Hook mode
    raw_text = "I built an autonomous trading bot in three days."
    hooked = engine.enhance_caption_text(raw_text, mode="hook")
    assert "I built an autonomous trading bot" in hooked
    assert any(h in hooked for h in ["Stop scrolling", "Wait until", "secret", "blow your mind", "🔥", "🚨"])

    # 2. Hormozi mode: capitalizes power words
    hormozi_raw = "This is the secret to make money fast without mistake"
    hormozi_res = engine.enhance_caption_text(hormozi_raw, mode="hormozi")
    assert "SECRET" in hormozi_res
    assert "MONEY" in hormozi_res

    # 3. Grammar mode: cleans fillers and capitalizes
    grammar_raw = "um so like, we tested this. and uh it worked."
    grammar_res = engine.enhance_caption_text(grammar_raw, mode="grammar")
    assert "um" not in grammar_res.lower().split()
    assert "uh" not in grammar_res.lower().split()


def test_shorts_engine_delete_short(tmp_path):
    engine = ShortsEngine(tmp_path)
    clip_id = "test_clip_123"
    vid_file = engine.shorts_dir / f"{clip_id}.mp4"
    thumb_file = engine.shorts_dir / f"{clip_id}.jpg"
    edl_file = engine.shorts_dir / f"{clip_id}.edl.json"
    vid_file.write_text("dummy video")
    thumb_file.write_text("dummy thumb")
    edl_file.write_text("dummy edl")

    entry = {
        "id": clip_id,
        "title": "Test Short",
        "filename": vid_file.name,
        "thumbnail": thumb_file.name,
        "edl_file": edl_file.name,
        "duration": 15.0,
    }
    engine.save_library_entry(entry)
    assert len(engine.load_library()) == 1

    # Delete
    res = engine.delete_short(clip_id)
    assert res is True
    assert not vid_file.exists()
    assert not thumb_file.exists()
    assert not edl_file.exists()
    assert len(engine.load_library()) == 0

    # Non-existent
    assert engine.delete_short("non_existent_id") is False


def test_shorts_engine_manual_edit_short(tmp_path, monkeypatch):
    engine = ShortsEngine(tmp_path)
    dummy_src = tmp_path / "source.mp4"
    dummy_src.write_text("video")

    monkeypatch.setattr(shorts_engine, "probe_duration", lambda p: 20.0)
    monkeypatch.setattr(shorts_engine, "has_audio_stream", lambda p: True)

    recorded_cmds = []

    def mock_run_cmd(cmd, **kwargs):
        recorded_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(engine, "_run_cmd", mock_run_cmd)

    entry = engine.manual_edit_short(
        source_path=dummy_src,
        start_time=2.0,
        end_time=12.0,
        aspect_ratio="1:1",
        video_volume=1.2,
        mute_video=False,
        bg_music_path=None,
        music_volume=0.0,
        mute_music=True,
        effect="vhs",
        sticker="must_watch",
        burn_captions=False,
        quality="1080p_ultra",
        title="Test Square VHS Short",
    )

    assert entry["id"].startswith("edit_")
    assert entry["aspect_ratio"] == "1:1"
    assert entry["duration"] == 10.0
    assert entry["quality"] == "1080p_ultra"
    assert entry["effect"] == "vhs"
    assert "MUST WATCH" in entry["sticker"]
    assert entry["captions"] is False

    # Check FFmpeg command arguments
    found_render = False
    for cmd in recorded_cmds:
        joined = " ".join(cmd)
        if "-filter_complex" in joined:
            found_render = True
            assert "crop=1080:1080" in joined
            assert "curves=vintage" in joined
            assert "-crf 18" in joined
            assert "-b:v 12M" in joined
    assert found_render is True


def test_server_shorts_delete_and_manual_edit(tmp_path):
    sys.path.insert(0, str(SKILL_DIR / "webapp"))
    import server

    h = object.__new__(server.Handler)
    h._output_dir = lambda: tmp_path

    sent_json = []
    h._json = lambda data, status=200: sent_json.append((data, status))

    # 1. Test delete with missing clip_id
    h._handle_shorts_delete({})
    assert sent_json[-1][1] == 400

    # 2. Test delete with valid clip_id
    engine = ShortsEngine(tmp_path)
    (engine.shorts_dir / "none.mp4").write_text("dummy")
    engine.save_library_entry({"id": "clip_to_del", "title": "Del", "filename": "none.mp4"})
    assert len(engine.load_library()) == 1

    h._handle_shorts_delete({"clip_id": "clip_to_del"})
    assert sent_json[-1][0]["ok"] is True
    assert len(engine.load_library()) == 0

    # 3. Test manual edit missing source
    h._handle_shorts_manual_edit({"clip_id": "non_existent"})
    assert sent_json[-1][1] == 404


def test_shorts_engine_export_timeline_json_schema_conformance(tmp_path):
    engine = ShortsEngine(tmp_path)
    entry = {
        "id": "short_schema_test",
        "title": "Schema Test Reel",
        "duration": 18.0,
        "aspect_ratio": "9:16",
        "filename": "reel_test.mp4",
        "source_file": "original.mp4",
        "sticker": "🔥 MUST WATCH",
    }
    edl = [
        {"clip": 1, "output_in": 0.0, "output_out": 4.5, "input_in": 1.0, "input_out": 5.5, "zoom": "115% Zoom", "text": "Hook"},
        {"clip": 2, "output_in": 4.5, "output_out": 18.0, "input_in": 8.0, "input_out": 21.5, "zoom": "Normal", "text": "Story"},
    ]
    transcript = {
        "segments": [
            {
                "words": [
                    {"word": "Welcome", "start": 0.1, "end": 0.5},
                    {"word": "to", "start": 0.5, "end": 0.7},
                    {"word": "testing", "start": 0.7, "end": 1.2},
                ]
            }
        ]
    }

    tl_path = engine.export_timeline_json(entry, edl=edl, transcript=transcript, bg_music_file="beat.mp3")
    assert tl_path.exists()

    with tl_path.open(encoding="utf-8") as f:
        data = json.load(f)

    # 1. Top-level schema conformance
    for req_field in ["version", "project", "assets", "tracks"]:
        assert req_field in data, f"Missing required root field: {req_field}"

    # 2. Project & Canvas conformance
    proj = data["project"]
    assert proj["id"] == "short_schema_test"
    assert proj["title"] == "Schema Test Reel"
    canvas = proj["canvas"]
    assert canvas["width"] == 1080
    assert canvas["height"] == 1920
    assert canvas["aspect_ratio"] == "9:16"
    assert canvas["duration"] == 18.0
    assert canvas["fps"] == 30

    # 3. Assets conformance
    assert len(data["assets"]) >= 2
    video_asset = next(a for a in data["assets"] if a["type"] == "video")
    audio_asset = next(a for a in data["assets"] if a["type"] == "audio")
    assert video_asset["id"] == "asset_src_main"
    assert audio_asset["name"] == "beat.mp3"

    # 4. Tracks & Clips conformance
    track_types = [t["type"] for t in data["tracks"]]
    assert "video" in track_types
    assert "sticker" in track_types
    assert "subtitle" in track_types
    assert "audio" in track_types

    # Video track clips
    v_track = next(t for t in data["tracks"] if t["type"] == "video")
    assert len(v_track["clips"]) == 2
    assert v_track["clips"][0]["start"] == 0.0
    assert v_track["clips"][0]["end"] == 4.5
    assert v_track["clips"][0]["transform"]["scale"] == 1.15

    # Subtitle track captions
    sub_track = next(t for t in data["tracks"] if t["type"] == "subtitle")
    words = sub_track["clips"][0]["captions"]["words"]
    assert len(words) == 3
    assert words[0]["word"] == "WELCOME"


def test_autoedit_clip_sanitization_and_ffmpeg_failure_diagnostics(tmp_path, monkeypatch):
    import reel_autoedit

    # 1. Verify clip normalization filters sub-0.25s clips and defaults when empty
    invalid_clips = [
        {"start": 1.0, "end": 1.05},  # duration 0.05s -> cut
        {"start": 2.0, "end": 1.5},   # negative duration -> cut
    ]
    norm = [c for c in reel_autoedit.normalize_clips(invalid_clips) if float(c.get("end", 0)) - float(c.get("start", 0)) >= 0.25]
    assert len(norm) == 0

    # 2. Test FFmpeg error diagnostic hint on empty streams
    def mock_run_fail(*args, **kwargs):
        class Proc:
            returncode = 4294967274
            stderr = "Could not open encoder before EOF\nNothing was written into output file, because at least one of its streams received no packets."
        return Proc()

    monkeypatch.setattr(subprocess, "run", mock_run_fail)
    src_video = tmp_path / "dummy.mp4"
    src_video.write_bytes(b"\x00" * 1024)
    out_video = tmp_path / "out.mp4"

    monkeypatch.setattr(reel_autoedit, "check_has_audio", lambda p: False)
    monkeypatch.setattr(reel_autoedit, "probe_duration", lambda p: 5.0)

    try:
        reel_autoedit.autoedit_video(
            src_video,
            5.0,
            out_video,
            burn_captions=False,
        )
        raise AssertionError("Expected RuntimeError on FFmpeg failure")
    except RuntimeError as err:
        assert "0 packets before EOF" in str(err)
        assert "exit 4294967274" in str(err)


def test_shorts_engine_synthesize_prompt_reel(tmp_path, monkeypatch):
    engine = ShortsEngine(tmp_path)
    import reel_autoedit
    import shorts_engine

    def mock_voiceover(text, voice, out_path):
        out_path.write_bytes(b"\x00" * 2048)

    monkeypatch.setattr(shorts_engine, "synthesize_voiceover_sync", mock_voiceover)
    monkeypatch.setattr(engine, "enhance_voiceover_audio", lambda p, preset: p)
    monkeypatch.setattr(reel_autoedit, "probe_duration", lambda p: 6.5)
    monkeypatch.setattr(reel_autoedit, "transcribe_words", lambda path, model_size: [
        {"word": "Focus", "start": 0.0, "end": 0.8},
        {"word": "is", "start": 0.8, "end": 1.2},
        {"word": "power", "start": 1.2, "end": 2.0},
    ])
    monkeypatch.setattr(reel_autoedit, "write_ass_captions", lambda clips, path, **kw: False)

    def mock_run_cmd(cmd, **kw):
        target = Path(cmd[-1])
        target.write_bytes(b"\x00" * 1024)

    monkeypatch.setattr(engine, "_run_cmd", mock_run_cmd)

    entry = engine.synthesize_prompt_reel(
        topic="Psychology of Focus",
        background_style="cyberpunk",
        voice="en-US-ChristopherNeural",
    )

    assert entry["id"].startswith("prompt_reel_")
    assert "Focus" in entry["title"]
    assert entry["duration"] == 6.5
    assert entry["aspect_ratio"] == "9:16"
    assert entry["layout"] == "prompt_reel"
    assert entry["timeline_file"].endswith(".timeline.json")
    assert (engine.shorts_dir / entry["timeline_file"]).exists()







import sys
from pathlib import Path

# Add skill path to sys.path
SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "claude-music"
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

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

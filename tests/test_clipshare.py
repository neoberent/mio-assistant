"""Clip sharing: what gets re-encoded, how it's signed, which old clips go, and "upload the clip"."""
import asyncio
import datetime as dt
from pathlib import Path

import pytest

from assistant import clips, clipshare
from assistant.brain import fastpath
from assistant.clipshare import ClipInfo, plan_for
from assistant.tools import ToolBox

CAP, MAX = 120_000_000, 32000


def info(seconds=30.0, size=197_000_000, codec="h264", pix="yuv420p", audio="aac"):
    return ClipInfo(seconds, size, 1920, 1080, 60, codec, pix, audio)


def test_a_clip_that_fits_is_not_re_encoded():
    plan = plan_for(info(15, 97_000_000), CAP, MAX)
    assert plan.remux and plan.size == 97_000_000


def test_a_30_second_clip_gets_the_tested_bitrate_and_stays_under_the_cap():
    plan = plan_for(info(), CAP, MAX)
    assert not plan.remux and 30000 <= plan.video_kbps <= MAX
    assert plan.size <= CAP


def test_a_short_big_clip_never_goes_above_the_highest_bitrate():
    assert plan_for(info(20, 125_000_000), CAP, MAX).video_kbps == MAX


def test_a_long_clip_gets_a_lower_bitrate_and_says_so():
    plan = plan_for(info(180, 1_100_000_000), CAP, MAX)
    assert plan.size <= CAP and plan.video_kbps < 8000
    assert "lower quality" in clipshare.describe(info(180, 1_100_000_000), plan)


@pytest.mark.parametrize("clip", [info(10, 50_000_000, codec="hevc"), info(10, 50_000_000, pix="yuv444p"), info(10, 50_000_000, audio="opus")])
def test_what_discord_may_not_play_is_re_encoded_even_if_small(clip):
    assert not plan_for(clip, CAP, MAX).remux


def test_no_sound_means_no_audio_in_the_file():
    plan = plan_for(info(), CAP, MAX, audio=False)
    args = clipshare.encode_args(Path("a"), Path("b"), plan, False, True)
    assert "-an" in args and plan.audio_kbps == 0


def test_remux_copies_and_moves_the_index_to_the_front():
    args = clipshare.encode_args(Path("a"), Path("b"), plan_for(info(15, 97_000_000), CAP, MAX), True, True)
    assert args[args.index("-c:v") + 1] == "copy" and "+faststart" in args


def test_signature_matches_awss_published_example():
    """From the AWS 'Signature Calculations' docs: GET Object example."""
    key = "/test.txt"
    empty = clipshare.EMPTY_SHA
    headers = {"Host": "examplebucket.s3.amazonaws.com", "Range": "bytes=0-9", "x-amz-content-sha256": empty,
               "x-amz-date": "20130524T000000Z"}
    auth = clipshare.sigv4("GET", "examplebucket.s3.amazonaws.com", key, {}, headers, empty, "AKIAIOSFODNN7EXAMPLE",
                           "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1", dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc))
    assert auth.endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_oldest_clips_go_first_and_only_as_many_as_needed():
    stored = [("clips/new.mp4", 40, "2026-11-03"), ("clips/new.jpg", 1, "2026-11-03"), ("clips/old.mp4", 40, "2026-11-01"),
              ("clips/old.json", 1, "2026-11-01"), ("clips/mid.mp4", 40, "2026-11-02")]
    assert clipshare.oldest_to_remove(stored, 30, 200) == []
    assert clipshare.oldest_to_remove(stored, 30, 130) == ["old"]
    assert clipshare.oldest_to_remove(stored, 30, 90) == ["old", "mid"]


@pytest.mark.parametrize("name,title", [("Roblox 2026-11-04 19-22-36.mp4", "Roblox"), ("Elden Ring 2026-01-02 03-04-05.mp4", "Elden Ring"),
                                        ("2026-01-02 03-04-05.mp4", "Clip"), ("my clip.mp4", "my clip")])
def test_default_title_is_the_game(name, title):
    assert clipshare.default_title(Path(name)) == title


def test_title_is_cleaned_and_falls_back():
    assert clipshare.clean_title(" nice\n  win\x00 ", "x") == "nice win"
    assert clipshare.clean_title("   ", "Roblox") == "Roblox"
    assert len(clipshare.clean_title("a" * 500, "x")) == 100


def test_missing_keys_are_named_without_their_values(tmp_path, monkeypatch):
    env = tmp_path / "clip-share.env"
    env.write_text("# comment\nB2_BUCKET=b\nB2_WRITE_KEY=topsecret\n")
    monkeypatch.setattr(clipshare, "ENV_FILE", env)
    with pytest.raises(clipshare.ShareError) as e:
        clipshare.load_settings()
    assert "B2_ENDPOINT" in str(e.value) and "topsecret" not in str(e.value)


def test_the_newest_clip_is_picked_by_time(tmp_path):
    import os
    assert clips.latest_clip(tmp_path) is None
    for n, t in (("b.mp4", 100), ("a.mp4", 300), ("c.txt", 900)):
        (tmp_path / n).write_text("x")
        os.utime(tmp_path / n, (t, t))
    assert clips.latest_clip(tmp_path).name == "a.mp4"


async def _no(*_):
    return False


def _tools(answer):
    box = ToolBox({"shell_allow": [], "shell_deny": []}, _no)
    box.called = []

    async def call(name, args, confirmed=False):
        box.called.append(name)
        return answer
    box.call = call
    return box


@pytest.mark.parametrize("said", ["upload the clip", "Upload that clip.", "Mio, upload my last clip", "share the clip", "please upload the latest clip",
                                  "post that clip to discord", "upload the clip I just made"])
def test_saying_it_opens_the_window(said):
    tools = _tools("opened the upload window for x.mp4")
    result = asyncio.run(fastpath.handle(said, tools))
    assert tools.called == ["upload_clip"] and "upload" in result.reply


@pytest.mark.parametrize("said", ["clip that", "what is a clip", "upload my homework"])
def test_other_requests_do_not_open_it(said):
    tools = _tools("saved the last 30 seconds to x")
    asyncio.run(fastpath.handle(said, tools))
    assert "upload_clip" not in tools.called


def test_the_tool_says_so_when_keys_are_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(clipshare, "ENV_FILE", tmp_path / "nope.env")
    box = ToolBox({"shell_allow": [], "shell_deny": []}, _no)
    box.clipper = clips.Clipper({"folder": str(tmp_path)})
    assert "isn't set up" in asyncio.run(box.call("upload_clip", {}))

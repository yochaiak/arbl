"""Recording finalization, scope, and masked local Chromium video."""

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_browser import chromium as chromium
from test_browser import fixture as fixture

from abrl import browser, recording


def ready_page():
    return SimpleNamespace(url="https://fixture.example/", on=lambda *_: None, remove_listener=lambda *_: None)


def test_recording_encoder_probe_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(recording.shutil, "which", lambda _: None)
    assert not recording.recording_encoder_available.__wrapped__()


async def test_capture_discards_image_if_the_validated_frame_changes():
    source = "https://fixture.example/"
    frame = SimpleNamespace(url=source, locator=lambda selector: selector)

    async def screenshot(**options):
        assert options["mask"] == [recording.MASKED_FIELDS, "#secret"]
        assert options["style"] == recording.MASK_STYLE
        frame.url = "https://outside.example/"
        return b"frame from different origin"

    page = SimpleNamespace(frames=[frame], screenshot=screenshot)
    assert not await recording._masked_frame(page, ["#secret"], lambda url: url == source)


async def test_recorder_publishes_completed_clip_before_attempt_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(recording.shutil, "which", lambda _: "ffmpeg")
    monkeypatch.setattr(recording, "FRAME_RATE", 20)
    monkeypatch.setattr(recording, "CLIP_SECONDS", 0.1)

    async def encode(input_file: Path, output_file: Path) -> bool:
        output_file.write_bytes(input_file.read_bytes())
        return True

    async def screenshot(*_args):
        return b"sample frame"

    monkeypatch.setattr(recording, "_encode", encode)
    monkeypatch.setattr(recording, "_masked_frame", screenshot)
    first_clip = asyncio.Event()
    stored = []

    async def store(segment):
        assert segment.path.read_bytes().startswith(b"sample frame")
        stored.append(segment)
        first_clip.set()

    recorder = recording.SessionRecorder(tmp_path, store, None)
    recorder.start(ready_page(), [], lambda _: True)
    await asyncio.wait_for(first_clip.wait(), 2)
    assert not recorder.stopping.is_set()
    assert stored[0].sequence == 0
    assert await recorder.finish() == "ready"
    assert not tuple(tmp_path.iterdir())


async def test_recording_storage_failure_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(recording.shutil, "which", lambda _: "ffmpeg")
    started = asyncio.Event()

    async def screenshot(*_args):
        started.set()
        return b"sample frame"

    async def encode(input_file, output_file):
        output_file.write_bytes(input_file.read_bytes())
        return True

    async def store(_segment):
        raise OSError("storage unavailable")

    monkeypatch.setattr(recording, "_masked_frame", screenshot)
    monkeypatch.setattr(recording, "_encode", encode)
    recorder = recording.SessionRecorder(tmp_path, store, None)
    recorder.start(ready_page(), [], lambda _: True)
    await asyncio.wait_for(started.wait(), 2)
    assert await recorder.finish() == "unavailable"
    assert not tuple(tmp_path.iterdir())


async def test_first_frame_wait_preserves_assignment_cleanup_allowance():
    async def wait_first_frame():
        await asyncio.sleep(0.06)

    execution = browser.Execution("https://fixture.example", set())
    execution.recording = SimpleNamespace(waiting_for_first_frame=True, wait_first_frame=wait_first_frame)
    async with asyncio.timeout(0.035) as deadline:
        await browser._wait_for_first_recording_frame(execution, deadline)
        assert not deadline.expired()
        assert execution.recording_wait_seconds >= 0.06


@pytest.mark.integration
@pytest.mark.parametrize(("cancel_when_ready", "post_wait_ms"), [(False, 0), (False, 1400), (True, 1400)])
async def test_local_chromium_records_masked_viewport(chromium, fixture, tmp_path, cancel_when_ready, post_wait_ms):
    if not recording.recording_encoder_available():
        pytest.skip("FFmpeg H.264 encoder is unavailable")
    origin, _, _ = fixture
    clips = []
    ready = asyncio.Event()

    async def cancel():
        return cancel_when_ready and ready.is_set()

    async def store(segment):
        sample = tmp_path / f"saved-{segment.sequence}.mp4"
        sample.write_bytes(segment.path.read_bytes())
        clips.append(sample)

    async def emit(event):
        if cancel_when_ready and event.get("kind") == "action_started":
            asyncio.get_running_loop().call_later(0.9, ready.set)

    result = await browser.execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=[{"action": "navigate", "url": "/lab?recording", "post_navigation_wait_ms": post_wait_ms}],
        cancel=cancel,
        emit=emit,
        recording_directory=tmp_path / "scratch",
        store_recording_segment=store,
    )
    assert result.outcome == ("cancelled" if cancel_when_ready else "completed"), result
    assert result.metadata["recording_status"] == "ready"
    assert len(clips) == 1 and clips[0].stat().st_size < recording.MAX_CLIP_BYTES
    frames = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(clips[0]), "-vf", "scale=32:18",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True, check=True, timeout=5,
    ).stdout
    pixels = list(zip(frames[::3], frames[1::3], frames[2::3], strict=True))
    assert any(green > 200 and red < 50 and blue < 50 for red, green, blue in pixels)
    assert not any(red > 200 and green < 40 and blue < 40 for red, green, blue in pixels)
    assert not tuple((tmp_path / "scratch").iterdir())

"""Record masked browser viewports as bounded, independently playable clips."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page

FRAME_RATE = 2
CLIP_SECONDS = 30
MAX_FRAME_BYTES = 256 * 1024
MAX_CLIP_BYTES = 8 * 1024**2
MAX_ATTEMPT_BYTES = 256 * 1024**2
ENCODE_TIMEOUT_SECONDS = 10
FINALIZE_TIMEOUT_SECONDS = 20
FIRST_FRAME_TIMEOUT_SECONDS = 0.7
EMPTY_FRAME_URLS = {"", "about:blank", "about:srcdoc"}
MASKED_FIELDS = 'input, textarea, [contenteditable]:not([contenteditable="false"]), [data-abrl-secret]'
MASK_STYLE = f"{MASKED_FIELDS} {{ opacity: 0 !important }}"

RecordingStatus = Literal["ready", "partial", "unavailable"]


@dataclass(frozen=True, slots=True)
class RecordingSegment:
    path: Path
    sequence: int
    start_ms: int
    duration_ms: int


@dataclass(slots=True)
class CapturedFrames:
    path: Path
    sequence: int
    start_ms: int
    count: int = 0


@lru_cache(maxsize=1)
def recording_encoder_available() -> bool:
    """Check the worker's local FFmpeg build without presenting its output."""
    encoder = shutil.which("ffmpeg")
    if not encoder:
        return False
    try:
        result = subprocess.run(
            [encoder, "-hide_banner", "-encoders"], capture_output=True, timeout=3, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and b" libx264 " in result.stdout


async def _encode(frames: Path, destination: Path) -> bool:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "image2pipe", "-framerate", str(FRAME_RATE), "-vcodec", "mjpeg", "-i", str(frames),
        "-an", "-vf", "scale=854:480:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=854:480:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-threads", "1",
        "-b:v", "96k", "-maxrate", "128k", "-bufsize", "128k", "-movflags", "+faststart",
        "-fs", str(MAX_CLIP_BYTES), str(destination),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(ENCODE_TIMEOUT_SECONDS):
            return await process.wait() == 0
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def _in_scope(frames: tuple[tuple[Frame, str], ...], permits: Callable[[str], bool]) -> bool:
    return all(url in EMPTY_FRAME_URLS or permits(url) for _, url in frames)


async def _masked_frame(page: Page, secret_selectors: list[str], permits: Callable[[str], bool]) -> bytes:
    # A document or frame switch can race the screenshot. Discard the image if
    # the validated set changed while the masks were being applied.
    frames = tuple((frame, frame.url) for frame in page.frames)
    if not _in_scope(frames, permits):
        return b""
    masks = [
        frame.locator(selector)
        for frame, _ in frames
        for selector in (MASKED_FIELDS, *secret_selectors)
    ]
    image = await page.screenshot(
        type="jpeg", quality=45, scale="css", mask=masks, style=MASK_STYLE, timeout=650
    )
    if tuple((frame, frame.url) for frame in page.frames) != frames or not _in_scope(frames, permits):
        return b""
    return image if len(image) <= MAX_FRAME_BYTES else b""


class SessionRecorder:
    def __init__(
        self,
        directory: Path,
        store: Callable[[RecordingSegment], Awaitable[None]],
        started: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self.directory = directory
        self.store = store
        self.started = started
        self.stopping = asyncio.Event()
        self.document_changed = asyncio.Event()
        self.first_frame = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.incomplete = False
        self.published_bytes = 0
        self.page: Page | None = None

    def start(self, page: Page, secret_selectors: list[str], permits: Callable[[str], bool]) -> None:
        self.page = page
        self.task = asyncio.create_task(self._run(page, secret_selectors, permits))

    def select_page(self, page: Page) -> None:
        self.page = page
        self.document_changed.set()

    def stop(self) -> None:
        self.stopping.set()
        self.document_changed.set()

    @property
    def waiting_for_first_frame(self) -> bool:
        return self.task is not None and not self.task.done() and not self.first_frame.is_set()

    async def wait_first_frame(self) -> None:
        with suppress(TimeoutError):
            async with asyncio.timeout(FIRST_FRAME_TIMEOUT_SECONDS):
                await self.first_frame.wait()

    async def finish(self) -> RecordingStatus:
        self.stop()
        if self.task is not None:
            try:
                async with asyncio.timeout(FINALIZE_TIMEOUT_SECONDS):
                    await self.task
            except Exception:
                self.incomplete = True
        if self.published_bytes == 0:
            return "unavailable"
        return "partial" if self.incomplete else "ready"

    async def _notify_started(self) -> None:
        if self.started:
            # Recording status is optional and must never delay the first frame.
            with suppress(Exception):
                async with asyncio.timeout(3):
                    await self.started()

    async def _run(self, page: Page, secret_selectors: list[str], permits: Callable[[str], bool]) -> None:
        # The service readiness check confirms the codec. Keeping startup here
        # independent of that subprocess lets even a short attempt take a frame.
        if not shutil.which("ffmpeg"):
            return
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="capture-", dir=self.directory) as folder:
            directory = Path(folder)
            queue: asyncio.Queue[CapturedFrames | None] = asyncio.Queue(maxsize=1)
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._notify_started())
                tasks.create_task(self._publish(queue, directory))
                await self._sample(page, secret_selectors, permits, queue, directory)
                await queue.put(None)

    async def _sample(
        self,
        page: Page,
        secret_selectors: list[str],
        permits: Callable[[str], bool],
        queue: asyncio.Queue[CapturedFrames | None],
        directory: Path,
    ) -> None:
        start = monotonic()

        # The blank tab should not consume the first sampling interval. Start
        # taking images promptly when the first document becomes available.
        while not self.stopping.is_set():
            selected = self.page or page
            if selected.url not in EMPTY_FRAME_URLS:
                break
            self.document_changed.clear()

            def document_changed(frame: Frame) -> None:
                if frame == selected.main_frame:
                    self.document_changed.set()

            selected.on("framenavigated", document_changed)
            try:
                await self.document_changed.wait()
            finally:
                selected.remove_listener("framenavigated", document_changed)

        clip = None
        missed = 0
        while not self.stopping.is_set():
            captured_at = monotonic()
            elapsed = captured_at - start
            sequence = int(elapsed / CLIP_SECONDS)
            if clip and clip.sequence != sequence:
                if queue.full():
                    clip.path.unlink()
                    self.incomplete = True
                else:
                    queue.put_nowait(clip)
                clip = None

            try:
                image = await _masked_frame(self.page or page, secret_selectors, permits)
            except (PlaywrightError, ValueError):
                image = b""
            if image:
                if clip is None:
                    clip = CapturedFrames(directory / f"{sequence}.mjpeg", sequence, round(elapsed * 1000))
                with clip.path.open("ab") as stream:
                    stream.write(image)
                clip.count += 1
                missed = 0
                self.first_frame.set()
            elif not self.stopping.is_set():
                # One attempted frame can legitimately race a navigation. A
                # sustained gap or a dropped clip makes the result partial.
                missed += 1
                self.incomplete |= missed >= 3

            delay = max(0.0, captured_at + 1 / FRAME_RATE - monotonic())
            with suppress(TimeoutError):
                async with asyncio.timeout(delay):
                    await self.stopping.wait()
        if clip:
            await queue.put(clip)

    async def _publish(self, queue: asyncio.Queue[CapturedFrames | None], directory: Path) -> None:
        while (clip := await queue.get()) is not None:
            path = directory / f"{clip.sequence}.mp4"
            try:
                if self.published_bytes >= MAX_ATTEMPT_BYTES or not await _encode(clip.path, path):
                    self.incomplete = True
                    continue
                size = path.stat().st_size
                if not 0 < size < MAX_CLIP_BYTES or self.published_bytes + size > MAX_ATTEMPT_BYTES:
                    self.incomplete = True
                    continue
                await self.store(
                    RecordingSegment(path, clip.sequence, clip.start_ms, round(clip.count * 1000 / FRAME_RATE))
                )
                self.published_bytes += size
            except Exception:
                self.incomplete = True
            finally:
                path.unlink(missing_ok=True)
                clip.path.unlink(missing_ok=True)

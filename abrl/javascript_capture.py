"""Bounded JavaScript response and execution evidence from Chromium CDP."""

import asyncio
import base64
import hashlib
import io
import json
import re
from collections import Counter
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from itertools import count
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

from playwright.async_api import BrowserContext, CDPSession, Error, Page, Response

MAX_ARCHIVE_BYTES = 8 * 1024**2
MAX_SOURCE_BYTES = 4 * 1024**2
# The remote relay limits each JSON frame to 16 MiB. UTF-8 text can expand
# through JSON escaping, and related targets add nested JSON envelopes.
MAX_CDP_TEXT_BYTES = 2 * 1024**2
MAX_TOTAL_SOURCE_BYTES = 6 * 1024**2
MAX_RECORDS = 1000
MAX_TARGETS = 16
MAX_TARGET_DEPTH = 3
MAX_PENDING_READS = 32
MAX_URL_LENGTH = 512
CDP_TIMEOUT = 1.5
STOP_TIMEOUT = 3
COVERAGE_INTERVAL = 0.35

CHILD_TYPES = ("iframe", "worker", "service_worker", "shared_worker")
AUTO_ATTACH = {
    "autoAttach": True,
    "waitForDebuggerOnStart": True,
    "flatten": False,
    "filter": [{"type": kind} for kind in CHILD_TYPES] + [{"exclude": True}],
}
JAVASCRIPT_MIME = re.compile(r"(?:java|ecma)script", re.IGNORECASE)
JAVASCRIPT_SUFFIXES = (".js", ".mjs", ".cjs")


def _scheme(url: str) -> str:
    try:
        return urlsplit(url).scheme
    except ValueError:
        return ""


def _safe_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            authority = parsed.netloc.rsplit("@", 1)[-1]
            return f"{parsed.scheme}://{authority}{parsed.path}"[:MAX_URL_LENGTH]
        if parsed.scheme in {"blob", "data"}:
            return f"{parsed.scheme}:"
    except ValueError:
        pass
    return None


def _javascript_url(url: str) -> bool:
    try:
        return urlsplit(url).path.lower().endswith(JAVASCRIPT_SUFFIXES)
    except ValueError:
        return False


def _identity_length(size: str | None, encoding: str | None) -> int | None:
    if encoding and encoding.lower() != "identity":
        return None
    if size is not None and len(size) <= 20 and size.isascii() and size.isdecimal():
        return int(size)
    return None


@dataclass
class _Target:
    number: int
    path: tuple[str, ...]
    kind: str
    url: str
    frames: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    scripts: dict[str, dict] = field(default_factory=dict)
    responses: dict[str, dict] = field(default_factory=dict)
    response_lengths: dict[str, tuple[int, int | None]] = field(default_factory=dict)
    executed: dict[str, str] = field(default_factory=dict)
    precise_coverage: bool = False


class JavaScriptCapture:
    """Observe the workflow page and its related targets without routing requests."""

    def __init__(self):
        self.session: CDPSession | None = None
        self.page: Page | None = None
        self.permits_url: Callable[[str], bool] = lambda _: False
        self.targets: dict[tuple[str, ...], _Target] = {}
        self.target_numbers = count(1)
        self.target_ids: set[str] = set()
        self.disabled_auto_attach: set[tuple[str, ...]] = set()
        self.held: set[tuple[str, ...]] = set()
        self.context_id: str | None = None
        self.commands = count(1)
        self.pending: dict[tuple[tuple[str, ...], int], asyncio.Future] = {}
        self.listeners: dict[str, Callable] = {}
        self.network_urls: set[str] = set()
        self.worker_entries: dict[str, _Target] = {}
        self.browser_responses: dict[str, Response] = {}
        self.browser_listener: Callable[[Response], None] | None = None
        self.tasks: set[asyncio.Task] = set()
        self.reads: set[asyncio.Task] = set()
        self.sampler: asyncio.Task | None = None
        self.gaps: Counter[str] = Counter()
        self.blobs: dict[str, bytes] = {}
        self.source_bytes = 0
        self.delivered: list[dict] = []
        self.runtime: list[dict] = []
        self.attached_count = 0
        self.started = False
        self.accepting = False
        self.stopped = False

    def note_gap(self, reason: str) -> None:
        self.gaps[reason] += 1

    async def start(
        self, context: BrowserContext, page: Page, *, permits_url: Callable[[str], bool]
    ) -> None:
        self.page = page
        self.permits_url = permits_url
        try:
            async with asyncio.timeout(CDP_TIMEOUT):
                self.session = await context.new_cdp_session(page)
        except (Error, TimeoutError):
            self.note_gap("page_cdp_unavailable")
            return

        self.accepting = True

        def observe_response(response: Response) -> None:
            self._browser_response(response)

        self.browser_listener = observe_response
        page.on("response", observe_response)
        root = _Target(next(self.target_numbers), (), "page", page.url)
        self.targets[root.path] = root
        self.attached_count += 1
        for method in (
            "Target.attachedToTarget",
            "Target.detachedFromTarget",
            "Target.receivedMessageFromTarget",
            "Target.targetCreated",
            "Target.targetInfoChanged",
            "Page.frameNavigated",
            "Page.frameDetached",
            "Network.responseReceived",
            "Network.dataReceived",
            "Network.loadingFinished",
            "Network.loadingFailed",
            "Debugger.scriptParsed",
            "Debugger.scriptFailedToParse",
            "Profiler.preciseCoverageDeltaUpdate",
        ):
            listener = self._listener(method)
            self.listeners[method] = listener
            self.session.on(method, listener)

        await self._enable(root)
        if self.started:
            self.sampler = asyncio.create_task(self._sample())

    def _listener(self, method: str) -> Callable[[dict], None]:
        def listen(params: dict) -> None:
            self._event((), method, params)

        return listen

    async def _send(self, path: tuple[str, ...], method: str, params: dict | None = None) -> dict:
        assert self.session is not None
        async with asyncio.timeout(CDP_TIMEOUT):
            if not path:
                return await self.session.send(method, params)

            identity = next(self.commands)
            future = asyncio.get_running_loop().create_future()
            key = (path, identity)
            self.pending[key] = future
            message = {"id": identity, "method": method, "params": params or {}}
            for child in reversed(path[1:]):
                message = {
                    "id": next(self.commands),
                    "method": "Target.sendMessageToTarget",
                    "params": {"sessionId": child, "message": json.dumps(message)},
                }
            try:
                await self.session.send(
                    "Target.sendMessageToTarget", {"sessionId": path[0], "message": json.dumps(message)}
                )
                result = await future
                if "error" in result:
                    raise Error("Related Chromium target rejected the diagnostic command")
                return result.get("result", {})
            finally:
                self.pending.pop(key, None)
                future.cancel()

    def _spawn(self, coroutine: Coroutine, tasks: set[asyncio.Task]) -> None:
        task = asyncio.create_task(coroutine)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _enable(self, target: _Target) -> None:
        path = target.path

        async def network():
            try:
                await self._send(
                    path,
                    "Network.enable",
                    {
                        "maxTotalBufferSize": MAX_TOTAL_SOURCE_BYTES,
                        "maxResourceBufferSize": MAX_SOURCE_BYTES,
                    },
                )
                if not path:
                    self.started = True
            except (Error, TimeoutError):
                self.note_gap("network_observation_unavailable")

        async def frames():
            try:
                await self._send(path, "Page.enable")
                tree = await self._send(path, "Page.getFrameTree")
                self._frame_tree(target, tree.get("frameTree", {}))
            except (Error, TimeoutError):
                self.note_gap("frame_observation_unavailable")

        async def debugger():
            try:
                await self._send(path, "Debugger.setSkipAllPauses", {"skip": True})
                await self._send(path, "Debugger.enable")
                if not path:
                    self.started = True
            except (Error, TimeoutError):
                self.note_gap("script_observation_unavailable")

        async def coverage():
            try:
                await self._send(path, "Profiler.enable")
                if path:
                    try:
                        prior = await self._send(path, "Profiler.getBestEffortCoverage")
                        self._coverage(target, prior, "best_effort_coverage")
                    except (Error, TimeoutError):
                        self.note_gap("target_startup_coverage_unavailable")
                await self._send(
                    path,
                    "Profiler.startPreciseCoverage",
                    {
                        "callCount": False,
                        "detailed": False,
                        "allowTriggeredUpdates": True,
                    },
                )
                target.precise_coverage = True
            except (Error, TimeoutError):
                self.note_gap("execution_coverage_unavailable")

        async def descendants():
            try:
                await self._send(path, "Target.setAutoAttach", AUTO_ATTACH)
            except (Error, TimeoutError):
                self.note_gap("related_target_observation_unavailable")
            if not path:
                try:
                    info = await self._send(path, "Target.getTargetInfo")
                    self.context_id = info.get("targetInfo", {}).get("browserContextId")
                    if not self.context_id:
                        self.note_gap("shared_worker_discovery_unavailable")
                        return
                    await self._send(
                        path,
                        "Target.setDiscoverTargets",
                        {
                            "discover": True,
                            "filter": [{"type": "shared_worker"}, {"exclude": True}],
                        },
                    )
                except (Error, TimeoutError):
                    self.note_gap("shared_worker_discovery_unavailable")

        setup = [network(), debugger(), coverage(), descendants()]
        if target.kind in {"page", "iframe"}:
            setup.append(frames())
        await asyncio.gather(*setup)

    def _frame_tree(self, target: _Target, tree: dict) -> None:
        if frame := tree.get("frame"):
            self._frame_navigated(target, frame)
        for child in tree.get("childFrames", ()):
            self._frame_tree(target, child)

    def _frame_navigated(self, target: _Target, frame: dict) -> None:
        if frame_id := frame.get("id"):
            target.frames[frame_id] = (frame.get("url", ""), frame.get("parentId"))

    def _active_url(self, target: _Target, frame_id: str | None = None) -> bool:
        if target.kind not in {"page", "iframe"}:
            return True
        if frame_id is not None and frame_id in target.frames:
            visited = set()
            while frame_id in target.frames and frame_id not in visited:
                visited.add(frame_id)
                url, frame_id = target.frames[frame_id]
                if self.permits_url(url):
                    return True
                if url not in {"about:blank", "about:srcdoc"} and _scheme(url) not in {"data", "blob"}:
                    return False
        if target.path:
            return self.permits_url(target.url)
        return self.page is not None and self.permits_url(self.page.url)

    def _event(self, path: tuple[str, ...], method: str, params: dict) -> None:
        if method == "Target.receivedMessageFromTarget":
            child_path = (*path, params["sessionId"])
            message = json.loads(params["message"])
            if "id" in message:
                future = self.pending.get((child_path, message["id"]))
                if future is not None and not future.done():
                    future.set_result(message)
            elif "method" in message:
                self._event(child_path, message["method"], message.get("params", {}))
            return

        if target := self.targets.get(path):
            if method == "Target.detachedFromTarget":
                self._detached((*path, params["sessionId"]))
            elif method == "Profiler.preciseCoverageDeltaUpdate":
                self._coverage(target, params, "precise_coverage")
            elif self.accepting or method in {
                "Target.attachedToTarget",
                "Network.dataReceived",
                "Network.loadingFinished",
                "Network.loadingFailed",
            }:
                self._observe(target, method, params)

    def _observe(self, target: _Target, method: str, params: dict) -> None:
        if method == "Target.attachedToTarget":
            self._attached(target, params)
        elif method in {"Target.targetCreated", "Target.targetInfoChanged"}:
            info = params.get("targetInfo", {})
            if (
                info.get("type") == "shared_worker"
                and self.context_id
                and info.get("browserContextId") == self.context_id
                and self._active_url(target)
                and self.permits_url(info.get("url", ""))
            ):
                if info["targetId"] not in self.target_ids and self.attached_count < MAX_TARGETS:
                    self.target_ids.add(info["targetId"])
                    self._spawn(self._attach_shared(info["targetId"]), self.tasks)
        elif method == "Page.frameNavigated":
            self._frame_navigated(target, params.get("frame", {}))
        elif method == "Page.frameDetached":
            target.frames.pop(params.get("frameId"), None)
        elif method == "Network.responseReceived":
            self._response(target, params)
        elif method == "Network.dataReceived":
            request_id = params.get("requestId")
            if request_id in target.response_lengths:
                received, declared = target.response_lengths[request_id]
                target.response_lengths[request_id] = (received + params.get("dataLength", 0), declared)
        elif method in {"Network.loadingFinished", "Network.loadingFailed"}:
            request_id = params.get("requestId")
            if record := target.responses.pop(request_id, None):
                received, declared = target.response_lengths.pop(request_id)
                if method == "Network.loadingFailed":
                    self._omit(record, "response_not_finished")
                elif received == 0 and declared is None:
                    self._omit(record, "response_size_unknown")
                elif max(received, declared or 0) > MAX_CDP_TEXT_BYTES >> len(target.path):
                    self._omit(record, "source_transport_limit")
                else:
                    self._read(target, "Network.getResponseBody", {"requestId": request_id}, record)
        elif method in {"Debugger.scriptParsed", "Debugger.scriptFailedToParse"}:
            self._script(target, params, parsed=method == "Debugger.scriptParsed")

    def _attached(self, parent: _Target, params: dict) -> None:
        info = params["targetInfo"]
        kind, url = info["type"], info.get("url", "")
        path = (*parent.path, params["sessionId"])
        waiting = bool(params.get("waitingForDebugger"))
        if waiting:
            self.held.add(path)
        if kind not in CHILD_TYPES:
            if waiting:
                self._spawn(self._release(path), self.tasks)
            return
        if not self.accepting:
            if waiting:
                self._spawn(self._release(path), self.tasks)
            return
        if self.attached_count >= MAX_TARGETS or len(path) > MAX_TARGET_DEPTH:
            self.note_gap("related_target_limit")
            if waiting:
                self._spawn(self._release(path), self.tasks)
            if parent.path not in self.disabled_auto_attach:
                self.disabled_auto_attach.add(parent.path)
                self._spawn(self._disable_auto_attach(parent.path), self.tasks)
            return
        if kind != "iframe" and not self._active_url(parent) and not self.permits_url(url):
            if waiting:
                self._spawn(self._release(path), self.tasks)
            return
        self.target_ids.add(info["targetId"])
        target = _Target(next(self.target_numbers), path, kind, url)
        self.targets[path] = target
        self.attached_count += 1
        if not waiting:
            self.note_gap("related_target_attached_after_start")
        if kind != "iframe" and _scheme(url) in {"http", "https"}:
            self.worker_entries.setdefault(url, target)
            self._worker_response(url)
        self._spawn(self._start_child(target, waiting), self.tasks)

    async def _start_child(self, target: _Target, waiting: bool) -> None:
        try:
            async with asyncio.timeout(CDP_TIMEOUT):
                await self._enable(target)
        except TimeoutError:
            self.note_gap("related_target_setup_timeout")
        finally:
            if waiting:
                await self._release(target.path)

    async def _release(self, path: tuple[str, ...]) -> None:
        try:
            await self._send(path, "Runtime.runIfWaitingForDebugger")
        except (Error, TimeoutError):
            if path in self.held:
                self.note_gap("related_target_resume_unavailable")
        else:
            self.held.discard(path)

    async def _disable_auto_attach(self, path: tuple[str, ...]) -> None:
        with suppress(Error, TimeoutError):
            await self._send(path, "Target.setAutoAttach", {**AUTO_ATTACH, "autoAttach": False})

    async def _attach_shared(self, target_id: str) -> None:
        try:
            await self._send((), "Target.attachToTarget", {"targetId": target_id, "flatten": False})
        except (Error, TimeoutError):
            self.note_gap("shared_worker_attachment_unavailable")

    def _detached(self, path: tuple[str, ...]) -> None:
        self.held.difference_update(
            candidate for candidate in tuple(self.held) if candidate[: len(path)] == path
        )
        for key in tuple(self.targets):
            if key[: len(path)] == path:
                target = self.targets.pop(key)
                for record in target.responses.values():
                    self._omit(record, "target_detached")
                if any(record["execution"] == "unknown" for record in target.scripts.values()):
                    self.note_gap("target_detached_before_execution_snapshot")
        for (target_path, _), future in tuple(self.pending.items()):
            if target_path[: len(path)] == path and not future.done():
                future.set_result({"error": {}})

    def _new_record(self, records: list[dict], target: _Target, url: str, **fields) -> dict | None:
        if len(self.delivered) + len(self.runtime) >= MAX_RECORDS:
            if not self.gaps["record_limit"]:
                self.note_gap("record_limit")
            return None
        record = {"target": target.number, "target_type": target.kind, "url": _safe_url(url), **fields}
        records.append(record)
        return record

    def _response(self, target: _Target, params: dict) -> None:
        response = params.get("response", {})
        url = response.get("url", "")
        mime_type = response.get("mimeType", "")
        if (
            params.get("type") != "Script"
            and not JAVASCRIPT_MIME.search(mime_type)
            and not _javascript_url(url)
        ):
            return
        if _scheme(url) not in {"http", "https", "blob", "data"}:
            return
        if not self._active_url(target, params.get("frameId")) and not self.permits_url(url):
            return
        request_id = params.get("requestId")
        if not request_id or request_id in target.responses:
            return
        record = self._new_record(
            self.delivered,
            target,
            url,
            status=response.get("status"),
            mime_type=mime_type[:100],
            from_cache=bool(response.get("fromDiskCache") or response.get("fromPrefetchCache")),
            from_service_worker=bool(response.get("fromServiceWorker")),
        )
        if record is not None:
            target.responses[request_id] = record
            headers = {name.lower(): str(value) for name, value in response.get("headers", {}).items()}
            declared = _identity_length(headers.get("content-length"), headers.get("content-encoding"))
            if declared is None and response.get("status") in {204, 205}:
                declared = 0
            target.response_lengths[request_id] = (0, declared)
            self.network_urls.add(url)

    def _browser_response(self, response: Response) -> None:
        # Worker bootstrap can be fetched before its inspector Network session exists.
        if (
            self.accepting
            and response.request.resource_type == "script"
            and _scheme(response.url) in {"http", "https"}
            and response.url not in self.network_urls
        ):
            if len(self.browser_responses) >= MAX_PENDING_READS:
                self.browser_responses.pop(next(iter(self.browser_responses)))
            self.browser_responses[response.url] = response
            self._worker_response(response.url)

    def _worker_response(self, url: str) -> None:
        if url in self.network_urls or url not in self.worker_entries or url not in self.browser_responses:
            return
        response = self.browser_responses.pop(url)
        target = self.worker_entries[url]
        record = self._new_record(self.delivered, target, url, status=response.status, channel="playwright")
        if record is not None:
            self.network_urls.add(url)
            if len(self.reads) >= MAX_PENDING_READS:
                self._omit(record, "concurrent_read_limit")
            else:
                self._spawn(self._read_worker_response(response, record), self.reads)

    async def _read_worker_response(self, response: Response, record: dict) -> None:
        try:
            async with asyncio.timeout(CDP_TIMEOUT):
                size = _identity_length(
                    await response.header_value("content-length"),
                    await response.header_value("content-encoding"),
                )
                if size is None:
                    self._omit(record, "response_size_unknown")
                    return
                if size > MAX_CDP_TEXT_BYTES:
                    self._omit(record, "source_transport_limit")
                    return
                payload = await response.body()
            self._store(record, payload)
            if "source" in record:
                record["encoding"] = "browser_bytes"
        except (Error, TimeoutError):
            self._omit(record, "source_unavailable")
        except asyncio.CancelledError:
            self._omit(record, "capture_stopped_before_source_read")
            raise

    def _script(self, target: _Target, params: dict, *, parsed: bool) -> None:
        if params.get("scriptLanguage") == "WebAssembly":
            return
        context = params.get("executionContextAuxData") or {}
        if context.get("isDefault") is False:
            return
        script_id, url = params.get("scriptId"), params.get("url", "")
        if not script_id or script_id in target.scripts or url.startswith("__playwright"):
            return
        embedder = params.get("embedderName", "")
        stack = params.get("stackTrace", {}).get("callFrames", ())
        if not self._active_url(target, context.get("frameId")) and not self.permits_url(embedder):
            return
        if not embedder and _scheme(url) not in {"http", "https", "blob", "data"} and not stack:
            return
        if stack and any(frame.get("url", "").startswith("__playwright") for frame in stack):
            return
        if not embedder and stack and not any(frame.get("scriptId") in target.scripts for frame in stack):
            return
        if (
            target.kind not in {"page", "iframe"}
            and not target.url
            and _scheme(embedder) in {"http", "https"}
        ):
            target.url = embedder
            self.worker_entries.setdefault(embedder, target)
            self._worker_response(embedder)

        evidence = target.executed.get(script_id)
        generated = bool(stack or params.get("hasSourceURL") or _scheme(url) in {"blob", "data"})
        frame_url = target.frames.get(context.get("frameId"), (None, None))[0]
        kind = "generated" if generated else "inline" if embedder == frame_url else "external"
        record = self._new_record(
            self.runtime,
            target,
            url,
            execution="executed" if evidence else "unknown",
            evidence=evidence,
            module=bool(params.get("isModule")),
            kind=kind,
            parsed=parsed,
            line=params.get("startLine", 0),
            column=params.get("startColumn", 0),
        )
        if record is not None:
            target.scripts[script_id] = record
            # Nested JSON escaping across target sessions can enlarge a CDP frame.
            limit = MAX_CDP_TEXT_BYTES >> len(target.path)
            if params.get("length") is None:
                self._omit(record, "source_size_unknown")
            elif params["length"] > limit:
                self._omit(record, "source_transport_limit")
            else:
                self._read(target, "Debugger.getScriptSource", {"scriptId": script_id}, record)

    def _read(self, target: _Target, method: str, params: dict, record: dict) -> None:
        if len(self.reads) >= MAX_PENDING_READS:
            self._omit(record, "concurrent_read_limit")
        elif self.source_bytes >= MAX_TOTAL_SOURCE_BYTES:
            self._omit(record, "total_source_limit")
        else:
            self._spawn(self._read_source(target, method, params, record), self.reads)

    async def _read_source(self, target: _Target, method: str, params: dict, record: dict) -> None:
        try:
            result = await self._send(target.path, method, params)
            representation = "utf8_text"
            if method == "Debugger.getScriptSource":
                payload = result["scriptSource"].encode("utf-8", errors="surrogatepass")
            elif result.get("base64Encoded"):
                payload = base64.b64decode(result["body"], validate=True)
                representation = "binary"
            else:
                payload = result["body"].encode("utf-8", errors="surrogatepass")
            self._store(record, payload)
            if "source" in record:
                record["encoding"] = representation
        except (Error, TimeoutError, KeyError, ValueError):
            self._omit(record, "source_unavailable")
        except asyncio.CancelledError:
            self._omit(record, "capture_stopped_before_source_read")
            raise

    def _store(self, record: dict, payload: bytes) -> None:
        if len(payload) > MAX_SOURCE_BYTES:
            self._omit(record, "source_size_limit")
            return
        checksum = hashlib.sha256(payload).hexdigest()
        if checksum not in self.blobs:
            if self.source_bytes + len(payload) > MAX_TOTAL_SOURCE_BYTES:
                self._omit(record, "total_source_limit")
                return
            self.blobs[checksum] = payload
            self.source_bytes += len(payload)
        record.update(source=f"sources/{checksum}.js", sha256=checksum, bytes=len(payload))

    def _omit(self, record: dict, reason: str) -> None:
        if "omission" not in record and "source" not in record:
            self.note_gap(reason)
            record["omission"] = reason

    def _coverage(self, target: _Target, params: dict, evidence: str) -> None:
        for item in params.get("result", ()):
            if not any(r.get("count", 0) > 0 for function in item["functions"] for r in function["ranges"]):
                continue
            script_id = item["scriptId"]
            record = target.scripts.get(script_id)
            if record is None and len(target.executed) >= MAX_RECORDS:
                continue
            if evidence == "best_effort_coverage" and target.executed.get(script_id) == "precise_coverage":
                continue
            target.executed[script_id] = evidence
            if record is not None:
                record.update(execution="executed", evidence=evidence)

    async def _snapshot_target(self, target: _Target) -> None:
        try:
            result = await self._send(target.path, "Profiler.takePreciseCoverage")
            self._coverage(target, result, "precise_coverage")
        except (Error, TimeoutError):
            if target.path in self.targets:
                target.precise_coverage = False
                self.note_gap("execution_snapshot_unavailable")

    async def snapshot(self) -> None:
        await asyncio.gather(
            *(self._snapshot_target(target) for target in self.targets.values() if target.precise_coverage)
        )

    async def _sample(self) -> None:
        while self.accepting:
            await asyncio.sleep(COVERAGE_INTERVAL)
            if self.accepting:
                await self.snapshot()

    async def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        self.accepting = False
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                if self.sampler:
                    await self.sampler
                while self.tasks or self.reads or any(target.responses for target in self.targets.values()):
                    if pending := self.tasks | self.reads:
                        await asyncio.wait(pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
                    else:
                        await asyncio.sleep(0.05)
                await self.snapshot()
        except TimeoutError:
            self.note_gap("capture_drain_timeout")
        finally:
            for task in self.tasks | self.reads:
                task.cancel()
            for target in self.targets.values():
                for record in target.responses.values():
                    self._omit(record, "response_not_finished")
            for url in self.worker_entries.keys() - self.network_urls:
                self.note_gap("worker_entry_response_unobserved")
            if self.page and self.browser_listener:
                self.page.remove_listener("response", self.browser_listener)
            if self.session:
                await asyncio.gather(*(self._release(path) for path in self.held))
                for method, listener in self.listeners.items():
                    self.session.remove_listener(method, listener)
                try:
                    async with asyncio.timeout(CDP_TIMEOUT):
                        await self.session.detach()
                except (Error, TimeoutError):
                    self.note_gap("capture_detach_unavailable")

    def build_archive(self, metadata: dict) -> tuple[bytes, dict]:
        """Return the versioned offline ZIP and its small public status summary."""
        while True:
            archive, summary = self._archive(metadata)
            if len(archive) <= MAX_ARCHIVE_BYTES:
                return archive, summary
            if not self.blobs:
                # The record and URL limits leave ample room for the manifest.
                raise ValueError("JavaScript capture metadata exceeds the artifact limit")
            checksum = max(self.blobs, key=lambda candidate: len(self.blobs[candidate]))
            self.source_bytes -= len(self.blobs.pop(checksum))
            for record in (*self.delivered, *self.runtime):
                if record.get("sha256") == checksum:
                    for key in ("source", "sha256", "bytes", "encoding"):
                        record.pop(key, None)
                    self._omit(record, "archive_size_limit")

    def _archive(self, metadata: dict) -> tuple[bytes, dict]:
        summary = {
            "status": "partial" if self.gaps else "ready",
            "delivered_count": len(self.delivered),
            "runtime_count": len(self.runtime),
            "executed_count": sum(record["execution"] == "executed" for record in self.runtime),
            "source_count": len(self.blobs),
            "omitted_count": sum("omission" in record for record in (*self.delivered, *self.runtime)),
            "target_count": self.attached_count,
            "gaps": dict(self.gaps),
        }
        if not self.started:
            summary["status"] = "unavailable"
            if not summary["gaps"]:
                summary["gaps"] = {"capture_not_started": 1}
        manifest = {
            "version": 1,
            "attempt": metadata,
            "capture": summary,
            "responses": self.delivered,
            "runtime_scripts": self.runtime,
        }
        output = io.BytesIO()
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
            for checksum, payload in self.blobs.items():
                archive.writestr(f"sources/{checksum}.js", payload)
        return output.getvalue(), summary

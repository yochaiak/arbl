"""Authenticated, independently leased browser and egress processes.

Run ``python -m abrl.remote_browser browser`` and
``python -m abrl.remote_browser broker`` in isolated network namespaces.
Neither process needs database credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import platform
import shutil
import socket
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from playwright.async_api import async_playwright

from abrl.adapters import AdapterUnavailable, BrowserRuntime, launch_adapter, package_version
from abrl.browser_settings import DevicePreset, device_viewport, supports_user_agent_override
from abrl.network import (
    PROXY_CHECK_TIMEOUT_SECONDS,
    PROXY_DESTINATION_DENIED,
    EgressGuard,
    NetworkDenied,
    check_proxy_ip,
    normalize_proxy_check_ip,
)
from abrl.platform_schemas import validate_user_agent
from abrl.profile_storage import ProfileStorageError, ProfileStore
from abrl.proxy import ProxyBinding

TRANSFER_LIMIT = 256 * 1024 * 1024


def capability_token(master: str, purpose: str) -> str:
    if purpose not in {"browser-control", "broker-control", "proxy-access"}:
        raise ValueError("Unknown remote capability purpose")
    return hmac.digest(master.encode(), ("abrl-" + purpose).encode(), "sha256").hex()


class RuntimeLease:
    def __init__(
        self,
        token: str,
        session_id: str,
        epoch: int,
        *,
        seconds=30,
        startup_grace=180,
        proxy_token=None,
        lifetime_seconds=86400,
    ):
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Remote runtime requires a high-entropy session token")
        if not session_id or epoch < 1 or not 1 <= seconds <= 60:
            raise ValueError("Invalid remote runtime identity or lease")
        if not 1 <= lifetime_seconds <= 86400:
            raise ValueError("Remote runtime lifetime must be between 1 and 86400 seconds")
        self.token, self.session_id, self.epoch = token, session_id, epoch
        self.proxy_token = proxy_token or token
        self.seconds = seconds
        self.lifetime_deadline = monotonic() + lifetime_seconds
        self.deadline = min(monotonic() + startup_grace, self.lifetime_deadline)
        self.stopped = False

    def authorized(self, headers):
        token = headers.get("authorization", "").removeprefix("Bearer ")
        return (
            hmac.compare_digest(token.encode(), self.token.encode())
            and headers.get("x-abrl-session") == self.session_id
            and headers.get("x-abrl-epoch") == str(self.epoch)
        )

    @property
    def expired(self):
        return self.stopped or monotonic() >= self.deadline

    def renew(self):
        if self.expired:
            raise ValueError("Remote ownership lease expired; this runtime cannot be resurrected")
        self.deadline = min(monotonic() + self.seconds, self.lifetime_deadline)

    def identity(self):
        return {"session_id": self.session_id, "epoch": self.epoch}


class RemoteBroker(EgressGuard):
    def __init__(self, config: dict, lease: RuntimeLease, *, bind_host="0.0.0.0", bind_port=3128):
        self.lease = lease
        upstream = ProxyBinding.from_config(config["proxy"]) if config.get("proxy") else None
        super().__init__(
            config["origin"],
            config.get("resource_origins", []),
            lambda _: None,
            upstream=upstream,
            bind_host=bind_host,
            bind_port=bind_port,
            navigation_origins=config.get("navigation_origins", []),
            traffic_budget_bytes=config.get("traffic_budget_bytes", 104857600),
            max_connections=config["proxy"].get("max_connections", 1) if config.get("proxy") else 32,
            vendor_control_origins=config.get("vendor_control_origins", []),
            allow_automatic_assets=not (config.get("proxy") or {}).get("target_origin"),
            startup_origins=config.get("startup_origins", ()),
        )
        self.forwarding_enabled = False
        self.proxy_check_enabled = config.get("proxy_check") is True
        self.proxy_check_started = False

    async def authorize_request(self, method, address, headers, writer):
        values = {}
        for header in headers:
            name, separator, value = header.partition(b":")
            if not separator:
                return False
            name = name.decode("ascii").lower()
            if name in values:
                await self._reply(writer, b"400 Bad Request")
                return False
            values[name] = value.decode("ascii").strip()
        if address.startswith("/__abrl__/"):
            if not self.lease.authorized(values):
                await self._json(writer, 401, {"error": "Runtime authentication required"})
                return False
            command = address.removeprefix("/__abrl__/")
            if command == "status" and method == "GET":
                await self._json(writer, 200, self.state())
            elif command in {"lease", "activate"} and method == "POST":
                try:
                    self.lease.renew()
                    if command == "activate":
                        self.activate_forwarding()
                    await self._json(writer, 200, self.state())
                except ValueError:
                    await self.stop_route()
                    await self._json(writer, 409, {"error": "Runtime ownership expired"})
            elif command == "stop" and method == "POST":
                await self.stop_route()
                await self._json(writer, 200, self.state())
            elif command == "resource-origin" and method == "POST":
                await self._register_asset_origin(writer, values.get("x-abrl-resource-origin", ""))
            elif command == "proxy-check" and method == "POST":
                remaining = self.lease.deadline - monotonic()
                if (
                    not self.proxy_check_enabled
                    or self.proxy_check_started
                    or self.upstream is None
                    or not self.lease.stopped
                    or remaining <= 0
                    or self.budget_exhausted
                ):
                    await self._json(writer, 409, {"error": "Proxy check unavailable"})
                else:
                    self.proxy_check_started = True
                    address = None
                    with suppress(TimeoutError):
                        async with asyncio.timeout(remaining):
                            address = await check_proxy_ip(self.upstream)
                    await self._json(writer, 200, {"ip": address})
            else:
                await self._json(writer, 404, {"error": "Unknown runtime operation"})
            return False
        expected = "Basic " + base64.b64encode(f"abrl-session:{self.lease.proxy_token}".encode()).decode()
        if not hmac.compare_digest(values.get("proxy-authorization", "").encode(), expected.encode()):
            writer.write(
                b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="ABRL"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
            )
            await writer.drain()
            return False
        if self.lease.expired:
            await self.stop_route()
            await self._reply(writer, b"503 Service Unavailable")
            return False
        return True

    async def _register_asset_origin(self, writer, origin: str) -> None:
        if self.lease.expired:
            await self._json(writer, 409, {"error": "Runtime ownership expired"})
            return
        try:
            allowed = self.allow_asset_origin(origin)
        except NetworkDenied:
            await self._json(writer, 400, {"error": "Invalid HTTPS resource origin"})
            return
        await self._json(writer, 200 if allowed else 409, {"allowed": allowed})

    async def stop_route(self):
        self.lease.stopped = True
        self.forwarding_enabled = False
        current = asyncio.current_task()
        active = [task for task in self._connections if task is not current]
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)

    def state(self):
        transport_events = self.transport_events[-128:]
        if len(self.transport_events) > 128:
            # A later burst of timeout events must not hide a denial from the runner.
            denial = next(
                (item for item in reversed(self.transport_events) if item.get("reason") == PROXY_DESTINATION_DENIED),
                None,
            )
            if denial is not None and denial not in transport_events:
                transport_events = [denial, *transport_events[1:]]
        return {
            **self.lease.identity(),
            "stopped": self.lease.stopped,
            "forwarding": self.forwarding_enabled and not self.lease.expired,
            "transport_summary": self.transport_summary(),
            "transport_events": transport_events,
            "transport_usage": self.transport_usage(),
        }

    @staticmethod
    async def _json(writer, status, value):
        body = json.dumps(value).encode()
        label = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 409: "Conflict"}[
            status
        ]
        writer.write(
            f"HTTP/1.1 {status} {label}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()

    async def watchdog(self):
        while not self.lease.expired:
            await asyncio.sleep(0.25)
        if not self.lease.stopped:
            await self.stop_route()


async def remote_proxy_check_ip(connection: dict) -> str | None:
    headers = {
        "Authorization": "Bearer " + capability_token(connection["token"], "broker-control"),
        "X-ABRL-Session": connection["session_id"],
        "X-ABRL-Epoch": str(connection["epoch"]),
    }
    try:
        async with asyncio.timeout(PROXY_CHECK_TIMEOUT_SECONDS + 1):
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(connection["proxy_url"] + "/__abrl__/proxy-check", headers=headers)
                if response.status_code != 200:
                    return None
                payload = response.json()
                return normalize_proxy_check_ip(payload.get("ip")) if isinstance(payload, dict) else None
    except (httpx.HTTPError, TimeoutError, ValueError):
        return None


def transfer_key(lease: RuntimeLease) -> bytes:
    return hmac.digest(
        lease.token.encode(), f"abrl-profile-transfer:{lease.session_id}:{lease.epoch}".encode(), "sha256"
    )


class BrokerRelay:
    """Loopback-only proxy credential relay for native/vendor browser contexts.

    Chromium sees no credentials and needs no authentication interception. The
    only upstream is the session's authenticated broker, which owns all scope,
    DNS, provider credentials and lease checks. This relay has no control token.
    """

    def __init__(self, broker_url: str, proxy_token: str):
        broker = urlsplit(broker_url)
        if (
            broker.scheme != "http"
            or not broker.hostname
            or broker.username
            or broker.path not in {"", "/"}
        ):
            raise ValueError("Relay requires the configured broker HTTP origin")
        self.host, self.port = broker.hostname, broker.port or 80
        self.authorization = base64.b64encode(f"abrl-session:{proxy_token}".encode())
        self.server = None
        self.connections = set()
        self.url = None

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=32768)
        self.url = "http://127.0.0.1:" + str(self.server.sockets[0].getsockname()[1])
        return self

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        tasks = list(self.connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, reader, writer):
        task, upstream = asyncio.current_task(), None
        self.connections.add(task)
        transfers = []
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            if len(head) > 16384:
                raise ValueError("Relay request header exceeds budget")
            lines = head[:-4].split(b"\r\n")
            headers = [
                line for line in lines[1:] if line.partition(b":")[0].lower() != b"proxy-authorization"
            ]
            source, upstream = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 5)
            upstream.write(
                b"\r\n".join([lines[0], *headers, b"Proxy-Authorization: Basic " + self.authorization])
                + b"\r\n\r\n"
            )
            await upstream.drain()
            transfers = [
                asyncio.create_task(EgressGuard._pipe(reader, upstream)),
                asyncio.create_task(EgressGuard._pipe(source, writer)),
            ]
            await asyncio.wait(transfers, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, TimeoutError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            for transfer in transfers:
                transfer.cancel()
            await asyncio.gather(*transfers, return_exceptions=True)
            for stream in (writer, upstream):
                if stream:
                    stream.close()
                    with suppress(OSError):
                        await stream.wait_closed()
            self.connections.discard(task)


class BrowserService:
    def __init__(self, config: dict, lease: RuntimeLease):
        proxy = urlsplit(config["proxy_url"])
        if proxy.scheme != "http" or not proxy.hostname or proxy.username or proxy.path not in {"", "/"}:
            raise ValueError("Remote browser requires a credential-free broker HTTP origin")
        self.config, self.lease = config, lease
        self.root = Path(tempfile.mkdtemp(prefix="abrl-remote-"))
        self.directory = self.root / "profile"
        self.transfer = ProfileStore(self.root / "transfer", transfer_key(lease))
        self.context = self.browser = self.driver = None
        self.metadata = {
            "adapter": config.get("adapter", "playwright"),
            "os": platform.system(),
            "architecture": platform.machine(),
            "wrapper_version": package_version("playwright"),
            "runtime_stopped": False,
            "profile_saved": False,
            "remote": True,
        }
        self.backend_ws = None
        self.persistent = False
        self.restore_profile = False
        self.device_preset: DevicePreset | None = None
        self.launched = False
        self.lock = asyncio.Lock()
        self.stop_lock = asyncio.Lock()
        self.adapter_context = None
        self.launch_task = None
        self.proxy_relay = None

    async def launch(
        self,
        persistent: bool,
        compatibility: dict,
        *,
        restore_profile: bool = False,
        user_agent: str | None = None,
        device_preset: DevicePreset | None = None,
    ):
        try:
            user_agent = validate_user_agent(user_agent)
            device_viewport(device_preset)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if user_agent is not None and not supports_user_agent_override(self.metadata["adapter"]):
            raise HTTPException(409, "Custom User-Agent requires Playwright Chromium")
        restore_profile = restore_profile or bool(compatibility)
        if restore_profile and not persistent:
            raise HTTPException(409, "profile_restore_failed")
        async with self.lock:
            if self.lease.expired:
                raise HTTPException(409, "Browser lease expired")
            if self.launched:
                configured_user_agent = self.metadata.get("configured_user_agent")
                if (
                    persistent != self.persistent
                    or restore_profile != self.restore_profile
                    or user_agent != configured_user_agent
                    or device_preset != self.device_preset
                ):
                    raise HTTPException(409, "Runtime launch configuration is immutable")
                return self.state()
            self.lease.renew()
            self.persistent = persistent
            self.restore_profile = restore_profile
            self.device_preset = device_preset
            input_path = self.transfer.root / self.lease.session_id / "input.abrl"
            if restore_profile and not input_path.is_file():
                raise HTTPException(409, "profile_restore_failed")
            if persistent and input_path.exists():
                try:
                    await asyncio.to_thread(
                        self.transfer.restore,
                        self.lease.session_id,
                        "input",
                        self.directory,
                        compatibility=compatibility,
                    )
                except ProfileStorageError as exc:
                    raise HTTPException(409, "profile_restore_failed") from exc
            with socket.socket() as temporary:
                temporary.bind(("127.0.0.1", 0))
                port = temporary.getsockname()[1]
            # Remove controller/provider credentials from the Chromium child env.
            browser_env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("ABRL_", "AWS_", "PG", "REDIS_", "KAMELEO_", "CLOAKBROWSER_"))
                and key not in {"DATABASE_URL", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
            }
            try:
                self.launch_task = asyncio.current_task()
                proxy_url = self.config["proxy_url"]
                if self.metadata["adapter"] in {"manual_chrome", "cloakbrowser", "kameleo"}:
                    self.proxy_relay = await BrokerRelay(proxy_url, self.lease.proxy_token).start()
                    proxy_url = self.proxy_relay.url
                self.adapter_context = launch_adapter(
                    self.metadata["adapter"],
                    proxy_url,
                    str(self.directory) if persistent else None,
                    self.metadata,
                    debug_port=port,
                    child_env=browser_env,
                    expected_compatibility=compatibility if restore_profile else None,
                    proxy_auth=None
                    if self.proxy_relay is not None
                    else {"username": "abrl-session", "password": self.lease.proxy_token},
                    user_agent=user_agent,
                    device_preset=device_preset,
                )
                runtime = await self.adapter_context.__aenter__()
                self.context = runtime.context
                self.browser = self.context.browser
                if self.metadata["adapter"] in {"kameleo", "manual_chrome"}:
                    self.backend_ws = self.metadata["cdp_endpoint"]
                    page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                    session = await self.context.new_cdp_session(page)
                    info = await session.send("Browser.getVersion")
                    self.metadata["browser_version"] = info["product"]
                else:
                    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                        info = (await client.get(f"http://127.0.0.1:{port}/json/version")).json()
                    self.backend_ws = info["webSocketDebuggerUrl"]
                    self.metadata["browser_version"] = info["Browser"]
                marker = "about:blank#abrl-" + self.lease.session_id
                page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                await page.goto(marker)
                self.metadata["context_marker"] = marker
                self.metadata["configured_device_preset"] = device_preset
                self.launched = True
            except BaseException:
                await self.stop()
                raise
            finally:
                self.launch_task = None
            return self.state()

    def state(self):
        return {
            **self.lease.identity(),
            "launched": self.launched,
            "stopped": self.metadata["runtime_stopped"],
            "metadata": self.metadata,
            "persistent": self.persistent,
        }

    async def stop(self):
        # An expired lease can arrive during a slow vendor launch. Cancel and
        # await its cleanup instead of exiting an async generator while it is
        # still entering, which could leave an unowned process behind.
        launching = self.launch_task
        if launching and launching is not asyncio.current_task() and not launching.done():
            launching.cancel()
            await asyncio.gather(launching, return_exceptions=True)
        async with self.stop_lock:
            if self.metadata["runtime_stopped"]:
                if self.proxy_relay:
                    await self.proxy_relay.close()
                return
            self.lease.stopped = True
            try:
                async with asyncio.timeout(20):
                    if self.adapter_context:
                        await self.adapter_context.__aexit__(None, None, None)
                    elif self.context:
                        await self.context.close()
                    if self.browser and not self.adapter_context:
                        await self.browser.close()
                    if self.driver:
                        await self.driver.stop()
                if not self.adapter_context:
                    self.metadata.update(
                        runtime_stopped=True, profile_saved=self.persistent and self.launched
                    )
            except Exception:
                self.metadata.update(
                    runtime_stopped=False,
                    profile_saved=False,
                    cleanup_error="Remote browser shutdown could not be confirmed",
                )
            finally:
                if self.proxy_relay:
                    await self.proxy_relay.close()

    async def watchdog(self):
        while not self.lease.expired:
            await asyncio.sleep(0.25)
        await self.stop()


def create_browser_app(config: dict, lease: RuntimeLease) -> FastAPI:
    service = BrowserService(config, lease)

    @asynccontextmanager
    async def lifespan(app):
        watchdog = asyncio.create_task(service.watchdog())
        try:
            yield
        finally:
            watchdog.cancel()
            with suppress(asyncio.CancelledError):
                await watchdog
            await service.stop()
            if service.metadata["runtime_stopped"]:
                shutil.rmtree(service.root)

    app = FastAPI(lifespan=lifespan, docs_url=None, openapi_url=None, redoc_url=None)
    app.state.runtime = service

    def authenticate(headers):
        if not lease.authorized(headers):
            raise HTTPException(401, "Runtime authentication required")

    @app.get("/v1/status")
    async def status(request: Request):
        authenticate(request.headers)
        return service.state()

    @app.post("/v1/lease")
    async def renew(request: Request):
        authenticate(request.headers)
        try:
            lease.renew()
        except ValueError:
            raise HTTPException(409, "Runtime ownership expired")
        return service.state()

    @app.post("/v1/launch")
    async def launch(request: Request):
        authenticate(request.headers)
        content = await request.json()
        if content.get("adapter", "playwright") != service.metadata["adapter"]:
            raise HTTPException(409, "Remote browser adapter does not match the admitted image")
        restore_profile = content.get("restore_profile", False)
        if type(restore_profile) is not bool:
            raise HTTPException(422, "Invalid profile restoration flag")
        try:
            return await service.launch(
                bool(content.get("persistent")),
                content.get("compatibility") or {},
                restore_profile=restore_profile,
                user_agent=content.get("user_agent"),
                device_preset=content.get("device_preset"),
            )
        except AdapterUnavailable as exc:
            if exc.status == "profile_restore_failed":
                raise HTTPException(409, exc.status) from exc
            raise

    @app.post("/v1/stop")
    async def stop(request: Request):
        authenticate(request.headers)
        await service.stop()
        return service.state()

    @app.post("/v1/profile")
    async def upload(request: Request):
        authenticate(request.headers)
        if service.launched or lease.expired:
            raise HTTPException(409, "Profile upload requires a new owned runtime")
        payload = bytearray()
        async for part in request.stream():
            payload.extend(part)
            if len(payload) > TRANSFER_LIMIT:
                raise HTTPException(413, "Profile transfer exceeds 256 MiB")
        folder = service.transfer.root / lease.session_id
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / "input.abrl"
        try:
            with path.open("xb") as stream:
                stream.write(payload)
            path.chmod(0o600)
        except FileExistsError:
            raise HTTPException(409, "Profile upload is immutable")
        return {"uploaded": True, "sha256": hashlib.sha256(payload).hexdigest()}

    @app.get("/v1/profile")
    async def download(request: Request):
        authenticate(request.headers)
        if not service.metadata["runtime_stopped"] or not service.metadata["profile_saved"]:
            raise HTTPException(409, "Profile export requires confirmed clean shutdown")
        path = service.transfer.root / lease.session_id / "output.abrl"
        if not path.exists():
            compatibility = {
                key: service.metadata.get(key)
                for key in ("adapter", "browser_version", "os", "architecture")
            }
            await asyncio.to_thread(
                service.transfer.save,
                lease.session_id,
                service.directory,
                clean_shutdown=True,
                compatibility=compatibility,
                generation="output",
            )
        if path.stat().st_size > TRANSFER_LIMIT:
            raise HTTPException(413, "Profile transfer exceeds 256 MiB")
        return Response(path.read_bytes(), media_type="application/octet-stream")

    @app.get("/json/version")
    @app.get("/json/version/")
    async def version(request: Request):
        authenticate(request.headers)
        if not service.launched or lease.expired:
            raise HTTPException(409, "Browser is unavailable")
        return {
            "Browser": service.metadata["browser_version"],
            "webSocketDebuggerUrl": str(request.base_url)
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            .rstrip("/")
            + "/cdp",
        }

    @app.websocket("/cdp")
    async def cdp(socket: WebSocket):
        if not lease.authorized(socket.headers) or lease.expired or not service.backend_ws:
            await socket.close(code=1008)
            return
        import websockets

        await socket.accept()
        try:
            async with websockets.connect(service.backend_ws, max_size=16 * 1024**2, proxy=None) as backend:

                async def send_to_browser():
                    while True:
                        await backend.send(await socket.receive_text())

                async def send_to_controller():
                    async for message in backend:
                        await socket.send_text(message)

                tasks = [asyncio.create_task(send_to_browser()), asyncio.create_task(send_to_controller())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (WebSocketDisconnect, OSError, websockets.ConnectionClosed):
            pass
        finally:
            with suppress(RuntimeError, WebSocketDisconnect):
                await socket.close()

    return app


@asynccontextmanager
async def remote_adapter(
    connection: dict,
    profile_dir: str | None,
    metadata: dict,
    expected_compatibility: dict | None,
    *,
    adapter="playwright",
    user_agent: str | None = None,
    device_preset: DevicePreset | None = None,
):
    master = connection["token"]
    lease = RuntimeLease(
        capability_token(master, "browser-control"),
        connection["session_id"],
        int(connection["epoch"]),
        proxy_token=capability_token(master, "proxy-access"),
    )
    headers = {
        "Authorization": "Bearer " + lease.token,
        "X-ABRL-Session": lease.session_id,
        "X-ABRL-Epoch": str(lease.epoch),
    }
    browser_url, proxy_url = connection["browser_url"], connection["proxy_url"]
    client = httpx.AsyncClient(headers=headers, timeout=60, trust_env=False)
    heartbeat = None
    transfer_dir = Path(tempfile.mkdtemp(prefix="abrl-profile-transfer-"))
    store = ProfileStore(transfer_dir, transfer_key(lease))
    driver = browser = context = None

    async def request(method, endpoint, **kwargs):
        if endpoint.startswith(proxy_url + "/__abrl__/"):
            request_headers = httpx.Headers(kwargs.get("headers"))
            request_headers.update(headers)
            request_headers["Authorization"] = "Bearer " + capability_token(master, "broker-control")
            kwargs["headers"] = request_headers
        response = await client.request(method, endpoint, **kwargs)
        if response.status_code >= 400:
            if response.status_code == 409 and endpoint == browser_url + "/v1/launch":
                restore_failure = False
                with suppress(ValueError):
                    restore_failure = response.json() == {"detail": "profile_restore_failed"}
                if restore_failure:
                    raise AdapterUnavailable("The saved remote profile could not be restored", "profile_restore_failed")
            raise ValueError(f"Remote runtime operation failed ({response.status_code})")
        return response

    async def keepalive():
        while True:
            await request("POST", browser_url + "/v1/lease")
            await request("POST", proxy_url + "/__abrl__/lease")
            await asyncio.sleep(3)

    async def activate(*, traffic_budget_bytes=104857600):
        state = (await request("GET", proxy_url + "/__abrl__/status")).json()
        if state.get("transport_usage", {}).get("limit_bytes") != traffic_budget_bytes:
            raise ValueError("Remote broker traffic budget does not match the assignment")
        response = (await request("POST", proxy_url + "/__abrl__/activate")).json()
        if response.get("session_id") != lease.session_id or response.get("epoch") != lease.epoch:
            raise ValueError("Remote broker identity does not match")

    async def allow_asset_origin(origin: str) -> bool:
        try:
            response = await request(
                "POST",
                proxy_url + "/__abrl__/resource-origin",
                headers={"X-ABRL-Resource-Origin": origin},
                timeout=2,
            )
            return response.status_code == 200
        except (httpx.HTTPError, ValueError):
            return False

    try:
        # Pod Running is not application readiness. Wait only within a finite startup budget.
        for _ in range(30):
            try:
                state = (await request("GET", browser_url + "/v1/status")).json()
                break
            except httpx.TransportError:
                await asyncio.sleep(1)
        else:
            raise ValueError("Remote browser did not become ready")
        if state.get("session_id") != lease.session_id or state.get("epoch") != lease.epoch:
            raise ValueError("Remote browser identity does not match")
        heartbeat = asyncio.create_task(keepalive())
        if profile_dir:
            manifest = await asyncio.to_thread(
                store.save,
                lease.session_id,
                profile_dir,
                clean_shutdown=True,
                compatibility=expected_compatibility or {},
                generation="input",
            )
            payload = Path(manifest["path"]).read_bytes()
            if len(payload) > TRANSFER_LIMIT:
                raise ValueError("Remote profile transfer exceeds 256 MiB")
            await request("POST", browser_url + "/v1/profile", content=payload)
        launch_config = {
            "persistent": bool(profile_dir),
            "compatibility": expected_compatibility or {},
            "restore_profile": expected_compatibility is not None,
            "adapter": adapter,
        }
        if user_agent is not None:
            launch_config["user_agent"] = user_agent
        if device_preset is not None:
            launch_config["device_preset"] = device_preset
        state = (
            await request(
                "POST",
                browser_url + "/v1/launch",
                json=launch_config,
            )
        ).json()
        launched_metadata = state["metadata"]
        if user_agent is not None and launched_metadata.get("configured_user_agent") != user_agent:
            raise AdapterUnavailable(
                "Remote Playwright Chromium did not confirm the configured User-Agent",
                "unsupported_operation",
            )
        if device_preset is not None and launched_metadata.get("configured_device_preset") != device_preset:
            raise AdapterUnavailable("Remote browser did not confirm the device preset", "unsupported_operation")
        driver = await async_playwright().start()
        browser = await driver.chromium.connect_over_cdp(browser_url, headers=headers, timeout=30_000)
        marker = launched_metadata["context_marker"]
        contexts = [
            context for context in browser.contexts if any(page.url == marker for page in context.pages)
        ]
        if len(contexts) != 1:
            raise ValueError("Remote browser must expose exactly one owned context marker")
        if user_agent is not None:
            page = next(page for page in contexts[0].pages if page.url == marker)
            if await page.evaluate("navigator.userAgent") != user_agent:
                raise AdapterUnavailable(
                    "Remote Playwright Chromium did not apply the configured User-Agent",
                    "invalid_configuration",
                )
        context = contexts[0]
        metadata.update(
            launched_metadata, egress_enforcement="remote broker; deployment network policies required"
        )
        runtime = BrowserRuntime(context, metadata)
        runtime.activate = activate
        runtime.allow_asset_origin = allow_asset_origin
        runtime.lease_task = heartbeat
        yield runtime
    finally:

        async def shutdown():
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                state = (await request("POST", browser_url + "/v1/stop")).json()
                broker_state = (await request("POST", proxy_url + "/__abrl__/stop")).json()
                metadata.update(
                    transport_summary=broker_state["transport_summary"],
                    transport_events=broker_state["transport_events"],
                    transport_usage=broker_state["transport_usage"],
                    vendor_control_bytes=broker_state["transport_usage"]["vendor_control_bytes"],
                    runtime_stopped=bool(state["stopped"] and broker_state["stopped"]),
                    browser_closed=bool(state["stopped"]),
                    profile_saved=False,
                )
                if (
                    profile_dir
                    and ((user_agent is None and device_preset is None) or context is not None)
                    and state["metadata"].get("profile_saved")
                    and metadata["runtime_stopped"]
                ):
                    payload = (await request("GET", browser_url + "/v1/profile")).content
                    folder = store.root / lease.session_id
                    folder.mkdir(mode=0o700, exist_ok=True)
                    (folder / "output.abrl").write_bytes(payload)
                    compatibility = {
                        key: state["metadata"].get(key)
                        for key in ("adapter", "browser_version", "os", "architecture")
                    }
                    destination = Path(profile_dir).with_name(Path(profile_dir).name + ".restored")
                    await asyncio.to_thread(
                        store.restore, lease.session_id, "output", destination, compatibility=compatibility
                    )
                    shutil.rmtree(profile_dir)
                    os.rename(destination, profile_dir)
                    metadata["profile_saved"] = True
            except Exception:
                metadata.update(
                    profile_saved=False,
                    runtime_stopped=False,
                    cleanup_error="Remote shutdown/profile transfer could not be confirmed",
                )
            finally:
                if browser:
                    with suppress(Exception):
                        await browser.close()
                if driver:
                    with suppress(Exception):
                        await driver.stop()
                await client.aclose()
                shutil.rmtree(transfer_dir)

        cleanup = asyncio.create_task(shutdown())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


async def broker_main(config, lease):
    async with RemoteBroker(config, lease, bind_port=int(os.getenv("ABRL_REMOTE_PORT", "3128"))) as broker:
        await broker.watchdog()
        # Keep the authenticated stopped status available until pod teardown.
        await asyncio.Event().wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("browser", "broker"))
    args = parser.parse_args()
    lease = RuntimeLease(
        os.environ["ABRL_REMOTE_TOKEN"],
        os.environ["ABRL_REMOTE_SESSION_ID"],
        int(os.environ["ABRL_REMOTE_EPOCH"]),
        proxy_token=os.environ["ABRL_REMOTE_PROXY_TOKEN"],
        lifetime_seconds=int(os.getenv("ABRL_REMOTE_LIFETIME_SECONDS", "86400")),
    )
    if args.role == "broker":
        asyncio.run(broker_main(json.loads(os.environ["ABRL_EGRESS_CONFIG_JSON"]), lease))
    else:
        import uvicorn

        adapter = os.environ.get("ABRL_REMOTE_ADAPTER", "playwright")
        if adapter in {"cloakbrowser", "kameleo"}:
            address = urlsplit(os.environ["ABRL_REMOTE_PROXY_URL"])
            authenticated_proxy = f"http://abrl-session:{lease.proxy_token}@{address.netloc}"
            os.environ.update(
                HTTP_PROXY=authenticated_proxy,
                HTTPS_PROXY=authenticated_proxy,
                NO_PROXY="127.0.0.1,localhost",
            )
        app = create_browser_app(
            {"proxy_url": os.environ["ABRL_REMOTE_PROXY_URL"], "adapter": adapter}, lease
        )
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("ABRL_REMOTE_PORT", "9222")), access_log=False)


if __name__ == "__main__":
    main()

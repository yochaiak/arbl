"""Authenticated, independently leased browser and egress processes.

Run in separate network namespaces/pods: ``python -m abrl.remote_browser browser``
and ``python -m abrl.remote_browser broker``. Neither process needs DB credentials.
The Kubernetes controller enforces the network boundary and confirms pod deletion.
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

from abrl.adapters import BrowserRuntime, launch_adapter, package_version
from abrl.network import EgressGuard, NetworkDenied, normalize_origin
from abrl.profile_storage import ProfileStore
from abrl.proxy import OxylabsBinding

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
        upstream = OxylabsBinding.from_config(config["proxy"]) if config.get("proxy") else None
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
        )
        self.forwarding_enabled = False
        self.vendor_controls = {
            normalize_origin(origin) for origin in config.get("vendor_control_origins", [])
        }
        self.allowed |= self.vendor_controls

    def forwarding_allowed(self, method, address):
        if self.forwarding_enabled:
            return True
        try:
            return method == "CONNECT" and normalize_origin("https://" + address) in self.vendor_controls
        except NetworkDenied:
            return False

    def uses_upstream(self, origin):
        return self.upstream is not None and origin not in self.vendor_controls

    def is_vendor_control(self, origin):
        return origin in self.vendor_controls

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
                        self.forwarding_enabled = True
                    await self._json(writer, 200, self.state())
                except ValueError:
                    await self.stop_route()
                    await self._json(writer, 409, {"error": "Runtime ownership expired"})
            elif command == "stop" and method == "POST":
                await self.stop_route()
                await self._json(writer, 200, self.state())
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

    async def stop_route(self):
        self.lease.stopped = True
        self.forwarding_enabled = False
        current = asyncio.current_task()
        active = [task for task in self._connections if task is not current]
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)

    def state(self):
        return {
            **self.lease.identity(),
            "stopped": self.lease.stopped,
            "forwarding": self.forwarding_enabled and not self.lease.expired,
            "transport_summary": self.transport_summary(),
            "transport_events": self.transport_events[-128:],
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
        await self.stop_route()


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
        self.launched = False
        self.lock = asyncio.Lock()
        self.stop_lock = asyncio.Lock()
        self.adapter_context = None
        self.launch_task = None
        self.proxy_relay = None

    async def launch(self, persistent: bool, compatibility: dict):
        async with self.lock:
            if self.lease.expired:
                raise HTTPException(409, "Browser lease expired")
            if self.launched:
                if persistent != self.persistent:
                    raise HTTPException(409, "Runtime launch configuration is immutable")
                return self.state()
            self.lease.renew()
            self.persistent = persistent
            input_path = self.transfer.root / self.lease.session_id / "input.abrl"
            if persistent and input_path.exists():
                await asyncio.to_thread(
                    self.transfer.restore,
                    self.lease.session_id,
                    "input",
                    self.directory,
                    compatibility=compatibility,
                )
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
                if self.metadata["adapter"] in {"manual_chrome", "kameleo"}:
                    self.proxy_relay = await BrokerRelay(proxy_url, self.lease.proxy_token).start()
                    proxy_url = self.proxy_relay.url
                self.adapter_context = launch_adapter(
                    self.metadata["adapter"],
                    proxy_url,
                    str(self.directory) if persistent else None,
                    self.metadata,
                    debug_port=port,
                    child_env=browser_env,
                    proxy_auth=None
                    if self.metadata["adapter"] in {"manual_chrome", "kameleo"}
                    else {"username": "abrl-session", "password": self.lease.proxy_token},
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
        return await service.launch(bool(content.get("persistent")), content.get("compatibility") or {})

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
            kwargs["headers"] = {
                **headers,
                "Authorization": "Bearer " + capability_token(master, "broker-control"),
            }
        response = await client.request(method, endpoint, **kwargs)
        if response.status_code >= 400:
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
        state = (
            await request(
                "POST",
                browser_url + "/v1/launch",
                json={
                    "persistent": bool(profile_dir),
                    "compatibility": expected_compatibility or {},
                    "adapter": adapter,
                },
            )
        ).json()
        metadata.update(
            state["metadata"], egress_enforcement="remote broker; deployment network policies required"
        )
        driver = await async_playwright().start()
        browser = await driver.chromium.connect_over_cdp(browser_url, headers=headers, timeout=30_000)
        contexts = [
            context
            for context in browser.contexts
            if any(page.url == state["metadata"]["context_marker"] for page in context.pages)
        ]
        if len(contexts) != 1:
            raise ValueError("Remote browser must expose exactly one owned context marker")
        context = contexts[0]
        runtime = BrowserRuntime(context, metadata)
        runtime.activate = activate
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
                if profile_dir and state["metadata"].get("profile_saved") and metadata["runtime_stopped"]:
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

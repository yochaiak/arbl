"""Supported browser lifecycles. No custom fingerprint mutations are applied."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import platform
import shutil
import signal
import socket
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from playwright.async_api import BrowserContext, async_playwright

from .profile_storage import ProfileLock

NETWORK_ARGS = [
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    "--proxy-bypass-list=<-loopback>",
]


class AdapterUnavailable(ValueError):
    def __init__(self, reason: str, status: str = "unavailable"):
        super().__init__(reason)
        self.status = status


def package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def chrome_path() -> str | None:
    configured = os.environ.get("ABRL_CHROME_EXECUTABLE")
    candidates = (
        [configured]
        if configured
        else [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            shutil.which("google-chrome"),
            shutil.which("google-chrome-stable"),
        ]
    )
    return next((p for p in candidates if p and Path(p).is_file() and os.access(p, os.X_OK)), None)


def _chromium_cached() -> bool:
    import playwright

    directory = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if directory == "0":
        root = Path(playwright.__file__).parent / "driver/package/.local-browsers"
    elif directory:
        root = Path(directory)
    elif platform.system() == "Darwin":
        root = Path.home() / "Library/Caches/ms-playwright"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ms-playwright"
    manifest = json.loads((Path(playwright.__file__).parent / "driver/package/browsers.json").read_text())
    chromium = next(item for item in manifest["browsers"] if item["name"] == "chromium")
    paths = [root / f"chromium-{chromium['revision']}"]
    paths += [root / f"chromium-{r}" for r in chromium.get("revisionOverrides", {}).values()]
    return any(p.is_dir() for p in paths)


def _entry(identifier, name, status, reason, *, capabilities=None, **extra):
    return {
        "id": identifier,
        "name": name,
        "available": status in {"ready", "configured"},
        "status": status,
        "reason": reason,
        "capabilities": capabilities or ["fresh", "persistent", "manual", "automated_preparation"],
        **extra,
    }


def adapter_capabilities() -> list[dict]:
    """Side-effect free inventory. Runtime probes, license acquisition are launch-time."""
    try:
        cached = _chromium_cached()
    except (OSError, ValueError, StopIteration):
        cached = False
    entries = [
        _entry(
            "playwright",
            "Playwright Chromium",
            "configured" if cached else "missing_binary",
            "Local Chromium installed; launch validation occurs per session"
            if cached
            else "Install Chromium with: uv run playwright install chromium",
            version=package_version("playwright"),
            max_concurrency=None,
        )
    ]
    chrome = chrome_path()
    display = platform.system() == "Darwin" or bool(os.environ.get("DISPLAY"))
    entries.append(
        _entry(
            "manual_chrome",
            "Manual Google Chrome",
            "configured" if chrome and display else "missing_binary" if not chrome else "missing_display",
            "Requires a controlling operator"
            if chrome and display
            else "Install Google Chrome and configure a headed display",
            capabilities=["fresh", "persistent", "manual", "human_control"],
            max_concurrency=None,
        )
    )
    cloak_version = package_version("cloakbrowser")
    cloak_status, cloak_reason = "missing_package", "Install the optional cloakbrowser package"
    info = {}
    if cloak_version:
        try:
            cloak = importlib.import_module("cloakbrowser")
            info = cloak.binary_info()
            if not os.environ.get("CLOAKBROWSER_LICENSE_KEY"):
                cloak_status, cloak_reason = "missing_license", "Set CLOAKBROWSER_LICENSE_KEY"
            elif not info.get("installed"):
                cloak_status, cloak_reason = "missing_binary", "Preinstall the licensed CloakBrowser binary"
            elif os.environ.get("CLOAKBROWSER_VERSION") and os.environ.get("ABRL_CLOAK_TIER") != "pro":
                cloak_status, cloak_reason = (
                    "incompatible_license",
                    "Exact version pinning requires a verified Pro entitlement",
                )
            else:
                cloak_status, cloak_reason = (
                    "configured",
                    "License and seat availability verified at launch",
                )
        except (ImportError, OSError, ValueError):
            cloak_status, cloak_reason = (
                "unsupported_platform",
                "CloakBrowser could not inspect its platform binary",
            )
    entries.append(
        _entry(
            "cloakbrowser",
            "CloakBrowser",
            cloak_status,
            cloak_reason,
            version=cloak_version,
            binary_version=info.get("version"),
            max_concurrency=1 if os.environ.get("ABRL_CLOAK_TIER", "free") == "free" else None,
            limitations=[
                "Free tier: one seat and no exact binary pinning",
                "Vendor-default baseline; humanize disabled",
            ],
        )
    )
    sdk = package_version("kameleo.local-api-client")
    endpoint = os.environ.get("ABRL_KAMELEO_ENDPOINT")
    status, reason = "missing_package", "Install matching kameleo.local-api-client SDK and engine"
    if sdk:
        status, reason = "configured", "Engine readiness, SDK compatibility and quota verified at launch"
        if not endpoint:
            status, reason = "missing_engine", "Set ABRL_KAMELEO_ENDPOINT to the same-host engine"
        elif platform.system() == "Linux" and platform.machine().lower() not in {"x86_64", "amd64"}:
            status, reason = "unsupported_platform", "Kameleo Linux requires amd64"
    entries.append(
        _entry(
            "kameleo",
            "Kameleo Chroma",
            status,
            reason,
            version=sdk,
            max_concurrency=None,
            limitations=[
                "Same-host engine and shared export directory required",
                "Proxy change requires stop/restart",
            ],
        )
    )
    if os.environ.get("ABRL_DEPLOYMENT") == "cloud" and not os.environ.get("ABRL_REMOTE_ADAPTER"):
        images = {
            "playwright": "ABRL_BROWSER_IMAGE",
            "manual_chrome": "ABRL_BROWSER_IMAGE_MANUAL_CHROME",
            "cloakbrowser": "ABRL_CLOAK_BROWSER_IMAGE",
            "kameleo": "ABRL_KAMELEO_BROWSER_IMAGE",
        }
        for entry in entries:
            if entry["id"] in images:
                configured = bool(os.environ.get(images[entry["id"]]))
                entry.update(
                    available=configured,
                    status="configured" if configured else "missing_image",
                    reason="Remote package, engine and license readiness checked in the admitted runtime"
                    if configured
                    else f"Configure {images[entry['id']]} with a tested image digest",
                    max_concurrency=None
                    if entry["id"] in {"playwright", "manual_chrome"}
                    else max(1, int(os.environ.get("ABRL_VENDOR_CAPACITY", "1"))),
                )
    if os.environ.get("ABRL_BROWSER_WS_ENDPOINT") or os.environ.get("ABRL_EGRESS_PROXY_URL"):
        for entry in entries:
            entry.update(
                available=False,
                status="unsupported_operation",
                reason="Remote browser execution requires an authenticated session-scoped broker; not yet verified",
            )
    return entries


@dataclass
class BrowserRuntime:
    context: BrowserContext
    metadata: dict


def _check_capability(adapter: str):
    entry = next((item for item in adapter_capabilities() if item["id"] == adapter), None)
    if not entry:
        raise AdapterUnavailable("Unknown browser adapter", "unsupported_adapter")
    if not entry["available"]:
        raise AdapterUnavailable(entry["reason"], entry["status"])


async def _close(cleanup, metadata: dict, persistent: bool):
    # Cancellation of the workflow does not cancel orderly shutdown. A bounded
    # failed shutdown explicitly leaves the profile unsafe for publication.
    try:
        async with asyncio.timeout(20):
            await cleanup()
        metadata["profile_saved"] = persistent
        metadata["browser_closed"] = True
        metadata["runtime_stopped"] = True
    except Exception:
        metadata["profile_saved"] = False
        metadata["browser_closed"] = False
        metadata["runtime_stopped"] = False
        metadata["cleanup_error"] = "Browser shutdown could not be confirmed; quarantine profile"


@asynccontextmanager
async def launch_adapter(
    adapter: str,
    proxy_url: str,
    profile_dir: str | None,
    metadata: dict,
    *,
    headless=True,
    remote: dict | None = None,
    expected_compatibility: dict | None = None,
    debug_port: int | None = None,
    proxy_auth: dict | None = None,
    child_env: dict | None = None,
) -> AsyncIterator[BrowserRuntime]:
    if remote is not None:
        if adapter not in {"playwright", "manual_chrome", "cloakbrowser", "kameleo"}:
            raise AdapterUnavailable(
                "This adapter does not support the remote runtime", "unsupported_operation"
            )
        from .remote_browser import remote_adapter

        async with remote_adapter(
            remote, profile_dir, metadata, expected_compatibility, adapter=adapter
        ) as runtime:
            yield runtime
        return
    _check_capability(adapter)
    metadata.update(
        profile_saved=False,
        browser_closed=False,
        runtime_stopped=False,
        wrapper_version=package_version(
            "cloakbrowser"
            if adapter == "cloakbrowser"
            else "kameleo.local-api-client"
            if adapter == "kameleo"
            else "playwright"
        ),
    )
    lock = ProfileLock(profile_dir) if profile_dir else None
    if lock:
        lock.acquire()
    arguments = [*NETWORK_ARGS]
    if debug_port is not None:
        if not 1 <= debug_port <= 65535:
            raise AdapterUnavailable("Invalid internal CDP port")
        arguments.append(f"--remote-debugging-port={debug_port}")
    proxy_settings = {"server": proxy_url, **(proxy_auth or {})}
    try:
        if adapter == "manual_chrome":
            async with _native_chrome(
                proxy_url, profile_dir, metadata, proxy_auth=proxy_auth, child_env=child_env
            ) as runtime:
                yield runtime
            return
        if adapter == "kameleo":
            async with _kameleo(proxy_url, profile_dir, metadata, proxy_auth=proxy_auth) as runtime:
                yield runtime
            return
        if adapter == "cloakbrowser":
            cloak = importlib.import_module("cloakbrowser")
            license_info = await asyncio.to_thread(
                cloak.validate_license, os.environ["CLOAKBROWSER_LICENSE_KEY"]
            )
            if license_info is None or not license_info.valid:
                raise AdapterUnavailable("CloakBrowser license could not be verified", "unverified_license")
            metadata["license_plan"] = license_info.plan
            if os.environ.get("CLOAKBROWSER_VERSION") and license_info.plan != "pro":
                raise AdapterUnavailable(
                    "Exact CloakBrowser version pinning requires Pro", "incompatible_license"
                )
            licensing = importlib.import_module("cloakbrowser.license")
            seats = await asyncio.to_thread(
                licensing.get_session_seats, os.environ["CLOAKBROWSER_LICENSE_KEY"]
            )
            metadata["license_capacity"] = {
                "active": seats.active,
                "limit": seats.limit,
                "state": seats.state,
            }
            if seats.state != "ok":
                raise AdapterUnavailable(
                    "CloakBrowser seat availability could not be verified", "capacity_unverified"
                )
            if seats.limit is not None and seats.active is not None and seats.active >= seats.limit:
                raise AdapterUnavailable("CloakBrowser license capacity is exhausted", "capacity_exhausted")
            kwargs = dict(
                headless=headless,
                proxy=proxy_settings,
                args=arguments + (["--license-route=proxy"] if child_env is not None else []),
                humanize=False,
                geoip=False,
                accept_downloads=False,
                service_workers="block",
            )
            try:
                browser = None
                if profile_dir:
                    if child_env is not None:
                        kwargs["env"] = child_env
                    context = await cloak.launch_persistent_context_async(profile_dir, **kwargs)
                elif child_env is not None:
                    launch_kwargs = {
                        k: v for k, v in kwargs.items() if k not in {"accept_downloads", "service_workers"}
                    }
                    browser = await cloak.launch_async(**launch_kwargs, env=child_env)
                    context = await browser.new_context(accept_downloads=False, service_workers="block")
                else:
                    context = await cloak.launch_context_async(**kwargs)
            except BaseException as exc:
                if browser is not None:
                    await _close(browser.close, metadata, False)
                if not isinstance(exc, RuntimeError):
                    raise
                raise AdapterUnavailable(
                    "CloakBrowser could not acquire its licensed runtime", "license_or_launch_error"
                ) from exc
            try:
                yield BrowserRuntime(context, metadata)
            finally:

                async def cleanup():
                    await context.close()
                    if browser:
                        await browser.close()

                await _close(cleanup, metadata, bool(profile_dir))
            return
        async with async_playwright() as driver:
            kwargs = dict(
                headless=headless,
                chromium_sandbox=True,
                proxy=proxy_settings,
                args=arguments,
                **({"env": child_env} if child_env is not None else {}),
            )
            kwargs["channel"] = "chromium"
            metadata["launch_mode"] = "playwright_launcher"
            browser = None
            if profile_dir:
                context = await driver.chromium.launch_persistent_context(
                    profile_dir, accept_downloads=False, service_workers="block", **kwargs
                )
            else:
                browser = await driver.chromium.launch(**kwargs)
                try:
                    context = await browser.new_context(accept_downloads=False, service_workers="block")
                except BaseException:
                    await browser.close()
                    raise

            async def cleanup():
                await context.close()
                if browser:
                    await browser.close()

            try:
                yield BrowserRuntime(context, metadata)
            finally:
                await _close(cleanup, metadata, bool(profile_dir))
    finally:
        if lock:
            lock.release()


@asynccontextmanager
async def _native_chrome(
    proxy_url: str, profile_dir: str | None, metadata: dict, *, proxy_auth=None, child_env=None
):
    """Native Chrome process; CDP attaches only after normal application launch."""
    directory = profile_dir or tempfile.mkdtemp(prefix="abrl-manual-chrome-")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = browser = None
    clean = False
    metadata.update(
        launch_mode="native_subprocess",
        controller_attachment="CDP observation and operator input",
        ground_truth="human",
        baseline_limitation="CDP and target-scope interception are attached; this is distinct from the unattached manual lab control",
    )
    try:
        process = await asyncio.create_subprocess_exec(
            chrome_path(),
            f"--user-data-dir={directory}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            f"--proxy-server={proxy_url}",
            *NETWORK_ARGS,
            "about:blank",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=child_env,
        )
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            for _ in range(100):
                if process.returncode is not None:
                    raise AdapterUnavailable(
                        "Native Chrome exited before its observation port became ready", "launch_failed"
                    )
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/json/version")
                    if response.is_success:
                        metadata["cdp_endpoint"] = response.json()["webSocketDebuggerUrl"]
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise AdapterUnavailable(
                    "Native Chrome observation port did not become ready", "launch_timeout"
                )
        async with async_playwright() as driver:
            browser = await driver.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            if len(browser.contexts) != 1:
                raise AdapterUnavailable("Native Chrome must expose one dedicated profile context")
            if proxy_auth:
                await _proxy_authentication(browser.contexts[0], proxy_auth)
            try:
                yield BrowserRuntime(browser.contexts[0], metadata)
            finally:
                try:
                    if process.returncode is None:
                        session = await browser.new_browser_cdp_session()
                        await session.send("Browser.close")
                    await asyncio.wait_for(process.wait(), timeout=10)
                    clean = process.returncode == 0
                except Exception:
                    clean = False
                await browser.close()
    finally:
        if process and process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        stopped = process is not None and process.returncode is not None
        metadata.update(
            runtime_stopped=stopped, browser_closed=stopped, profile_saved=bool(profile_dir and clean)
        )
        if profile_dir and not clean:
            metadata["profile_save_failed"] = True
            metadata["cleanup_error"] = "Native Chrome did not confirm a clean profile shutdown"
        if not profile_dir:
            shutil.rmtree(directory, ignore_errors=True)


async def _proxy_authentication(context, proxy_auth):
    """Ephemeral session-broker credentials; never answer an origin challenge."""

    async def authenticate_proxy(page):
        session = await context.new_cdp_session(page)

        async def challenge(params):
            auth = {"response": "Default"}
            if params.get("authChallenge", {}).get("source") == "Proxy":
                auth = {"response": "ProvideCredentials", **proxy_auth}
            await session.send(
                "Fetch.continueWithAuth",
                {
                    "requestId": params["requestId"],
                    "authChallengeResponse": auth,
                },
            )

        async def resume(params):
            await session.send("Fetch.continueRequest", {"requestId": params["requestId"]})

        session.on("Fetch.authRequired", challenge)
        session.on("Fetch.requestPaused", resume)
        await session.send("Fetch.enable", {"handleAuthRequests": True})

    context.on("page", authenticate_proxy)
    for page in context.pages:
        await authenticate_proxy(page)


async def _sdk_call(method, *args, **kwargs):
    """SDK networking is blocking; timeouts also bound cancellation cleanup."""
    kwargs["_request_timeout"] = 15.0
    task = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Don't leave a create/start operation running after cleanup starts.
        result = await task
        # Complete a cancelled creation's rollback before losing the returned ID.
        if method.__name__ in {"create_profile", "import_profile"}:
            owner = method.__self__
            await asyncio.to_thread(owner.delete_profile, result.id, _request_timeout=15.0)
        elif method.__name__ == "start_profile":
            await asyncio.to_thread(method.__self__.stop_profile, args[0], _request_timeout=15.0)
        raise


@asynccontextmanager
async def _kameleo(
    proxy_url: str, profile_dir: str | None, metadata: dict, *, proxy_auth: dict | None = None
):
    from kameleo.local_api_client import KameleoLocalApiClient
    from kameleo.local_api_client.models import (
        BrowserSettings,
        CreateProfileRequest,
        ExportProfileRequest,
        ImportProfileRequest,
    )

    endpoint = os.environ["ABRL_KAMELEO_ENDPOINT"].rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path:
        raise AdapterUnavailable(
            "Kameleo must share the worker network namespace and export volume", "unsupported_operation"
        )
    client = KameleoLocalApiClient(endpoint=endpoint)
    profile = None
    stopped = False
    archive = Path(profile_dir) / "profile.kameleo" if profile_dir else None
    try:
        await _sdk_call(client.general.healthcheck)
        info = await _sdk_call(client.general.get_user_info)
        engine = info.version
        sdk = package_version("kameleo.local-api-client")
        if engine.split(".")[:2] != sdk.split(".")[:2]:
            raise AdapterUnavailable(
                "Kameleo SDK and engine major/minor versions differ", "incompatible_version"
            )
        metadata["engine_version"] = engine
        quota = info.running_profiles.model_dump()
        metadata["license_capacity"] = quota
        for scope in ("user", "tenant"):
            total = quota.get(scope, {}).get("total", {})
            limit = total.get("maximum_limit", total.get("maximumLimit", -1))
            usage = total.get("current_usage", total.get("currentUsage", 0))
            if limit >= 0 and usage >= limit:
                raise AdapterUnavailable(
                    "Kameleo running-profile capacity is exhausted", "capacity_exhausted"
                )
        if archive and archive.exists():
            profile = await _sdk_call(
                client.profile.import_profile,
                import_profile_request=ImportProfileRequest(path=str(archive.resolve())),
            )
        else:
            fingerprints = await _sdk_call(
                client.fingerprint.search_fingerprints, device_type="desktop", browser_product="chrome"
            )
            if not fingerprints:
                raise AdapterUnavailable("No Chroma browser fingerprint is available", "missing_binary")
            profile = await _sdk_call(
                client.profile.create_profile,
                CreateProfileRequest(
                    fingerprint_id=fingerprints[0].id,
                    name="ABRL research session",
                    start_page="about:blank",
                ),
            )
        metadata["vendor_profile_id"] = str(profile.id)
        # Documented start arguments route the browser to our credential-free
        # broker. No stored provider proxy and no automatic external proxy test.
        await _sdk_call(
            client.profile.start_profile,
            profile.id,
            BrowserSettings(arguments=[*NETWORK_ARGS, f"--proxy-server={proxy_url}"]),
        )
        async with async_playwright() as driver:
            browser = await driver.chromium.connect_over_cdp(
                endpoint.replace("http://", "ws://", 1) + f"/playwright/{profile.id}", timeout=90_000
            )
            if len(browser.contexts) != 1:
                raise AdapterUnavailable(
                    "Kameleo must expose exactly one context per profile", "incompatible_version"
                )
            metadata["cdp_endpoint"] = endpoint.replace("http://", "ws://", 1) + f"/playwright/{profile.id}"
            if proxy_auth:
                await _proxy_authentication(browser.contexts[0], proxy_auth)
            try:
                yield BrowserRuntime(browser.contexts[0], metadata)
            finally:
                # Stop the engine profile first. CDP disconnect alone does not
                # guarantee process shutdown or preserve browser state.
                await _sdk_call(client.profile.stop_profile, profile.id)
                stopped = True
                metadata["browser_closed"] = True
                metadata["runtime_stopped"] = True
                await browser.close()
                if archive:
                    temporary = archive.with_suffix(".pending.kameleo")
                    await _sdk_call(
                        client.profile.export_profile,
                        profile.id,
                        export_profile_request=ExportProfileRequest(path=str(temporary.resolve())),
                    )
                    if not temporary.is_file() or temporary.stat().st_size == 0:
                        raise AdapterUnavailable("Kameleo export did not produce a profile archive")
                    os.replace(temporary, archive)
                    metadata["profile_saved"] = True
    except AdapterUnavailable:
        if archive and not metadata.get("profile_saved"):
            metadata["profile_save_failed"] = True
        raise
    except Exception as exc:
        if archive and not metadata.get("profile_saved"):
            metadata["profile_save_failed"] = True
        raise AdapterUnavailable(
            "Kameleo lifecycle failed; inspect the engine's private logs", "engine_error"
        ) from exc
    finally:
        if profile:
            try:
                if not stopped:
                    await _sdk_call(client.profile.stop_profile, profile.id)
                    metadata["browser_closed"] = True
                    metadata["runtime_stopped"] = True
                # Retain the vendor profile when export was uncertain, for recovery.
                if not archive or metadata.get("profile_saved"):
                    await _sdk_call(client.profile.delete_profile, profile.id)
            except Exception:
                metadata["profile_saved"] = False
                metadata["browser_closed"] = False
                metadata["runtime_stopped"] = False
                metadata["cleanup_error"] = "Kameleo cleanup unconfirmed; profile quarantined"

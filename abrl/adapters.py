"""Supported browser lifecycles and browser context options."""

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
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from playwright.async_api import BrowserContext, CDPSession, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .batch_presets import preset_details
from .browser_settings import (
    DevicePreset,
    adapter_contract,
    device_viewport,
    session_capabilities,
    supports_user_agent_override,
)
from .platform_schemas import validate_user_agent
from .profile_storage import ProfileLock, ProfileStorageError

NETWORK_ARGS = [
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    "--proxy-bypass-list=<-loopback>",
]
_LAUNCH_TIMEOUT_MS = 30_000


async def quic_launch_check(session: CDPSession) -> str:
    """Return only the QUIC switch verdict from the running owned fixture browser."""
    try:
        async with asyncio.timeout(1):
            response = await session.send("Browser.getBrowserCommandLine")
    except (PlaywrightError, TimeoutError):
        # Some vendor runtimes restrict this command. A requested flag alone is insufficient evidence.
        return "unavailable"
    arguments = response.get("arguments")
    if not isinstance(arguments, list) or not arguments or not all(isinstance(arg, str) for arg in arguments):
        return "unavailable"
    switches = set()
    for arg in arguments:
        if arg == "--":
            break
        if arg.startswith("--"):
            switches.add(arg.partition("=")[0])
    if "--disable-quic" not in switches or switches & {"--enable-quic", "--origin-to-force-quic-on"}:
        return "mismatch"
    return "passed"


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
    contract = adapter_contract(identifier)
    return {
        "id": identifier,
        "name": name,
        "available": status in {"ready", "configured"},
        "status": status,
        "reason": reason,
        "capabilities": [
            *(capabilities or ["fresh", "persistent", "manual", "automated_preparation"]),
            *session_capabilities(identifier),
        ],
        "features": contract.features.copy() if contract else None,
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
        except (ImportError, OSError, RuntimeError, ValueError):
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
            limitations=["Free tier: one seat and no exact binary pinning"],
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


async def _close_context(context: BrowserContext, browser):
    if browser is None:
        await context.close()
    else:
        # The owned process must be closed even if its disposable context failed.
        with suppress(Exception):
            await context.close()
        await browser.close()


def _launch_failure(browser: str, exc: Exception) -> AdapterUnavailable:
    if isinstance(exc, (PlaywrightTimeout, TimeoutError, httpx.TimeoutException)):
        return AdapterUnavailable(f"{browser} startup exceeded its deadline", "launch_timeout")
    return AdapterUnavailable(f"{browser} could not start an isolated session", "launch_failed")


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
    user_agent: str | None = None,
    device_preset: DevicePreset | None = None,
    batch_preset: dict | None = None,
    kameleo_fixture: bool = False,
) -> AsyncIterator[BrowserRuntime]:
    try:
        user_agent = validate_user_agent(user_agent)
        device = device_viewport(device_preset)
    except ValueError as exc:
        raise AdapterUnavailable(str(exc), "invalid_configuration") from exc
    if user_agent is not None and not supports_user_agent_override(adapter):
        raise AdapterUnavailable("Custom User-Agent requires Playwright Chromium", "unsupported_operation")
    responsive = None
    if batch_preset is not None:
        responsive = preset_details(batch_preset)
        contract = adapter_contract(adapter)
        if (
            responsive is None or contract is None or remote is not None
            or responsive["application"] != contract.features["batch_preset_application"]
            or user_agent is not None or device is not None
        ):
            raise AdapterUnavailable(
                "Batch presets require a compatible local adapter with browser-default identity",
                "invalid_configuration",
            )

    if remote is not None:
        if adapter not in {"playwright", "manual_chrome", "cloakbrowser", "kameleo"}:
            raise AdapterUnavailable(
                "This adapter does not support the remote runtime", "unsupported_operation"
            )
        from .remote_browser import remote_adapter

        metadata["runtime_stopped"] = False
        async with remote_adapter(
            remote,
            profile_dir,
            metadata,
            expected_compatibility,
            adapter=adapter,
            user_agent=user_agent,
            device_preset=device_preset,
        ) as runtime:
            yield runtime
        return
    _check_capability(adapter)
    metadata.update(
        profile_saved=False,
        browser_closed=True,
        runtime_stopped=True,
        wrapper_version=package_version(
            "cloakbrowser"
            if adapter == "cloakbrowser"
            else "kameleo.local-api-client"
            if adapter == "kameleo"
            else "playwright"
        ),
    )
    if (
        expected_compatibility is not None
        and "wrapper_version" in expected_compatibility
        and expected_compatibility["wrapper_version"] != metadata["wrapper_version"]
    ):
        metadata["profile_compatible"] = False
        raise AdapterUnavailable(
            "Persistent profile browser/platform compatibility does not match", "profile_incompatible"
        )
    if expected_compatibility is not None and (not profile_dir or not Path(profile_dir).is_dir()):
        raise AdapterUnavailable("The saved browser profile directory is missing", "profile_restore_failed")
    arguments = [*NETWORK_ARGS]
    if debug_port is not None:
        if not 1 <= debug_port <= 65535:
            raise AdapterUnavailable("Invalid internal CDP port")
        arguments.append(f"--remote-debugging-port={debug_port}")
    proxy_settings = {**(proxy_auth or {}), "server": proxy_url}
    lock = ProfileLock(profile_dir) if profile_dir else None
    if lock:
        try:
            lock.acquire()
        except ProfileStorageError:
            metadata.update(browser_closed=False, runtime_stopped=False)
            raise
        except OSError:
            raise AdapterUnavailable("The browser profile directory could not be opened", "profile_unavailable") from None
    try:
        profile_launch_state = "fresh"
        if profile_dir and adapter != "kameleo":
            state_file = Path(profile_dir, "Local State")
            try:
                restored = not state_file.is_symlink() and state_file.is_file() and state_file.stat().st_size > 0
            except OSError:
                raise AdapterUnavailable("The saved browser profile state could not be read", "profile_restore_failed") from None
            if expected_compatibility is not None and not restored:
                raise AdapterUnavailable("The saved browser profile state is missing or empty", "profile_restore_failed")
            profile_launch_state = "restored" if restored else "initialized"
        if adapter == "manual_chrome":
            async with _native_chrome(
                proxy_url, profile_dir, metadata, proxy_auth=proxy_auth, child_env=child_env
            ) as runtime:
                metadata["profile_launch_state"] = profile_launch_state
                yield runtime
            return
        if adapter == "kameleo":
            async with _kameleo(
                proxy_url,
                profile_dir,
                metadata,
                proxy_auth=proxy_auth,
                fixture=kameleo_fixture,
                restore_profile=expected_compatibility is not None,
            ) as runtime:
                yield runtime
            return
        if adapter == "cloakbrowser":
            license_key = os.environ.get("CLOAKBROWSER_LICENSE_KEY")
            if not license_key:
                raise AdapterUnavailable("CloakBrowser license is not configured", "missing_license")
            try:
                cloak = importlib.import_module("cloakbrowser")
                license_info = await asyncio.to_thread(cloak.validate_license, license_key)
            except (ImportError, OSError, RuntimeError, ValueError, httpx.HTTPError):
                raise AdapterUnavailable("CloakBrowser license could not be verified", "unverified_license") from None
            if license_info is None or not license_info.valid:
                raise AdapterUnavailable("CloakBrowser license could not be verified", "unverified_license")
            metadata["license_plan"] = license_info.plan
            if os.environ.get("CLOAKBROWSER_VERSION") and license_info.plan != "pro":
                raise AdapterUnavailable(
                    "Exact CloakBrowser version pinning requires Pro", "incompatible_license"
                )
            try:
                licensing = importlib.import_module("cloakbrowser.license")
                seats = await asyncio.to_thread(licensing.get_session_seats, license_key)
            except (ImportError, OSError, RuntimeError, ValueError, httpx.HTTPError):
                raise AdapterUnavailable(
                    "CloakBrowser seat availability could not be verified", "capacity_unverified"
                ) from None
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
                args=[*arguments, "--license-route=proxy"],
                humanize=True,
                geoip=True,
                accept_downloads=False,
                service_workers="block",
            )
            browser = None
            try:
                metadata.update(browser_closed=False, runtime_stopped=False)
                if profile_dir:
                    if child_env is not None:
                        kwargs["env"] = child_env
                    context = await cloak.launch_persistent_context_async(
                        profile_dir, timeout=_LAUNCH_TIMEOUT_MS, **kwargs
                    )
                elif child_env is not None:
                    launch_kwargs = {
                        k: v for k, v in kwargs.items() if k not in {"accept_downloads", "service_workers"}
                    }
                    browser = await cloak.launch_async(**launch_kwargs, env=child_env, timeout=_LAUNCH_TIMEOUT_MS)
                    context = await browser.new_context(accept_downloads=False, service_workers="block")
                else:
                    # This helper sends extra keywords to new_context rather than launch.
                    async with asyncio.timeout(_LAUNCH_TIMEOUT_MS / 1000):
                        context = await cloak.launch_context_async(**kwargs)
            except BaseException as exc:
                if browser is not None:
                    await _close(browser.close, metadata, False)
                if isinstance(exc, (PlaywrightError, OSError, RuntimeError, ValueError, httpx.HTTPError)):
                    failure = _launch_failure("CloakBrowser", exc)
                    if failure.status == "launch_failed" and isinstance(exc, RuntimeError):
                        failure = AdapterUnavailable(
                            "CloakBrowser could not acquire its licensed runtime", "license_or_launch_error"
                        )
                    raise failure from None
                raise
            try:
                metadata["profile_launch_state"] = profile_launch_state
                yield BrowserRuntime(context, metadata)
            finally:
                await _close(lambda: _close_context(context, browser), metadata, bool(profile_dir))
            return
        async with AsyncExitStack() as stack:
            kwargs = dict(
                headless=headless,
                chromium_sandbox=True,
                proxy=proxy_settings,
                args=arguments,
                timeout=_LAUNCH_TIMEOUT_MS,
                **({"env": child_env} if child_env is not None else {}),
            )
            kwargs["channel"] = "chromium"
            metadata["launch_mode"] = "playwright_launcher"
            context_options = dict(accept_downloads=False, service_workers="block")
            if user_agent is not None:
                context_options["user_agent"] = user_agent
            if device:
                context_options.update(
                    viewport=device.size,
                    screen=device.size,
                    device_scale_factor=device.scale,
                    is_mobile=True,
                    has_touch=True,
                )
            if responsive is not None:
                context_options.update(
                    viewport=responsive["viewport"],
                    screen=responsive["viewport"],
                    device_scale_factor=responsive["device_pixel_ratio"],
                    locale=responsive["locale"],
                    timezone_id=responsive["timezone"],
                )
            browser = None
            try:
                driver = await stack.enter_async_context(async_playwright())
                metadata.update(browser_closed=False, runtime_stopped=False)
                if profile_dir:
                    context = await driver.chromium.launch_persistent_context(
                        profile_dir, **context_options, **kwargs
                    )
                else:
                    browser = await driver.chromium.launch(**kwargs)
                    context = await browser.new_context(**context_options)
            except BaseException as exc:
                if browser is not None:
                    await _close(browser.close, metadata, False)
                elif isinstance(exc, PlaywrightError) and not metadata["runtime_stopped"]:
                    # A driver-reported startup failure can still own a partially
                    # launched process. Shut the driver down before releasing capacity.
                    await _close(stack.aclose, metadata, False)
                if isinstance(exc, (PlaywrightError, OSError, RuntimeError, ValueError)):
                    raise _launch_failure("Playwright Chromium", exc) from None
                raise

            try:
                metadata["profile_launch_state"] = profile_launch_state
                if user_agent is not None:
                    metadata["configured_user_agent"] = user_agent
                yield BrowserRuntime(context, metadata)
            finally:
                await _close(lambda: _close_context(context, browser), metadata, bool(profile_dir))
    finally:
        if lock:
            try:
                lock.release()
            except OSError:
                metadata["profile_saved"] = False
                metadata.setdefault("cleanup_error", "Browser profile lock release could not be confirmed")


@asynccontextmanager
async def _native_chrome(
    proxy_url: str, profile_dir: str | None, metadata: dict, *, proxy_auth=None, child_env=None
):
    """Native Chrome process; CDP attaches only after normal application launch."""
    executable = chrome_path()
    if executable is None:
        raise AdapterUnavailable("Native Chrome executable is unavailable", "missing_binary")
    directory = profile_dir or tempfile.mkdtemp(prefix="abrl-manual-chrome-")
    process = None
    spawn_failed = True
    clean = False
    metadata.update(
        launch_mode="native_subprocess",
        controller_attachment="CDP observation and operator input",
        ground_truth="human",
        baseline_limitation="CDP and target-scope interception are active",
    )
    try:
        async with AsyncExitStack() as stack:
            try:
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                metadata.update(browser_closed=False, runtime_stopped=False)
                spawn_failed = False
                try:
                    process = await asyncio.create_subprocess_exec(
                        executable,
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
                except OSError:
                    spawn_failed = True
                    raise
                async with asyncio.timeout(10), httpx.AsyncClient(trust_env=False, timeout=1) as client:
                    while True:
                        if process.returncode is not None:
                            raise AdapterUnavailable(
                                "Native Chrome exited before its observation port became ready", "launch_failed"
                            )
                        try:
                            response = await client.get(f"http://127.0.0.1:{port}/json/version")
                        except httpx.TransportError:
                            pass
                        else:
                            if response.is_success:
                                metadata["cdp_endpoint"] = response.json()["webSocketDebuggerUrl"]
                                break
                        await asyncio.sleep(0.1)
                driver = await stack.enter_async_context(async_playwright())
                async with asyncio.timeout(_LAUNCH_TIMEOUT_MS / 1000):
                    browser = await driver.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    if len(browser.contexts) != 1:
                        raise AdapterUnavailable(
                            "Native Chrome must expose one dedicated profile context", "launch_failed"
                        )
                    if proxy_auth:
                        await _proxy_authentication(browser.contexts[0], proxy_auth)
            except AdapterUnavailable:
                raise
            except (PlaywrightError, OSError, RuntimeError, ValueError, KeyError) as exc:
                raise _launch_failure("Native Chrome", exc) from None
            try:
                yield BrowserRuntime(browser.contexts[0], metadata)
            finally:
                try:
                    async with asyncio.timeout(10):
                        session = await browser.new_browser_cdp_session()
                        await session.send("Browser.close")
                        await process.wait()
                        clean = process.returncode == 0
                except Exception:
                    clean = False
                with suppress(Exception), asyncio.timeout(5):
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
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), 3)
        stopped = spawn_failed or (process is not None and process.returncode is not None)
        metadata.update(
            runtime_stopped=stopped, browser_closed=stopped, profile_saved=bool(profile_dir and clean)
        )
        if not stopped:
            metadata["cleanup_error"] = "Native Chrome shutdown could not be confirmed"
        if profile_dir and not clean and not spawn_failed:
            metadata["profile_save_failed"] = True
            metadata.setdefault("cleanup_error", "Native Chrome did not confirm a clean profile shutdown")
        if not profile_dir and stopped:
            await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)


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
    kwargs.setdefault("_request_timeout", 15.0)
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
    proxy_url: str,
    profile_dir: str | None,
    metadata: dict,
    *,
    proxy_auth: dict | None = None,
    fixture: bool = False,
    restore_profile: bool = False,
):
    metadata.update(browser_closed=True, runtime_stopped=True)
    archive = Path(profile_dir).resolve() / "profile.kameleo" if profile_dir else None
    if archive is not None and (archive.exists() or archive.is_symlink()):
        restore_profile = True
    if restore_profile and (
        archive is None or archive.is_symlink() or not archive.is_file() or not archive.stat().st_size
    ):
        raise AdapterUnavailable("The saved Kameleo profile archive is missing or empty", "profile_restore_failed")

    from kameleo.local_api_client import KameleoLocalApiClient
    from kameleo.local_api_client.exceptions import ApiException
    from kameleo.local_api_client.models import (
        BrowserSettings,
        CreateProfileRequest,
        ExportProfileRequest,
        ImportProfileRequest,
        ProxyChoice,
        Server,
        UpdateProfileRequest,
    )
    from urllib3.exceptions import MaxRetryError
    from urllib3.exceptions import TimeoutError as HTTPTimeout

    endpoint = os.environ.get("ABRL_KAMELEO_ENDPOINT", "").rstrip("/")
    try:
        parsed = urlsplit(endpoint)
        engine_port = parsed.port or 80
        broker = urlsplit(proxy_url)
        broker_port = broker.port
    except ValueError:
        raise AdapterUnavailable("Invalid Kameleo Engine or broker endpoint", "invalid_configuration") from None
    if (
        parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.path or parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        raise AdapterUnavailable(
            "Kameleo must share the worker network namespace and export volume", "unsupported_operation"
        )
    if (
        broker.scheme != "http" or broker.hostname != "127.0.0.1" or not broker_port
        or broker.username or broker.password or broker.path or broker.query or broker.fragment
    ):
        raise AdapterUnavailable("Kameleo requires a loopback broker", "unsupported_operation")
    profile_proxy = ProxyChoice(value="http", extra=Server(host=broker.hostname, port=broker_port))
    arguments = []
    for argument in NETWORK_ARGS:
        switch, _, value = argument.partition("=")
        if switch == "--host-resolver-rules":
            argument = f'{switch}="{value}"'
        elif switch == "--proxy-bypass-list":
            # Chroma's startup callback goes to the Engine's own API port.
            argument = f"{argument};127.0.0.1:{engine_port}"
        arguments.append(argument)
    client = KameleoLocalApiClient(endpoint=endpoint)
    profile = None
    profile_operation_started = False
    runtime_delivered = False
    workflow_error = None
    stopped = False
    start_pending = False
    try:
        try:
            await _sdk_call(client.general.healthcheck)
        except (ConnectionError, TimeoutError, MaxRetryError, HTTPTimeout) as exc:
            raise AdapterUnavailable("Kameleo Engine did not respond to its local health check", "engine_unavailable") from exc
        except ApiException as exc:
            if exc.status in {401, 403}:
                raise AdapterUnavailable("Kameleo Engine rejected the local health check", "engine_auth_failed") from None
            if exc.status not in {429, 502, 503, 504}:
                raise
            raise AdapterUnavailable("Kameleo Engine health check is temporarily unavailable", "engine_unavailable") from exc
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
        if restore_profile:
            profile_operation_started = True
            profile = await _sdk_call(
                client.profile.import_profile,
                import_profile_request=ImportProfileRequest(path=str(archive.resolve())),
            )
            await _sdk_call(
                client.profile.update_profile, profile.id, UpdateProfileRequest(proxy=profile_proxy)
            )
        else:
            fingerprints = await _sdk_call(
                client.fingerprint.search_fingerprints, device_type="desktop", browser_product="chrome"
            )
            if not fingerprints:
                raise AdapterUnavailable("No Chroma browser fingerprint is available", "missing_binary")
            profile_operation_started = True
            profile = await _sdk_call(
                client.profile.create_profile,
                CreateProfileRequest(
                    fingerprint_id=fingerprints[0].id,
                    name="ABRL browser session",
                    start_page="about:blank",
                    proxy=profile_proxy,
                ),
            )
        metadata["vendor_profile_id"] = str(profile.id)
        settings = BrowserSettings(arguments=arguments)
        metadata.update(browser_closed=False, runtime_stopped=False)
        start_pending = True
        try:
            await _sdk_call(client.profile.start_profile, profile.id, settings, _request_timeout=30.0)
        except ApiException as exc:
            start_pending = exc.error_code != "validation_failed"
            raise
        start_pending = False
        async with AsyncExitStack() as stack:
            browser = None
            try:
                try:
                    driver = await stack.enter_async_context(async_playwright())
                    cdp_endpoint = endpoint.replace("http://", "ws://", 1) + f"/playwright/{profile.id}"
                    browser = await driver.chromium.connect_over_cdp(cdp_endpoint, timeout=_LAUNCH_TIMEOUT_MS)
                    if len(browser.contexts) != 1:
                        raise AdapterUnavailable(
                            "Kameleo must expose exactly one context per profile", "incompatible_version"
                        )
                    if proxy_auth:
                        await _proxy_authentication(browser.contexts[0], proxy_auth)
                except AdapterUnavailable:
                    raise
                except (PlaywrightError, OSError, RuntimeError, ValueError) as exc:
                    raise _launch_failure("Kameleo CDP", exc) from None
                metadata["cdp_endpoint"] = cdp_endpoint
                metadata["profile_launch_state"] = (
                    "restored" if restore_profile else "initialized" if profile_dir else "fresh"
                )
                runtime_delivered = True
                try:
                    yield BrowserRuntime(browser.contexts[0], metadata)
                except BaseException as exc:
                    workflow_error = exc
                    raise
                finally:
                    # CDP disconnect alone cannot stop the engine-owned profile.
                    await _sdk_call(client.profile.stop_profile, profile.id)
                    stopped = True
                    metadata["browser_closed"] = True
                    metadata["runtime_stopped"] = True
            finally:
                if browser is not None:
                    with suppress(Exception), asyncio.timeout(5):
                        await browser.close()
                if archive and stopped and runtime_delivered:
                    await _sdk_call(client.profile.update_profile, profile.id, UpdateProfileRequest(proxy=None))
                    temporary = archive.with_suffix(f".pending-{uuid4().hex}.kameleo")
                    try:
                        await _sdk_call(
                            client.profile.export_profile,
                            profile.id,
                            export_profile_request=ExportProfileRequest(path=str(temporary)),
                        )
                        if temporary.is_symlink() or not temporary.is_file() or temporary.stat().st_size == 0:
                            raise AdapterUnavailable("Kameleo export did not produce a profile archive")
                        os.replace(temporary, archive)
                    except ApiException as exc:
                        if exc.error_code != "no_capability":
                            raise
                        raise AdapterUnavailable(
                            "Kameleo profile export is unavailable to the configured license",
                            "persistent_export_unavailable",
                        ) from None
                    finally:
                        temporary.unlink(missing_ok=True)
                    metadata["profile_saved"] = True
    except Exception as exc:
        if archive and profile_operation_started and not metadata.get("profile_saved"):
            metadata["profile_save_failed"] = True
        if workflow_error is not None and workflow_error is not exc:
            raise workflow_error from None
        if isinstance(exc, AdapterUnavailable) or workflow_error is exc:
            raise
        raise AdapterUnavailable(
            "Kameleo lifecycle failed; inspect the engine's private logs", "engine_error"
        ) from None
    finally:
        if profile:
            try:
                shutdown_confirmed = True
                if not stopped:
                    try:
                        await _sdk_call(client.profile.stop_profile, profile.id)
                    except Exception:
                        status = await _sdk_call(client.profile.get_profile_status, profile.id)
                        if status.lifetime_state not in {"created", "terminated"}:
                            raise
                        # A timed-out start can still reach the engine after a status read.
                        shutdown_confirmed = not start_pending
                # Keep an unsaved delivered profile for recovery. A startup-only
                # profile is disposable once the Engine confirms shutdown.
                if not archive or not runtime_delivered or metadata.get("profile_saved"):
                    try:
                        await _sdk_call(client.profile.delete_profile, profile.id)
                        shutdown_confirmed = True
                    except Exception as exc:
                        if not shutdown_confirmed or (
                            isinstance(exc, ApiException)
                            and exc.error_code in {"profile_running", "profile_already_running"}
                        ):
                            raise
                        metadata["vendor_profile_cleanup_error"] = (
                            "Kameleo could not delete the stopped profile"
                        )
                if not shutdown_confirmed:
                    raise AdapterUnavailable("Kameleo startup outcome is uncertain")
                metadata.update(browser_closed=True, runtime_stopped=True)
            except Exception:
                metadata["profile_saved"] = False
                metadata["browser_closed"] = False
                metadata["runtime_stopped"] = False
                metadata["cleanup_error"] = "Kameleo cleanup unconfirmed; profile quarantined"

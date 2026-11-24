"""Owned browser diagnostic fixture. This app does not mount the operational API."""

import os
import re
import secrets
from html import escape
from ipaddress import ip_address
from time import time_ns
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

DIAGNOSTIC_PATH = "/lab/diagnostics"
ECHO_PATH = f"{DIAGNOSTIC_PATH}/echo"
DIAGNOSTIC_GROUPS = ("identity", "hints", "environment", "rendering")
DIAGNOSTIC_PROBE_FAILURES = frozenset({
    "echo_fetch_failed", "echo_invalid_json", "echo_invalid_response", "probe_runtime_error",
})
HEADER_LIMITS = {
    "user-agent": 300,
    "accept-language": 240,
    "sec-ch-ua": 180,
    "sec-ch-ua-mobile": 8,
    "sec-ch-ua-platform": 40,
    "sec-ch-ua-full-version-list": 200,
    "sec-ch-ua-platform-version": 40,
}
PROXY_HEADER_NAMES = (
    "proxy-authorization", "proxy-connection", "forwarded", "x-forwarded-for", "x-real-ip", "via",
)
_HINTS = ("Sec-CH-UA-Full-Version-List", "Sec-CH-UA-Platform-Version")
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}
_STUN_URL = re.compile(r"stun:([a-z0-9.-]{1,253})(?::([0-9]{1,5}))?\Z", re.IGNORECASE | re.ASCII)

router = APIRouter(include_in_schema=False)


def _stun_url(request: Request) -> str | None:
    configured = getattr(request.app.state, "diagnostic_stun_url", None)
    if not configured or not (match := _STUN_URL.fullmatch(configured)):
        return None
    hostname, port = match.groups()
    public_origin = getattr(request.app.state, "diagnostic_public_origin", None)
    if public_origin:
        origin = urlsplit(public_origin)
        if origin.scheme != "https" or hostname.lower() != origin.hostname or request.url.hostname != hostname.lower():
            return None
    elif hostname != "127.0.0.1" or request.url.hostname != "127.0.0.1":
        return None
    return configured if port is None or 1 <= int(port) <= 65535 else None


@router.get(ECHO_PATH)
def diagnostic_echo(request: Request) -> JSONResponse:
    headers = {}
    for name, limit in HEADER_LIMITS.items():
        value = request.headers.get(name)
        headers[name] = value if value is not None and len(value) <= limit and value.isprintable() else None
    peer_ip = None
    if _stun_url(request) and request.client:
        try:
            address = ip_address(request.client.host)
            if address.is_global or (address.is_loopback and request.url.hostname == "127.0.0.1"):
                peer_ip = str(address)
        except ValueError:
            pass
    return JSONResponse(
        {
            "version": 1,
            "headers": headers,
            "proxy_header_names": [name for name in PROXY_HEADER_NAMES if name in request.headers],
            "server_epoch_ms": time_ns() // 1_000_000,
            "peer_ip": peer_ip,
        },
        headers=_HEADERS,
    )


@router.get(DIAGNOSTIC_PATH)
def diagnostic_page(request: Request) -> HTMLResponse:
    nonce = secrets.token_urlsafe(18)
    stun = escape(_stun_url(request) or "", quote=True)
    outputs = "".join(f'<pre id="abrl-{group}" hidden></pre>' for group in DIAGNOSTIC_GROUPS)
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ABRL browser diagnostics</title>
<style nonce="{nonce}">pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}</style>
<main><h1>ABRL browser diagnostics</h1><p id="abrl-probe-status" data-stun="{stun}">Collecting diagnostics</p>
{outputs}</main>
<script nonce="{nonce}">{_PROBE_SCRIPT}</script>
</html>""",
        headers={
            **_HEADERS,
            "Accept-CH": ", ".join(_HINTS),
            "Content-Security-Policy": (
                "default-src 'none'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
                f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'"
            ),
        },
    )


def create_app(*, public_origin: str | None = None, stun_url: str | None = None) -> FastAPI:
    fixture = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    fixture.state.diagnostic_public_origin = public_origin
    fixture.state.diagnostic_stun_url = stun_url
    fixture.include_router(router)
    return fixture


_PROBE_SCRIPT = """
const statusLabel = document.querySelector('#abrl-probe-status');
const publish = (group, value) => {
  const node = document.querySelector('#abrl-' + group);
  node.textContent = JSON.stringify(value);
  node.hidden = false;
};
const limited = (value, limit) => typeof value === 'string' && value.length <= limit ? value : null;
const locale = value => limited(value, 35);

function httpProtocol(entry) {
  const value = entry?.nextHopProtocol;
  if (['http/1.0', 'http/1.1', 'h2', 'h3'].includes(value)) return value;
  if (typeof value === 'string' && /^h3-[0-9]+$/.test(value)) return 'h3';
  return 'unavailable';
}

const support = present => present ? 'available' : 'unsupported';
const hasPerformanceEntries = () => typeof window.performance?.getEntriesByType === 'function'
  && typeof window.performance?.getEntriesByName === 'function';

function canvas2dSupport() {
  if (typeof window.HTMLCanvasElement?.prototype?.getContext !== 'function') return 'unsupported';
  try { return document.createElement('canvas').getContext('2d') ? 'available' : 'unavailable'; }
  catch { return 'unavailable'; }
}

function capabilities(webgl) {
  return {
    audio_context: support(typeof window.AudioContext === 'function' || typeof window.webkitAudioContext === 'function'),
    canvas_2d: canvas2dSupport(),
    webgl: webgl !== null ? 'available' : typeof window.WebGLRenderingContext === 'function' ? 'unavailable' : 'unsupported',
    font_loading: support(typeof document.fonts?.check === 'function'),
    performance_observer: support(typeof window.PerformanceObserver === 'function'),
    network_information: support('connection' in navigator),
    service_worker: support('serviceWorker' in navigator),
  };
}

function connectionTiming(entry) {
  const milliseconds = value => Number.isFinite(value) && value >= 0 && value <= 60000 ? Math.round(value) : null;
  const interval = (start, end) => start > 0 && end > start ? milliseconds(end - start) : null;
  const timing = {
    duration: entry && entry.responseEnd > entry.startTime ? milliseconds(entry.responseEnd - entry.startTime) : null,
    request_to_first_byte: entry ? interval(entry.requestStart, entry.responseStart) : null,
    connect: entry ? interval(entry.connectStart, entry.connectEnd) : null,
  };
  return {
    status: !hasPerformanceEntries() ? 'unsupported' :
      Object.values(timing).some(value => value !== null) ? 'observed' : 'unavailable',
    ...timing,
  };
}

function graphics() {
  try {
    const context = document.createElement('canvas').getContext('webgl');
    if (!context) return null;
    const info = context.getExtension('WEBGL_debug_renderer_info');
    return {
      vendor: limited(context.getParameter(info?.UNMASKED_VENDOR_WEBGL ?? context.VENDOR), 150),
      renderer: limited(context.getParameter(info?.UNMASKED_RENDERER_WEBGL ?? context.RENDERER), 250),
      unmasked: Boolean(info),
    };
  } catch { return null; }
}

function canonicalAddress(value) {
  if (!value || !/^[a-f0-9:.]+$/i.test(value)) return null;
  try { return new URL(value.includes(':') ? `http://[${value}]` : `http://${value}`).hostname; }
  catch { return null; }
}

async function iceSummary(stun, peer) {
  if (!stun) return {state: 'not_configured', literal: 0, other: 0, masked: 0};
  const expectedAddress = canonicalAddress(peer);
  if (!window.RTCPeerConnection || !expectedAddress) {
    return {state: 'unavailable', literal: 0, other: 0, masked: 0};
  }
  const result = {state: 'unavailable', literal: 0, other: 0, masked: 0};
  let connection;
  let timer;
  try {
    connection = new RTCPeerConnection({iceServers: [{urls: stun}]});
    const gathering = new Promise(resolve => {
      timer = setTimeout(() => resolve('timeout'), 2500);
      connection.onicecandidate = ({candidate}) => {
        if (!candidate) { resolve('complete'); return; }
        const host = candidate.address || candidate.candidate.split(' ')[4];
        const address = canonicalAddress(host);
        if (address) {
          result.literal++;
          if (address !== expectedAddress) result.other++;
        } else if (host?.endsWith('.local')) {
          result.masked++;
        }
      };
    });
    connection.createDataChannel('abrl-diagnostics');
    await connection.setLocalDescription(await connection.createOffer());
    result.state = await gathering;
  } catch {} finally {
    clearTimeout(timer);
    connection?.close();
  }
  return result;
}

let failureStage = 'echo_fetch_failed';
async function probe() {
  const before = Date.now();
  const response = await fetch('/lab/diagnostics/echo', {cache: 'no-store', credentials: 'omit', redirect: 'error'});
  failureStage = 'echo_invalid_response';
  if (!response.ok) throw new Error('Unexpected echo status');
  failureStage = 'echo_invalid_json';
  const echo = await response.json();
  const after = Date.now();
  failureStage = 'echo_invalid_response';
  if (echo?.version !== 1 || !echo?.headers) throw new Error('Unexpected echo response');
  failureStage = 'probe_runtime_error';
  const {headers} = echo;
  const ua = navigator.userAgentData;
  let high = {};
  try { high = await ua?.getHighEntropyValues(['fullVersionList', 'platformVersion']) ?? {}; } catch {}
  const brands = (high.fullVersionList ?? ua?.brands)?.slice(0, 5).map(({brand, version}) => [
    limited(brand, 35), limited(version, 20),
  ]).filter(pair => pair.every(Boolean)) ?? [];
  publish('identity', {
    server_ua: headers['user-agent'], browser_ua: limited(navigator.userAgent, 300),
    platform: limited(navigator.platform, 45), webdriver: navigator.webdriver ?? null,
    proxy_header_names: echo.proxy_header_names,
  });
  publish('hints', {
    server_brands: headers['sec-ch-ua'], server_full_versions: headers['sec-ch-ua-full-version-list'],
    server_mobile: headers['sec-ch-ua-mobile'], server_platform: headers['sec-ch-ua-platform'],
    server_platform_version: headers['sec-ch-ua-platform-version'],
    browser_brands: brands, browser_full_versions: Array.isArray(high.fullVersionList), browser_mobile: ua?.mobile ?? null,
    browser_platform: limited(ua?.platform, 35), browser_platform_version: limited(high.platformVersion, 35),
  });
  publish('environment', {
    accept_language: headers['accept-language'], language: locale(navigator.language),
    languages: navigator.languages.slice(0, 8).map(locale).filter(Boolean),
    timezone: limited(Intl.DateTimeFormat().resolvedOptions().timeZone, 64),
    offset_minutes: -new Date(after).getTimezoneOffset(),
    before_epoch_ms: before, after_epoch_ms: after, server_epoch_ms: echo.server_epoch_ms,
  });
  const ice = await iceSummary(statusLabel.dataset.stun, echo.peer_ip);
  const documentTiming = hasPerformanceEntries() ? performance.getEntriesByType('navigation')[0] : null;
  const echoTiming = hasPerformanceEntries() ? performance.getEntriesByName(response.url, 'resource').at(-1) : null;
  const webgl = graphics();
  publish('rendering', {
    inner: [innerWidth, innerHeight], screen: [screen.width, screen.height], dpr: devicePixelRatio,
    webgl, ice,
    document_protocol: httpProtocol(documentTiming),
    echo_protocol: httpProtocol(echoTiming),
    capabilities: capabilities(webgl),
    connection_timings_ms: {document: connectionTiming(documentTiming), echo: connectionTiming(echoTiming)},
  });
  statusLabel.textContent = 'Diagnostics complete';
}

probe().catch(() => {
  statusLabel.dataset.failure = failureStage;
  statusLabel.textContent = 'Diagnostics unavailable';
});
"""

app = create_app(
    public_origin=os.environ.get("ABRL_DIAGNOSTIC_URL"), stun_url=os.environ.get("ABRL_DIAGNOSTIC_STUN_URL"),
)

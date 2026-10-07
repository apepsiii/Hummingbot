#!/usr/bin/env python3
"""Local-first web dashboard for the Hummingbot AI scalper.

Reads the bot through the `hbot` CLI and watches the AI brain through MQTT, then serves a
single-page UI with no build step and no CDN dependencies. Built on aiohttp, which the Hummingbot
conda environment already ships, so there is nothing extra to install on the VPS.

    python server.py --demo                 # preview with synthetic telemetry, no bot needed
    python server.py                        # live: wrap the local `hbot` install
    python server.py --host 127.0.0.1 --port 8080 --mqtt-host localhost

A background poller keeps `hbot` output in memory, so HTTP handlers never block on a subprocess
(`hbot status` cold-starts the whole client and can take seconds).

Binding a non-loopback host without a token is refused: this UI can start, stop and retune a bot
that holds real funds. Reach it from your laptop with an SSH tunnel instead:
    ssh -L 8080:127.0.0.1:8080 user@vps
"""
import argparse
import asyncio
import json
import logging
import os
import secrets
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402

import hbot_client as hc  # noqa: E402
from demo_data import demo_bot  # noqa: E402
from signals import SignalFeed  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("webui")


class BotStore:
    """Background poller over the `hbot` CLI, or synthetic telemetry in demo mode."""

    def __init__(self, hbot: str, demo: bool, intervals: Optional[Dict[str, float]] = None):
        self.hbot = hbot
        self.demo = demo
        self.intervals = intervals or {"status": 3.0, "performance": 15.0, "config": 30.0,
                                       "logs": 8.0, "doctor": 300.0}
        self._cache: Dict[str, dict] = {}
        self._locks: Dict[str, asyncio.Lock] = {k: asyncio.Lock() for k in self.intervals}
        self._stop = asyncio.Event()
        self._tasks = []
        self.log_lines = 200
        self._demo = demo_bot() if demo else None

    async def start(self) -> None:
        for kind, interval in self.intervals.items():
            self._tasks.append(asyncio.ensure_future(self._loop(kind, interval)))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _loop(self, kind: str, interval: float) -> None:
        while not self._stop.is_set():
            await self.refresh(kind)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def refresh(self, kind: str) -> dict:
        if self._locks[kind].locked():
            return self.get(kind)
        async with self._locks[kind]:
            entry = await self._fetch(kind)
            entry["fetched_at"] = time.time()
            self._cache[kind] = entry
            return entry

    async def _fetch(self, kind: str) -> dict:
        if self.demo:
            return {"ok": True, "data": self._demo_fetch(kind), "error": None, "returncode": 0}
        runner = {
            "status": self._fetch_status,
            "performance": self._fetch_performance,
            "config": self._fetch_config,
            "logs": self._fetch_logs,
            "doctor": self._fetch_doctor,
        }[kind]
        try:
            return await runner()
        except Exception as e:
            return {"ok": False, "data": None, "error": f"{type(e).__name__}: {e}", "returncode": 1}

    def _demo_fetch(self, kind: str) -> Any:
        self._demo.advance()
        if kind == "status":
            return self._demo.status()
        if kind == "config":
            return self._demo.config()
        if kind == "doctor":
            return self._demo.doctor()
        if kind == "performance":
            rows = self._demo.history()
            return {"rows": rows, "summary": hc.summarize_history(rows)}
        return self._demo.logs(self.log_lines)

    async def _fetch_status(self) -> dict:
        res = await hc.run_hbot_async(["status", "--json"], self.hbot)
        data = hc.read_status(res)
        if data is None:
            return {"ok": False, "data": None, "error": hc.describe(res),
                    "returncode": res["returncode"]}
        return {"ok": True, "data": data, "error": None, "returncode": 0}

    async def _fetch_performance(self) -> dict:
        res = await hc.run_hbot_async(["history"], self.hbot)
        if res["returncode"] != hc.EXIT_SUCCESS:
            return {"ok": False, "data": {"rows": [], "summary": hc.summarize_history([])},
                    "error": hc.describe(res), "returncode": res["returncode"]}
        rows = hc.parse_history(res["stdout"])
        return {"ok": True, "data": {"rows": rows, "summary": hc.summarize_history(rows)},
                "error": None, "returncode": 0}

    async def _fetch_config(self) -> dict:
        res = await hc.run_hbot_async(["config", "--json"], self.hbot)
        data = hc.read_config(res)
        if data is None:
            return {"ok": False, "data": None, "error": hc.describe(res),
                    "returncode": res["returncode"]}
        return {"ok": True, "data": data, "error": None, "returncode": 0}

    async def _fetch_logs(self) -> dict:
        res = await hc.run_hbot_async(["logs", "-n", str(self.log_lines), "--json"], self.hbot)
        data = hc.read_logs(self.log_lines, res)
        if data is None:
            return {"ok": True, "data": {"file": None, "lines": []},
                    "error": hc.describe(res), "returncode": res["returncode"]}
        return {"ok": True, "data": data, "error": None, "returncode": 0}

    async def _fetch_doctor(self) -> dict:
        res = await hc.run_hbot_async(["doctor", "--json"], self.hbot)
        data = hc.read_doctor(res)
        if data is None:
            return {"ok": False, "data": None, "error": hc.describe(res),
                    "returncode": res["returncode"]}
        return {"ok": True, "data": data, "error": None, "returncode": 0}

    def get(self, kind: str) -> dict:
        return self._cache.get(kind, {"ok": False, "data": None, "error": "not fetched yet",
                                      "returncode": None, "fetched_at": None})

    def thresholds(self) -> Dict[str, float]:
        strategy = (self.get("config").get("data") or {}).get("strategy") or {}
        fields = strategy.get("fields") or {}
        out = {}
        for key in ("long_threshold", "short_threshold"):
            value = hc.to_number(fields.get(key), float)
            if value is not None:
                out[key] = value
        return out

    def signal_timeout(self) -> float:
        fields = ((self.get("config").get("data") or {}).get("strategy") or {}).get("fields") or {}
        return hc.to_number(fields.get("signal_timeout"), float) or 30.0

    def live_fields(self) -> list:
        strategy = (self.get("config").get("data") or {}).get("strategy") or {}
        return list(strategy.get("live_fields") or [])


class App:
    def __init__(self, args):
        self.args = args
        self.store = BotStore(args.hbot, args.demo)
        self.feed = SignalFeed(host=args.mqtt_host, port=args.mqtt_port,
                               username=args.mqtt_username, password=args.mqtt_password,
                               ssl=args.mqtt_ssl, enabled=not args.no_mqtt and not args.demo)
        self.token = args.token or os.environ.get("WEBUI_TOKEN") or ""
        self.started_at = time.time()
        self.demo_feed_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ helpers

    def mode(self) -> str:
        return "demo" if self.args.demo else "live"

    def authorized(self, request: web.Request) -> bool:
        if not self.token:
            return True
        supplied = request.headers.get("X-WebUI-Token") or request.query.get("token") or ""
        return secrets.compare_digest(supplied, self.token)

    def ai_state(self) -> dict:
        """Merge the MQTT feed with a demo generator so the panel is populated either way."""
        if self.args.demo:
            return {"pairs": {"BTC-USDT": demo_bot().signal()}, "feed": {
                "enabled": False, "connected": False, "broker": "demo (no MQTT)",
                "topic_filter": "hbot/predictions/#", "message_count": 0, "malformed": 0,
                "last_error": None, "last_message_age_s": None, "pairs": []}}
        enriched = self.feed.enrich(self.store.thresholds(), self.store.signal_timeout())
        return {"pairs": enriched, "feed": self.feed.snapshot(),
                "series": {pair: self.feed.series_for(pair, 120) for pair in enriched}}

    # ------------------------------------------------------------------ handlers

    async def index(self, request: web.Request) -> web.Response:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "mode": self.mode(),
            "hbot": self.store.hbot,
            "hbot_found": bool(self.args.demo or shutil.which(self.store.hbot)),
            "token_required": bool(self.token),
            "mqtt": self.feed.snapshot(),
            "server_uptime_s": round(time.time() - self.started_at, 1),
            "poll_intervals": self.store.intervals,
        })

    async def state(self, request: web.Request) -> web.Response:
        status = self.store.get("status")
        return web.json_response({
            "mode": self.mode(),
            "bot": status.get("data"),
            "bot_ok": status.get("ok", False),
            "bot_error": status.get("error"),
            "fetched_at": status.get("fetched_at"),
            "ai": self.ai_state(),
            "live_fields": self.store.live_fields(),
        }, dumps=lambda o: json.dumps(o, default=str))

    async def performance(self, request: web.Request) -> web.Response:
        entry = self.store.get("performance")
        return web.json_response({"ok": entry.get("ok"), "data": entry.get("data"),
                                  "error": entry.get("error"), "fetched_at": entry.get("fetched_at")},
                                 dumps=lambda o: json.dumps(o, default=str))

    async def config(self, request: web.Request) -> web.Response:
        entry = self.store.get("config")
        data = entry.get("data") or {}
        live = set(self.store.live_fields())
        fields = (data.get("strategy") or {}).get("fields") or {}
        tunables = []
        for key in sorted(live):
            if key in hc.BOOL_KEYS:
                item = {"key": key, "kind": "bool", "min": None, "max": None}
            elif key in hc.ENUM_CHOICES:
                item = {"key": key, "kind": "enum", "choices": hc.ENUM_CHOICES[key],
                        "min": None, "max": None}
            elif key in hc.NUMERIC_BOUNDS:
                low, high = hc.NUMERIC_BOUNDS[key]
                item = {"key": key, "kind": "number", "min": low, "max": high}
            elif key == "total_amount_quote":
                item = {"key": key, "kind": "number", "min": 0, "max": self.args.max_amount}
            else:
                continue
            item["value"] = fields.get(key)
            tunables.append(item)
        return web.json_response({
            "ok": entry.get("ok"), "error": entry.get("error"), "fetched_at": entry.get("fetched_at"),
            "global": data.get("global") or {}, "strategy": data.get("strategy") or {},
            "tunables": tunables,
            "readonly_live_fields": sorted(live - {t["key"] for t in tunables}),
        }, dumps=lambda o: json.dumps(o, default=str))

    async def logs(self, request: web.Request) -> web.Response:
        entry = self.store.get("logs")
        return web.json_response({"ok": entry.get("ok"), "data": entry.get("data"),
                                  "error": entry.get("error"), "fetched_at": entry.get("fetched_at")},
                                 dumps=lambda o: json.dumps(o, default=str))

    async def doctor(self, request: web.Request) -> web.Response:
        entry = self.store.get("doctor")
        return web.json_response({"ok": entry.get("ok"), "data": entry.get("data"),
                                  "error": entry.get("error"), "fetched_at": entry.get("fetched_at")},
                                 dumps=lambda o: json.dumps(o, default=str))

    async def series(self, request: web.Request) -> web.Response:
        pair = (request.query.get("pair") or "").upper()
        if not pair and self.feed.latest:
            pair = next(iter(self.feed.latest))
        limit = min(240, max(1, hc.to_number(request.query.get("limit"), int) or 120))
        return web.json_response({"pair": pair, "series": self.feed.series_for(pair, limit)})

    # ---- mutations

    async def _guard(self, request: web.Request) -> Optional[web.Response]:
        if self.args.demo:
            return web.json_response({"ok": False, "error": "demo mode: bot control is disabled"},
                                     status=403)
        if not self.authorized(request):
            return web.json_response({"ok": False, "error": "missing or wrong X-WebUI-Token"},
                                     status=401)
        return None

    async def _body(self, request: web.Request) -> dict:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    async def set_config(self, request: web.Request) -> web.Response:
        blocked = await self._guard(request)
        if blocked:
            return blocked
        body = await self._body(request)
        key, value = body.get("key"), body.get("value")
        live = set(self.store.live_fields())
        if live and key not in live:
            return web.json_response({"ok": False,
                                      "error": f"{key} is not live-updatable in the running config"},
                                     status=409)
        res = await asyncio.get_running_loop().run_in_executor(
            None, hc.set_config, key, value, self.store.hbot, self.args.max_amount)
        await self.store.refresh("config")
        log.info("config %s=%s -> %s", key, value, hc.describe(res))
        return web.json_response({"ok": res["returncode"] == hc.EXIT_SUCCESS,
                                  "returncode": res["returncode"], "detail": hc.describe(res)})

    async def start_bot(self, request: web.Request) -> web.Response:
        blocked = await self._guard(request)
        if blocked:
            return blocked
        body = await self._body(request)
        config = body.get("config") or None
        res = await asyncio.get_running_loop().run_in_executor(
            None, hc.start_bot, config, self.store.hbot)
        await self.store.refresh("status")
        log.info("start(%s) -> %s", config or "loaded", hc.describe(res))
        return web.json_response({"ok": res["returncode"] == hc.EXIT_SUCCESS,
                                  "returncode": res["returncode"], "detail": hc.describe(res)})

    async def stop_bot(self, request: web.Request) -> web.Response:
        blocked = await self._guard(request)
        if blocked:
            return blocked
        body = await self._body(request)
        force = bool(body.get("force"))
        res = await asyncio.get_running_loop().run_in_executor(
            None, hc.stop_bot, self.store.hbot, force)
        await asyncio.gather(self.store.refresh("status"), self.store.refresh("performance"))
        log.info("stop(force=%s) -> %s", force, hc.describe(res))
        return web.json_response({"ok": res["returncode"] == hc.EXIT_SUCCESS,
                                  "returncode": res["returncode"], "detail": hc.describe(res)})

    async def kill(self, request: web.Request) -> web.Response:
        blocked = await self._guard(request)
        if blocked:
            return blocked
        res = await asyncio.get_running_loop().run_in_executor(None, hc.kill_switch, self.store.hbot)
        await asyncio.gather(self.store.refresh("status"), self.store.refresh("config"))
        log.warning("KILL SWITCH -> %s", hc.describe(res))
        return web.json_response({"ok": res["returncode"] == hc.EXIT_SUCCESS,
                                  "returncode": res["returncode"], "detail": hc.describe(res)})


def build_app(state: App) -> web.Application:
    app = web.Application()

    async def on_startup(app: web.Application) -> None:
        await state.store.start()
        await state.feed.start()

    async def on_cleanup(app: web.Application) -> None:
        await state.feed.stop()
        await state.store.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/", state.index)
    app.router.add_static("/static/", path=str(STATIC_DIR), name="static")
    app.router.add_get("/api/health", state.health)
    app.router.add_get("/api/state", state.state)
    app.router.add_get("/api/performance", state.performance)
    app.router.add_get("/api/config", state.config)
    app.router.add_get("/api/logs", state.logs)
    app.router.add_get("/api/doctor", state.doctor)
    app.router.add_get("/api/signals/series", state.series)
    app.router.add_post("/api/config", state.set_config)
    app.router.add_post("/api/start", state.start_bot)
    app.router.add_post("/api/stop", state.stop_bot)
    app.router.add_post("/api/kill", state.kill)
    app["state"] = state
    return app


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Web dashboard for the Hummingbot AI scalper.")
    p.add_argument("--host", default=os.environ.get("WEBUI_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("WEBUI_PORT", "8080")))
    p.add_argument("--hbot", default=hc.find_hbot())
    p.add_argument("--demo", action="store_true",
                   help="serve synthetic telemetry; no hbot, no MQTT, no bot control")
    p.add_argument("--token", default=None, help="required for mutating calls (or WEBUI_TOKEN)")
    p.add_argument("--allow-insecure", action="store_true",
                   help="permit a non-loopback bind with no token (not recommended)")
    p.add_argument("--max-amount", type=float, default=hc.DEFAULT_MAX_AMOUNT,
                   help="cap for total_amount_quote edits from the UI")
    p.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "localhost"))
    p.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    p.add_argument("--mqtt-username", default=os.environ.get("MQTT_USERNAME", ""))
    p.add_argument("--mqtt-password", default=os.environ.get("MQTT_PASSWORD", ""))
    p.add_argument("--mqtt-ssl", action="store_true",
                   default=os.environ.get("MQTT_SSL", "") == "true")
    p.add_argument("--no-mqtt", action="store_true", help="disable the MQTT signal feed")
    p.add_argument("--log-lines", type=int, default=200)
    return p.parse_args(argv)


def _utf8_streams() -> None:
    """Windows consoles default to cp1252; hbot output can contain emoji (gateway status).

    Re-encode instead of raising UnicodeEncodeError deep inside a logging handler.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv=None) -> int:
    _utf8_streams()
    args = parse_args(argv)
    loopback = args.host in LOOPBACK
    if not loopback and not (args.token or os.environ.get("WEBUI_TOKEN")) and not args.allow_insecure:
        log.error("refusing to bind %s without a token: this UI can start, stop and retune a bot "
                  "holding real funds. Pass --token (or set WEBUI_TOKEN), or reach it over an SSH "
                  "tunnel: ssh -L %s:127.0.0.1:%s user@vps", args.host, args.port, args.port)
        return 2

    state = App(args)
    state.store.log_lines = max(1, min(args.log_lines, 5000))
    if args.demo:
        log.warning("DEMO mode: synthetic telemetry, bot control disabled")

    banner = [
        "",
        f"  AI scalper dashboard  ->  http://{args.host}:{args.port}",
        f"  mode: {state.mode()}    hbot: {args.hbot}",
        f"  mqtt: {'off' if (args.no_mqtt or args.demo) else args.mqtt_host + ':' + str(args.mqtt_port)}",
    ]
    if state.token:
        banner.append(f"  token: {state.token}   (send as X-WebUI-Token, or open "
                      f"?token={state.token})")
    elif loopback:
        banner.append("  token: none — loopback bind only, do not expose this port")
    banner.append("")
    print("\n".join(banner))

    web.run_app(build_app(state), host=args.host, port=args.port, print=None,
                access_log=log if os.environ.get("WEBUI_ACCESS_LOG") else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())

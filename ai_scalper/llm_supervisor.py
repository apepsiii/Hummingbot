#!/usr/bin/env python3
"""Meta layer for the AI scalper: an LLM supervisor that watches the bot and retunes it live.

The ML publisher owns the fast decisions (sub-second signals). This script owns the slow ones:
read `hbot status --json` / `hbot history`, judge whether the strategy is actually making money
after fees, then push bounded edits to live-updatable controller fields and trip a kill-switch
when drawdown or error rate crosses a limit.

Everything is dry-run by default. Nothing is written unless you pass --apply.

    python llm_supervisor.py --once                      # print the intended actions
    python llm_supervisor.py --every 300 --apply         # supervised loop
    python llm_supervisor.py --once --no-llm             # deterministic rules only
"""
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("llm_supervisor")

NUMERIC_BOUNDS: Dict[str, tuple] = {
    "long_threshold": (0.50, 0.95),
    "short_threshold": (0.50, 0.95),
    "signal_timeout": (5, 600),
    "sl_multiplier": (0.5, 5.0),
    "tp_multiplier": (0.5, 5.0),
    "min_barrier": (0.0005, 0.05),
    "max_barrier": (0.002, 0.10),
    "min_size_scale": (0.05, 1.0),
    "cooldown_time": (10, 3600),
    "max_executors_per_side": (1, 10),
}
BOOL_KEYS = {"close_on_stale_signal", "confidence_sizing", "manual_kill_switch"}

SYSTEM_PROMPT = (
    "You supervise an automated crypto scalping bot. You never place orders. You only propose "
    "bounded edits to live-updatable config keys, or request a stop. Judge performance on PnL "
    "AFTER fees, not on win rate or signal accuracy. Prefer doing nothing over frequent tuning: "
    "each change invalidates the sample you are measuring. Respond with JSON only, matching "
    '{"actions": [{"key": str, "value": number|bool, "reason": str}], "stop": bool, '
    '"rationale": str}. Only use keys from the allowed list; never invent keys or exceed bounds.'
)


def run_hbot(args: List[str], hbot: str = "hbot", timeout: float = 90.0) -> Dict[str, Any]:
    """Run one hbot command; return {returncode, stdout, stderr} without raising."""
    try:
        proc = subprocess.run([hbot, *args], capture_output=True, text=True, timeout=timeout)
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except FileNotFoundError:
        return {"returncode": 127, "stdout": "", "stderr": f"{hbot} not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"returncode": 5, "stdout": "", "stderr": f"hbot {' '.join(args)} timed out"}


def read_status(hbot: str) -> Optional[dict]:
    res = run_hbot(["status", "--json"], hbot)
    if res["returncode"] != 0:
        log.warning("hbot status failed (code %s): %s", res["returncode"], res["stderr"].strip())
        return None
    try:
        return json.loads(res["stdout"])
    except json.JSONDecodeError:
        log.warning("unparseable status JSON: %s", res["stdout"][:200])
        return None


def parse_history(markdown: str) -> List[dict]:
    """Parse the `hbot history` Markdown table into records."""
    rows = [ln for ln in markdown.splitlines() if ln.startswith("|")]
    if len(rows) < 3:
        return []
    header = [c.strip() for c in rows[0].strip("|").split("|")]
    out = []
    for line in rows[2:]:
        values = [c.strip() for c in line.strip("|").split("|")]
        if len(values) != len(header) or all(v == "" for v in values):
            continue
        record = dict(zip(header, values))
        for key in ("trades", "buys", "sells"):
            record[key] = _to_number(record.get(key), int)
        for key in ("base_vol", "quote_vol", "trade_pnl", "fees", "total_pnl", "return%"):
            record[key] = _to_number(record.get(key), float)
        out.append(record)
    return out


def _to_number(value: Any, caster) -> Optional[Any]:
    if value is None or value == "":
        return None
    try:
        return caster(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def summarize(status: Optional[dict], history_rows: List[dict]) -> dict:
    pnl = sum(r["total_pnl"] or 0.0 for r in history_rows)
    fees = sum(r["fees"] or 0.0 for r in history_rows)
    trades = sum(r["trades"] or 0 for r in history_rows)
    gross = sum(r["trade_pnl"] or 0.0 for r in history_rows)
    errors = (status or {}).get("errors") or {}
    return {
        "running": bool((status or {}).get("running")),
        "uptime_s": (status or {}).get("uptime_s"),
        "strategy": (status or {}).get("strategy"),
        "error_count": errors.get("count", 0),
        "error_messages": errors.get("messages", []),
        "markets": history_rows,
        "trades": trades,
        "gross_pnl": round(gross, 6),
        "fees": round(fees, 6),
        "net_pnl": round(pnl, 6),
        "fee_ratio": round(fees / gross, 4) if gross else None,
    }


def rule_based_actions(summary: dict, max_drawdown: float, max_errors: int) -> Dict[str, Any]:
    """Deterministic guardrails — also the fallback when no LLM is configured."""
    if summary["error_count"] >= max_errors:
        return {"actions": [], "stop": True,
                "rationale": f"{summary['error_count']} errors in the log window"}
    if summary["net_pnl"] <= -abs(max_drawdown):
        return {"actions": [], "stop": True,
                "rationale": f"net PnL {summary['net_pnl']} breached drawdown limit {max_drawdown}"}
    actions = []
    if summary["trades"] and summary["fees"] and summary["gross_pnl"]:
        if summary["fees"] >= abs(summary["gross_pnl"]):
            actions.append({"key": "long_threshold", "value": 0.75,
                            "reason": "fees exceed gross PnL; demand higher conviction per trade"})
            actions.append({"key": "short_threshold", "value": 0.75,
                            "reason": "fees exceed gross PnL; demand higher conviction per trade"})
            actions.append({"key": "cooldown_time", "value": 180,
                            "reason": "cut trade frequency to lower fee drag"})
    return {"actions": actions, "stop": False, "rationale": "rule-based guardrails"}


def call_llm(summary: dict, allowed: Dict[str, tuple]) -> Optional[Dict[str, Any]]:
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        return None
    base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    import requests
    user = (
        "Bot telemetry:\n" + json.dumps(summary, indent=2, default=str) +
        "\n\nAllowed keys with [min, max] bounds:\n" + json.dumps(allowed, indent=2) +
        "\n\nBoolean keys: " + ", ".join(sorted(BOOL_KEYS)) +
        "\n\nPropose at most 3 edits, or none. Reply with JSON only."
    )
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "temperature": 0.1,
                  "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                               {"role": "user", "content": user}]},
            timeout=60.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(re.search(r"\{.*\}", content, re.S).group(0))
    except Exception as e:
        log.warning("LLM decision unavailable (%s); falling back to rules", e)
        return None


def sanitize(decision: Dict[str, Any], allowed: Dict[str, tuple], max_amount: float) -> List[dict]:
    """Drop anything not whitelisted or out of bounds — the LLM is untrusted input."""
    safe = []
    for item in decision.get("actions") or []:
        if not isinstance(item, dict):
            continue
        key, value = item.get("key"), item.get("value")
        if key in BOOL_KEYS:
            if isinstance(value, bool) or str(value).lower() in ("true", "false"):
                safe.append({"key": key, "value": str(value).lower() in ("true", "1") or value is True,
                             "reason": item.get("reason", "")})
            continue
        if key == "total_amount_quote":
            amount = _to_number(value, float)
            if amount is None or amount <= 0 or amount > max_amount:
                log.warning("rejected total_amount_quote=%s (cap %s)", value, max_amount)
                continue
            safe.append({"key": key, "value": amount, "reason": item.get("reason", "")})
            continue
        if key not in allowed:
            log.warning("rejected unknown key: %s", key)
            continue
        number = _to_number(value, float)
        low, high = allowed[key]
        if number is None or not (low <= number <= high):
            log.warning("rejected %s=%s outside [%s, %s]", key, value, low, high)
            continue
        safe.append({"key": key, "value": number, "reason": item.get("reason", "")})
    return safe[:3]


def apply_actions(actions: List[dict], hbot: str) -> None:
    for action in actions:
        value = str(action["value"]).lower() if isinstance(action["value"], bool) else str(action["value"])
        res = run_hbot(["config", action["key"], value], hbot)
        level = logging.INFO if res["returncode"] == 0 else logging.ERROR
        log.log(level, "hbot config %s %s -> code %s %s", action["key"], value,
                res["returncode"], (res["stderr"] or res["stdout"]).strip()[:160])


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LLM/rule-based supervisor for the ai_scalper controller.")
    p.add_argument("--hbot", default=shutil.which("hbot") or "hbot")
    p.add_argument("--every", type=float, default=300.0, help="seconds between supervision cycles")
    p.add_argument("--once", action="store_true")
    p.add_argument("--apply", action="store_true", help="actually write config changes / stop the bot")
    p.add_argument("--no-llm", action="store_true", help="skip the LLM, use deterministic rules only")
    p.add_argument("--max-drawdown", type=float, default=25.0,
                   help="net loss in quote units that trips the kill-switch")
    p.add_argument("--max-errors", type=int, default=20, help="log errors that trip the kill-switch")
    p.add_argument("--max-amount", type=float, default=200.0,
                   help="hard cap the supervisor may set total_amount_quote to")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    while True:
        status = read_status(args.hbot)
        if status is None:
            log.error("cannot reach the bot; is it started and is `hbot` on PATH?")
            if args.once:
                return 1
        else:
            history = run_hbot(["history"], args.hbot)
            rows = parse_history(history["stdout"]) if history["returncode"] == 0 else []
            summary = summarize(status, rows)
            decision = None if args.no_llm else call_llm(summary, NUMERIC_BOUNDS)
            if decision is None:
                decision = rule_based_actions(summary, args.max_drawdown, args.max_errors)
                decision["source"] = "rules"
            else:
                decision["source"] = "llm"
            actions = sanitize(decision, NUMERIC_BOUNDS, args.max_amount)

            log.info("source=%s stop=%s rationale=%s",
                     decision.get("source"), decision.get("stop"), decision.get("rationale"))
            for action in actions:
                log.info("  propose %s=%s (%s)", action["key"], action["value"], action["reason"])

            if args.apply:
                if actions:
                    apply_actions(actions, args.hbot)
                if decision.get("stop"):
                    res = run_hbot(["stop"], args.hbot)
                    log.info("kill-switch: hbot stop -> code %s", res["returncode"])
            elif actions or decision.get("stop"):
                log.info("dry-run only; re-run with --apply to execute")

        if args.once:
            return 0
        time.sleep(max(10.0, args.every))


if __name__ == "__main__":
    sys.exit(main())

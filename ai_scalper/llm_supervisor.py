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
import sys
import time
from typing import Any, Dict, List, Optional

from hbot_client import (BOOL_KEYS, NUMERIC_BOUNDS, describe, find_hbot, kill_switch,
                         parse_history, read_history, read_status, run_hbot, set_config,
                         summarize_history, validate_tunable)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("llm_supervisor")

MAX_ACTIONS_PER_CYCLE = 3

SYSTEM_PROMPT = (
    "You supervise an automated crypto scalping bot. You never place orders. You only propose "
    "bounded edits to live-updatable config keys, or request a stop. Judge performance on PnL "
    "AFTER fees, not on win rate or signal accuracy. Prefer doing nothing over frequent tuning: "
    "each change invalidates the sample you are measuring. Respond with JSON only, matching "
    '{"actions": [{"key": str, "value": number|bool, "reason": str}], "stop": bool, '
    '"rationale": str}. Only use keys from the allowed list; never invent keys or exceed bounds.'
)


def summarize(status: Optional[dict], history_rows: List[dict]) -> dict:
    metrics = summarize_history(history_rows)
    errors = (status or {}).get("errors") or {}
    return {
        "running": bool((status or {}).get("running")),
        "uptime_s": (status or {}).get("uptime_s"),
        "strategy": (status or {}).get("strategy"),
        "error_count": errors.get("count", 0),
        "error_messages": errors.get("messages", []),
        "markets": history_rows,
        **metrics,
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
            reason = "fees exceed gross PnL; demand higher conviction per trade"
            actions.append({"key": "long_threshold", "value": 0.75, "reason": reason})
            actions.append({"key": "short_threshold", "value": 0.75, "reason": reason})
            actions.append({"key": "cooldown_time", "value": 180,
                            "reason": "cut trade frequency to lower fee drag"})
    return {"actions": actions, "stop": False, "rationale": "rule-based guardrails"}


def call_llm(summary: dict) -> Optional[Dict[str, Any]]:
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        return None
    base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    import requests
    user = (
        "Bot telemetry:\n" + json.dumps(summary, indent=2, default=str) +
        "\n\nAllowed numeric keys with [min, max] bounds:\n" + json.dumps(NUMERIC_BOUNDS, indent=2) +
        "\n\nBoolean keys: " + ", ".join(sorted(BOOL_KEYS)) +
        f"\n\nPropose at most {MAX_ACTIONS_PER_CYCLE} edits, or none. Reply with JSON only."
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


def sanitize(decision: Dict[str, Any], max_amount: float) -> List[dict]:
    """Drop anything not whitelisted or out of bounds — the LLM's output is untrusted input."""
    safe = []
    for item in decision.get("actions") or []:
        if not isinstance(item, dict):
            continue
        key, raw = item.get("key"), item.get("value")
        ok, value, reason = validate_tunable(key, raw, max_amount)
        if not ok:
            log.warning("rejected %s=%s: %s", key, raw, reason)
            continue
        safe.append({"key": key, "value": value, "reason": item.get("reason", "")})
    return safe[:MAX_ACTIONS_PER_CYCLE]


def apply_actions(actions: List[dict], hbot: str) -> None:
    for action in actions:
        res = set_config(action["key"], action["value"], hbot)
        level = logging.INFO if res["returncode"] == 0 else logging.ERROR
        log.log(level, "hbot config %s=%s -> code %s %s",
                action["key"], action["value"], res["returncode"], describe(res))


def collect(args) -> Optional[dict]:
    status = read_status(run_hbot(["status", "--json"], args.hbot))
    if status is None:
        log.error("cannot reach the bot; is it started and is `hbot` on PATH?")
        return None
    rows = read_history(run_hbot(["history"], args.hbot))
    return summarize(status, rows)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LLM/rule-based supervisor for the ai_scalper controller.")
    p.add_argument("--hbot", default=find_hbot())
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
        summary = collect(args)
        if summary is not None:
            decision = None if args.no_llm else call_llm(summary)
            if decision is None:
                decision = rule_based_actions(summary, args.max_drawdown, args.max_errors)
                decision["source"] = "rules"
            else:
                decision["source"] = "llm"
            actions = sanitize(decision, args.max_amount)

            log.info("source=%s stop=%s rationale=%s",
                     decision.get("source"), decision.get("stop"), decision.get("rationale"))
            for action in actions:
                log.info("  propose %s=%s (%s)", action["key"], action["value"], action["reason"])

            if args.apply:
                if actions:
                    apply_actions(actions, args.hbot)
                if decision.get("stop"):
                    res = kill_switch(args.hbot)
                    log.info("kill-switch -> code %s %s", res["returncode"], describe(res))
            elif actions or decision.get("stop"):
                log.info("dry-run only; re-run with --apply to execute")

        if args.once:
            return 0 if summary is not None else 1
        time.sleep(max(10.0, args.every))


if __name__ == "__main__":
    sys.exit(main())

"""Shared wrapper around the `hbot` CLI — the single source of truth for its contract.

Used by both `llm_supervisor.py` (autonomous retuning) and `webui/server.py` (human dashboard), so
the command surface, JSON shapes, Markdown parsing and tunable-key guardrails are defined once.

Exit codes are stable (`hummingbot/cli/output.py::ExitCode`):
0 SUCCESS, 1 ERROR, 2 NOT_FOUND, 3 NOT_RUNNING, 4 CONFIG_ERROR, 5 TIMEOUT, 127 hbot missing.
"""
import asyncio
import json
import logging
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("hbot_client")

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 2
EXIT_NOT_RUNNING = 3
EXIT_CONFIG_ERROR = 4
EXIT_TIMEOUT = 5
EXIT_MISSING = 127

READ_TIMEOUT = 90.0
WRITE_TIMEOUT = 120.0

# Keys the supervisor and the web UI may write, with hard bounds. Anything not listed here is
# refused even if the controller declares it live-updatable: `connector_name` or `leverage` would
# be technically writable but must never be changed by automation or a stray click.
NUMERIC_BOUNDS: Dict[str, Tuple[float, float]] = {
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
    "time_limit": (30, 86400),
    "stop_loss": (0.0005, 0.20),
    "take_profit": (0.0005, 0.20),
}
BOOL_KEYS = {"close_on_stale_signal", "confidence_sizing", "manual_kill_switch"}
# OrderType members (hummingbot/core/data_type/common.py). stop_loss/time_limit order types are
# deliberately absent: TripleBarrierConfig rejects anything but MARKET for those barriers.
ENUM_CHOICES: Dict[str, List[str]] = {
    "entry_order_type": ["MARKET", "LIMIT", "LIMIT_MAKER"],
    "take_profit_order_type": ["MARKET", "LIMIT", "LIMIT_MAKER"],
}
DEFAULT_MAX_AMOUNT = 200.0


def find_hbot(explicit: Optional[str] = None) -> str:
    return explicit or shutil.which("hbot") or "hbot"


def _result(returncode: int, stdout: str, stderr: str) -> Dict[str, Any]:
    return {"returncode": returncode, "stdout": stdout or "", "stderr": stderr or ""}


def run_hbot(args: List[str], hbot: Optional[str] = None,
             timeout: float = READ_TIMEOUT) -> Dict[str, Any]:
    """Run one hbot command synchronously; never raises."""
    binary = find_hbot(hbot)
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
        return _result(proc.returncode, proc.stdout, proc.stderr)
    except FileNotFoundError:
        return _result(EXIT_MISSING, "", f"{binary} not found on PATH")
    except subprocess.TimeoutExpired:
        return _result(EXIT_TIMEOUT, "", f"hbot {' '.join(args)} timed out after {timeout}s")
    except OSError as e:
        return _result(EXIT_ERROR, "", f"failed to run hbot: {e}")


async def run_hbot_async(args: List[str], hbot: Optional[str] = None,
                         timeout: float = READ_TIMEOUT) -> Dict[str, Any]:
    """Non-blocking variant for the web server; never raises."""
    binary = find_hbot(hbot)
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        decode = lambda b: (b or b"").decode("utf-8", "replace")  # noqa: E731
        return _result(proc.returncode if proc.returncode is not None else EXIT_ERROR,
                       decode(stdout), decode(stderr))
    except FileNotFoundError:
        return _result(EXIT_MISSING, "", f"{binary} not found on PATH")
    except asyncio.TimeoutError:
        proc.kill()
        return _result(EXIT_TIMEOUT, "", f"hbot {' '.join(args)} timed out after {timeout}s")
    except OSError as e:
        return _result(EXIT_ERROR, "", f"failed to run hbot: {e}")


def _parse_json(res: Dict[str, Any]) -> Optional[Any]:
    if res["returncode"] != EXIT_SUCCESS:
        return None
    try:
        return json.loads(res["stdout"])
    except json.JSONDecodeError:
        log.warning("unparseable JSON from hbot: %s", res["stdout"][:200])
        return None


def to_number(value: Any, caster=float) -> Optional[Any]:
    if value is None or value == "":
        return None
    try:
        return caster(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- read operations


def read_status(res: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    """`hbot status --json`. Exit 0 with running=false is valid (nothing started yet)."""
    return _parse_json(res if res is not None else run_hbot(["status", "--json"]))


def read_config(res: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    """`hbot config --json` -> {"global": {...}, "strategy": {file,type,state,running,fields,live_fields}}."""
    return _parse_json(res if res is not None else run_hbot(["config", "--json"]))


def read_logs(lines: int = 200, res: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    """`hbot logs -n N --json` -> {"file": str, "lines": [str]}."""
    lines = max(1, min(int(lines), 5000))
    return _parse_json(res if res is not None else run_hbot(["logs", "-n", str(lines), "--json"]))


def read_doctor(res: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    """`hbot doctor --json` -> {"healthy": bool, "checks": [{check,status,detail}]}.

    doctor exits 1 when unhealthy, so parse the payload regardless of the exit code.
    """
    payload = res if res is not None else run_hbot(["doctor", "--json"])
    try:
        return json.loads(payload["stdout"])
    except (json.JSONDecodeError, KeyError):
        return None


HISTORY_COLUMNS = ("market", "pair", "trades", "buys", "sells", "base_vol", "quote_vol",
                   "trade_pnl", "fees", "total_pnl", "return%")


def parse_history(markdown: str) -> List[dict]:
    """Parse the `hbot history` Markdown table (history has no --json) into typed records."""
    rows = [ln for ln in (markdown or "").splitlines() if ln.startswith("|")]
    if len(rows) < 3:
        return []
    header = [c.strip() for c in rows[0].strip("|").split("|")]
    out: List[dict] = []
    for line in rows[2:]:
        values = [c.strip() for c in line.strip("|").split("|")]
        if len(values) != len(header) or all(v == "" for v in values):
            continue
        record = dict(zip(header, values))
        for key in ("trades", "buys", "sells"):
            record[key] = to_number(record.get(key), int)
        for key in ("base_vol", "quote_vol", "trade_pnl", "fees", "total_pnl", "return%"):
            record[key] = to_number(record.get(key), float)
        out.append(record)
    return out


def read_history(res: Optional[Dict[str, Any]] = None) -> List[dict]:
    return parse_history((res if res is not None else run_hbot(["history"]))["stdout"])


def summarize_history(rows: List[dict]) -> dict:
    gross = sum(r.get("trade_pnl") or 0.0 for r in rows)
    fees = sum(r.get("fees") or 0.0 for r in rows)
    net = sum(r.get("total_pnl") or 0.0 for r in rows)
    returns = [r.get("return%") for r in rows if r.get("return%") is not None]
    return {
        "markets": len(rows),
        "trades": sum(r.get("trades") or 0 for r in rows),
        "gross_pnl": round(gross, 6),
        "fees": round(fees, 6),
        "net_pnl": round(net, 6),
        "fee_ratio": round(fees / gross, 4) if gross else None,
        "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
    }


# --------------------------------------------------------------- write operations


def validate_tunable(key: str, value: Any,
                     max_amount: float = DEFAULT_MAX_AMOUNT) -> Tuple[bool, Any, str]:
    """Check one proposed edit against the whitelist. Returns (ok, normalised_value, reason)."""
    if key in BOOL_KEYS:
        if isinstance(value, bool):
            return True, value, ""
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
            return True, value.strip().lower() in ("true", "1", "yes"), ""
        return False, None, f"{key} needs a boolean"

    if key == "total_amount_quote":
        amount = to_number(value, float)
        if amount is None or amount <= 0:
            return False, None, "total_amount_quote needs a positive number"
        if amount > max_amount:
            return False, None, f"total_amount_quote {amount} exceeds cap {max_amount}"
        return True, amount, ""

    if key in ENUM_CHOICES:
        if not isinstance(value, str):
            return False, None, f"{key} needs one of {ENUM_CHOICES[key]}"
        choice = value.strip().upper().replace("ORDERTYPE.", "")
        if choice not in ENUM_CHOICES[key]:
            return False, None, f"{key}={value} not in {ENUM_CHOICES[key]}"
        return True, choice, ""

    if key not in NUMERIC_BOUNDS:
        return False, None, f"{key} is not a tunable key"
    number = to_number(value, float)
    low, high = NUMERIC_BOUNDS[key]
    if number is None:
        return False, None, f"{key} needs a number"
    if not (low <= number <= high):
        return False, None, f"{key}={number} outside [{low}, {high}]"
    return True, number, ""


def format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def set_config(key: str, value: Any, hbot: Optional[str] = None,
               max_amount: float = DEFAULT_MAX_AMOUNT) -> Dict[str, Any]:
    """Validate then apply `hbot config <key> <value>`. Refuses anything off-whitelist."""
    ok, normalized, reason = validate_tunable(key, value, max_amount)
    if not ok:
        return _result(EXIT_CONFIG_ERROR, "", f"refused: {reason}")
    return run_hbot(["config", key, format_value(normalized)], hbot, WRITE_TIMEOUT)


def start_bot(config: Optional[str] = None, hbot: Optional[str] = None) -> Dict[str, Any]:
    args = ["start"]
    if config:
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+\.yml", config):
            return _result(EXIT_CONFIG_ERROR, "", "config name may only contain letters, digits, _ . -")
        args.append(config)
    return run_hbot(args, hbot, WRITE_TIMEOUT)


def stop_bot(hbot: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    args = ["stop"] + (["--force"] if force else [])
    return run_hbot(args, hbot, WRITE_TIMEOUT)


def kill_switch(hbot: Optional[str] = None) -> Dict[str, Any]:
    """Flag the controller to stop opening positions, then stop the bot gracefully."""
    flag = set_config("manual_kill_switch", True, hbot)
    stopped = stop_bot(hbot)
    return _result(stopped["returncode"] or flag["returncode"],
                   (flag["stdout"] + stopped["stdout"]).strip(),
                   (flag["stderr"] + stopped["stderr"]).strip())


def describe(res: Dict[str, Any]) -> str:
    """One-line human summary of an hbot result, for logs and UI toasts."""
    text = (res.get("stderr") or res.get("stdout") or "").strip()
    return re.sub(r"\s+", " ", text)[:300] or f"exit {res.get('returncode')}"

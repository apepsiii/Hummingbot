# AI scalper — ML signals + LLM supervision over Hummingbot

Three processes, one bot. The AI lives **outside** the Hummingbot runtime and talks to it over
MQTT (signals) and the `hbot` CLI (supervision). Hummingbot stays the only thing that places
orders.

```
  ┌─────────────────────┐        MQTT (JSON)         ┌──────────────────────────────┐
  │  ml_publisher.py    │ ─────────────────────────► │  Hummingbot (VPS)            │
  │  the ML brain       │  hbot/predictions/         │  conf_client.yml             │
  │  candles → features │  btc_usdt/ML_SIGNALS       │    mqtt_bridge.mqtt_autostart│
  │  → probabilities    │                            │  controllers/directional_    │
  └─────────────────────┘                            │    trading/ai_scalper.py     │
             ▲                                       │  → PositionExecutor          │
             │ model file / features                 │    (triple barrier)          │
             │                                       └──────────────┬───────────────┘
  ┌──────────┴──────────┐   hbot status/history/config/stop        │
  │  llm_supervisor.py  │ ◄───────────────────────────────────────┘
  │  the meta brain     │   bounded, whitelisted edits only
  └─────────────────────┘
             ▲ browser ──► ai_scalper/webui/server.py ──► hbot CLI (mutasi tervalidasi)
                          │            └────► MQTT hbot/predictions/# (read-only)
                          └─ SSH tunnel loopback-only
```

## Where everything lives

| Piece | Path | Runs on | Role |
|---|---|---|---|
| Controller | `controllers/directional_trading/ai_scalper.py` | VPS, inside Hummingbot | Turns a probability triple into a PositionExecutor; refuses stale signals |
| Controller config | `conf/controllers/conf_ai_scalper.yml` (from `conf_ai_scalper.example.yml`) | VPS | All tunables; `is_updatable` ones apply live within ~10s |
| ML brain | `ai_scalper/ml_publisher.py` | anywhere (dev box or VPS) | Fetches candles, runs the model, publishes predictions |
| Model | `ai_scalper/baseline_model.py` | with the publisher | Feature engineering + `predict()` contract |
| Meta brain | `ai_scalper/llm_supervisor.py` | VPS | Reads PnL/errors, retunes thresholds, trips the kill-switch |
| Web UI | `ai_scalper/webui/` | VPS, loopback (SSH tunnel to view) | Monitor & control dashboard — see `ai_scalper/webui/README.md` |
| CLI wrapper | `ai_scalper/hbot_client.py` | VPS | Shared hbot contract: whitelist, bounds, parsing (supervisor + UI) |
| Broker config | `ai_scalper/mosquitto.conf` | VPS | Mosquitto, loopback-only by default |
| Bootstrap | `ai_scalper/setup_vps.sh` | VPS | conda + mosquitto + configs, idempotent |
| Services | `ai_scalper/systemd/*.service` | VPS | Keep the AI processes and dashboard alive |

## 1. Provision the VPS

Ubuntu/Debian, as your normal user with sudo:

```bash
git clone https://github.com/hummingbot/hummingbot.git ~/hummingbot
cd ~/hummingbot
# drop the ai_scalper/ folder from this checkout into ~/hummingbot/ai_scalper/
chmod +x ai_scalper/setup_vps.sh
./ai_scalper/setup_vps.sh                    # add --install-services for systemd units
```

It installs build tools, Miniconda, Mosquitto, builds the Cython extensions via `make install`,
turns on the MQTT bridge, and writes `conf/controllers/conf_ai_scalper.yml` with paper-trading
defaults. Then `conda activate hummingbot` and run `hbot doctor`.

## 2. Start the bot, then the brain

Two shells on the VPS:

```bash
# shell A — the bot (paper trading, no API keys)
cd ~/hummingbot && conda activate hummingbot
hbot start conf_ai_scalper.yml
hbot logs -f          # expect: "AI scalper subscribed to MQTT topic: hbot/predictions/btc_usdt/ML_SIGNALS"

# shell B — the ML publisher
cd ~/hummingbot/ai_scalper
python ml_publisher.py --pair BTC-USDT --interval 1m --every 15 --dry-run   # verify predictions first
python ml_publisher.py --pair BTC-USDT --interval 1m --every 15             # then publish for real

# shell C — the dashboard (optional; loopback, use an SSH tunnel to view from your laptop)
python ai_scalper/webui/server.py            # http://127.0.0.1:8080
```

Verify the wire independently of both processes:

```bash
mosquitto_sub -h 127.0.0.1 -t 'hbot/predictions/#' -v
```

Then watch the bot react: `hbot status` prints the controller's signal line, `hbot history` prints
PnL and fees per market.

## 3. Wire format

Topic: `hbot/predictions/<pair lowercased, dash → underscore>/ML_SIGNALS`
Payload: a JSON **object** (the bridge calls `ujson.loads`, so a bare array is dropped).

```json
{
  "probabilities": [0.12, 0.28, 0.60],
  "target_pct": 0.0042,
  "pair": "BTC-USDT",
  "interval": "1m",
  "candle_close": 65432.1,
  "ts": 1730000000.0,
  "model": "baseline_heuristic"
}
```

| Key | Required | Meaning |
|---|---|---|
| `probabilities` | yes | `[short, neutral, long]`, each in `[0, 1]` |
| `target_pct` | no | Expected move the model is betting on; sizes the barriers. Falls back to `default_target_pct` |
| anything else | no | Ignored by the controller |

Malformed or out-of-range messages are counted and dropped — they never produce a trade.

## 4. What the controller does with a signal

| Condition | Action |
|---|---|
| `long > long_threshold` and `long >= short` | BUY executor |
| `short > short_threshold` and `short > long` | SELL executor |
| otherwise | no trade |
| no message for `signal_timeout` seconds | signal forced to 0; if `close_on_stale_signal`, active executors are stopped |
| barrier sizing | `stop_loss = clamp(target_pct × sl_multiplier)`, `take_profit = clamp(target_pct × tp_multiplier)`, both clamped to `[min_barrier, max_barrier]` |
| position size | `total_amount_quote / price / max_executors_per_side`, scaled by conviction: `(confidence − threshold) / (1 − threshold)`, floored at `min_size_scale` |

`min_barrier` exists because a scalping barrier tighter than round-trip fees is a guaranteed loss.
Do not lower it below your venue's taker fee × 2 without a reason.

## 5. Supervision layer

`llm_supervisor.py` is dry-run by default — it prints what it would change and touches nothing.

```bash
python llm_supervisor.py --once                  # one assessment
python llm_supervisor.py --once --no-llm         # deterministic rules only
python llm_supervisor.py --every 300 --apply     # act on its judgement
```

Set `LLM_API_KEY` (plus optional `LLM_BASE_URL`, `LLM_MODEL`) in `ai_scalper/.env` to enable the
LLM path; without it, the rule-based guardrails run. Either way the decision is passed through
`sanitize()`: only whitelisted keys, only inside hard-coded bounds, at most 3 edits per cycle,
`total_amount_quote` capped by `--max-amount`. Kill-switch triggers on `--max-drawdown` (net PnL)
or `--max-errors` (log errors), and runs `hbot stop`.

The LLM's output is treated as untrusted input. It cannot invent keys, cannot exceed bounds, and
cannot place an order — there is no code path from the LLM to an exchange call.

## 6. Replacing the baseline model

`baseline_model.py::BaselineHeuristicModel` is a transparent placeholder with **no edge**. Swap it
for a trained model without touching the publisher:

```python
from baseline_model import compute_features, FEATURES

df = compute_features(candles)          # ret_1_z, ret_5_z, ret_15_z, rsi_c, ema_spread_z, vol_z, imbalance, bb_pos
X = df[FEATURES]
y = ...                                  # 0 = short, 1 = neutral, 2 = long (triple-barrier labels)
clf.fit(X, y)
joblib.dump(clf, "scalper.joblib")
```

```bash
python ml_publisher.py --pair BTC-USDT --model scalper.joblib
```

Label with the same triple-barrier logic the executor uses, or the model optimises something the
bot never trades. Walk-forward validate and report PnL **after fees**.

Features are volatility-normalised (returns in ATR units, z-scores clipped), so one trained model
transfers across pairs and timeframes without rescaling. `atr_pct` is computed alongside them for
`target_pct` but is deliberately not a model feature.

Once the trained model replaces the baseline, raise `long_threshold` / `short_threshold` — the
0.55 defaults in the example config are calibrated for the weak baseline so the paper bot trades
at all.

## 7. Going live

```bash
hbot stop
hbot connect binance            # keys encrypted in the keystore
hbot config connector_name binance
hbot config total_amount_quote 100
hbot start
```

Prefer a perp venue for short signals — spot cannot sell short, so `signal = -1` has nothing to do
there. Start with an amount you are willing to lose entirely.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Failed to subscribe to MQTT signals` in `hbot logs` | `mqtt_bridge.mqtt_autostart` is not `true`, or Mosquitto is down. Set it with `hbot config mqtt_bridge.mqtt_autostart true`, restart the bot |
| Publisher logs predictions, controller stays `signal: 0` | Topic mismatch. `mosquitto_sub -t 'hbot/#' -v` and compare against the subscribed topic in the log; the pair segment is lowercased with underscores |
| `status` shows `stale: True` constantly | Publisher interval > `signal_timeout`. Raise `signal_timeout` or lower `--every` |
| Payload silently dropped | Not a JSON object, or `probabilities` missing/wrong length/out of `[0,1]` |
| Many trades, negative PnL, fees ≈ gross | Barriers below round-trip fees. Raise `min_barrier`, `long_threshold`, `cooldown_time` |
| `hbot: command not found` inside the conda env | The `make install` symlink step did not run; re-run `make install` from the repo root |
| Dashboard: "bot unreachable" | Run `server.py` inside the hummingbot conda env so `hbot` resolves on PATH (or `--demo` to preview the UI) |
| Dashboard: mutations return 401 | A `--token`/`WEBUI_TOKEN` is set — open the UI with `?token=<secret>` once, or send `X-WebUI-Token` |
| Dashboard on another machine | It binds 127.0.0.1 on purpose; tunnel it: `ssh -L 8080:127.0.0.1:8080 user@vps` |

## Security

- `ai_scalper/.env` holds secrets and matches the `.gitignore` `.env` rule — never commit it.
  `.env.example` is committed and must stay free of real values.
- Mosquitto binds `127.0.0.1` with `allow_anonymous true`. That is only safe because it is
  unreachable from outside. If the publisher moves to another host, enable the hardened block in
  `mosquitto.conf` (auth + TLS on 8883) and keep 1883/8883 closed in the firewall — an open
  anonymous broker lets anyone publish signals your bot will trade on.
- `hbot connect` stores exchange keys encrypted; `HBOT_PASSWORD` unlocks the keystore.

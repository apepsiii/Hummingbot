#!/usr/bin/env bash
# Bootstrap an Ubuntu VPS to run the Hummingbot AI scalper: conda env, Mosquitto broker,
# MQTT bridge config, and the ai_scalper controller config.
#
#   ./setup_vps.sh                     # install + configure
#   ./setup_vps.sh --install-services  # also install systemd units for the two AI processes
#
# Idempotent: re-running skips what already exists. Set HB_DIR to change the checkout location.
set -euo pipefail

HB_DIR="${HB_DIR:-$HOME/hummingbot}"
CONDA_DIR="${CONDA_DIR:-$HOME/miniconda3}"
ENV_NAME="hummingbot"
AI_DIR="$HB_DIR/ai_scalper"
INSTALL_SERVICES=false

for arg in "$@"; do
  case "$arg" in
    --install-services) INSTALL_SERVICES=true ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This script targets Linux (Ubuntu/Debian). Run it on the VPS, not on Windows." >&2
  exit 1
fi

log "1/6 system packages"
sudo apt-get update -y
sudo apt-get install -y build-essential curl git make mosquitto mosquitto-clients

log "2/6 miniconda"
if [[ ! -x "$CONDA_DIR/bin/conda" ]]; then
  installer="$(mktemp /tmp/miniconda-XXXXXX.sh)"
  curl -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -o "$installer"
  bash "$installer" -b -p "$CONDA_DIR"
  rm -f "$installer"
  "$CONDA_DIR/bin/conda" init bash >/dev/null
else
  echo "conda already present at $CONDA_DIR"
fi
# shellcheck disable=SC1091
source "$CONDA_DIR/etc/profile.d/conda.sh"

log "3/6 hummingbot checkout"
if [[ ! -d "$HB_DIR/.git" ]]; then
  git clone https://github.com/hummingbot/hummingbot.git "$HB_DIR"
fi
cd "$HB_DIR"

log "4/6 hummingbot env + Cython extensions"
# `make install` shells out to /bin/sh, where the `conda` shell function does not exist — put the
# real binaries on PATH so the Makefile's `command -v conda` check and `conda run` calls resolve.
export PATH="$CONDA_DIR/bin:$CONDA_DIR/condabin:$PATH"
make install
conda run -n "$ENV_NAME" hbot --version

log "5/6 mosquitto broker (loopback only)"
sudo install -m 0644 "$AI_DIR/mosquitto.conf" /etc/mosquitto/conf.d/hbot.conf
sudo systemctl enable mosquitto >/dev/null 2>&1 || true
sudo systemctl restart mosquitto
sleep 1
if mosquitto_sub -h 127.0.0.1 -p 1883 -t 'hbot/#' -C 1 -W 1 >/dev/null 2>&1; then
  echo "broker reachable on 127.0.0.1:1883"
else
  echo "broker did not answer a test subscribe; check: sudo systemctl status mosquitto" >&2
fi

log "6/6 hummingbot config"
conda run -n "$ENV_NAME" hbot config mqtt_bridge.mqtt_host localhost
conda run -n "$ENV_NAME" hbot config mqtt_bridge.mqtt_port 1883
conda run -n "$ENV_NAME" hbot config mqtt_bridge.mqtt_namespace hbot
conda run -n "$ENV_NAME" hbot config mqtt_bridge.mqtt_autostart true
conda run -n "$ENV_NAME" hbot config mqtt_bridge.mqtt_external_events true

mkdir -p conf/controllers
if [[ ! -f conf/controllers/conf_ai_scalper.yml ]]; then
  cp "$AI_DIR/conf_ai_scalper.example.yml" conf/controllers/conf_ai_scalper.yml
  echo "wrote conf/controllers/conf_ai_scalper.yml (paper trading defaults)"
else
  echo "conf/controllers/conf_ai_scalper.yml already exists, left untouched"
fi

if [[ ! -f "$AI_DIR/.env" ]]; then
  cp "$AI_DIR/.env.example" "$AI_DIR/.env"
  chmod 600 "$AI_DIR/.env"
  echo "wrote $AI_DIR/.env — edit it for MODEL_PATH / LLM_API_KEY / MQTT credentials"
fi

if [[ "$INSTALL_SERVICES" == true ]]; then
  log "systemd units for the AI processes"
  sudo mkdir -p /etc/systemd/system
  for unit in ml_publisher.service llm_supervisor.service webui.service; do
    sed -e "s|@HB_DIR@|$HB_DIR|g" \
        -e "s|@CONDA_DIR@|$CONDA_DIR|g" \
        -e "s|@USER@|$(id -un)|g" \
        "$AI_DIR/systemd/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
  done
  sudo systemctl daemon-reload
  echo "installed: ml_publisher.service, llm_supervisor.service, webui.service (not started)"
  echo "start with: sudo systemctl start ml_publisher llm_supervisor webui"
  echo "dashboard:  ssh -L 8080:127.0.0.1:8080 user@this-vps  then open http://127.0.0.1:8080"
fi

conda run -n "$ENV_NAME" python -m pip install -r "$AI_DIR/requirements.txt"

cat <<'NEXT'

Done. Next steps, in order:

  cd ~/hummingbot && conda activate hummingbot

  # 1. sanity-check the install (clock skew, keystore, disk, stale pid)
  hbot doctor

  # 2. paper-trade first — no API keys needed
  hbot start conf_ai_scalper.yml
  hbot status
  hbot logs -f          # look for "AI scalper subscribed to MQTT topic"

  # 3. start the ML brain (separate shell)
  cd ai_scalper && python ml_publisher.py --pair BTC-USDT --interval 1m --every 15

  # 3b. optional: the web dashboard (loopback; from your laptop: ssh -L 8080:127.0.0.1:8080)
  #     or with --install-services: sudo systemctl start webui
  python webui/server.py &

  # 4. once trades appear, review performance AFTER fees
  hbot history

  # 5. add the supervisor in dry-run, then let it act
  python ai_scalper/llm_supervisor.py --once
  python ai_scalper/llm_supervisor.py --every 300 --apply

  # 6. only when paper results are acceptable, go live
  hbot stop
  hbot connect binance          # or your exchange; keys are encrypted in the keystore
  hbot config connector_name binance
  hbot start

NEXT

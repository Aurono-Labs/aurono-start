#!/bin/bash
# ============================================================
#  Aurono Start – Universal Installer (macOS + Linux/RPi)
#  Version: v4.35 — dynamic systemd + OTA + cron + venv refresh
#  Author: Aurono Labs
# ============================================================

set -euo pipefail

INSTALL_VERSION="4.35"
REPO_URL="https://github.com/Aurono-Labs/aurono-start/archive/refs/tags/v4.35.zip"
APP_DIR="aurono-poc"

echo ""
echo "=============================================="
echo "     Aurono Start — Universal Installer       "
echo "                v${INSTALL_VERSION}           "
echo "=============================================="
echo ""

# ------------------------------------------------------------
# Determine runtime user/group early
# ------------------------------------------------------------
RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"

# ------------------------------------------------------------
# Detect OS
# ------------------------------------------------------------
PLATFORM=$(uname | tr '[:upper:]' '[:lower:]')
if [[ "$PLATFORM" == "darwin" ]]; then
  OS="macOS"
else
  OS="Linux"
fi
echo "Detected OS: $OS"
echo ""

# ------------------------------------------------------------
# Stop any running Aurono processes
# ------------------------------------------------------------
echo "🛑 Stopping any running Aurono processes..."

if [[ "$OS" == "Linux" ]] && grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
  sudo systemctl stop aurono-dashboard.service 2>/dev/null || true
  sudo systemctl stop aurono-trader.service 2>/dev/null || true
  sudo systemctl stop aurono-update.service 2>/dev/null || true
fi

pkill -f "dashboard.py" 2>/dev/null || true
pkill -f "trader_main.py" 2>/dev/null || true

echo "✅ All old Aurono processes stopped"
echo ""

# ------------------------------------------------------------
# Backup existing installation
# ------------------------------------------------------------
if [[ -d "$APP_DIR" ]]; then
  TS=$(date +"%Y%m%d_%H%M%S")
  BACKUP_DIR="${APP_DIR}_backup_${TS}"
  echo "📁 Existing installation detected → creating backup: $BACKUP_DIR"
  cp -a "$APP_DIR" "$BACKUP_DIR"
  echo "✅ Backup completed"
  echo ""
fi

# ------------------------------------------------------------
# Download and extract new version
# ------------------------------------------------------------
echo "📥 Downloading Aurono Start v${INSTALL_VERSION} from GitHub..."
curl -fL "$REPO_URL" -o aurono-latest.zip

echo "📦 Unpacking..."
unzip -o aurono-latest.zip >/dev/null
rm aurono-latest.zip

EXTRACTED_DIR=$(find . -maxdepth 1 -type d -name "aurono-start-*" | head -n 1)
if [[ -z "$EXTRACTED_DIR" ]]; then
  echo "❌ ERROR: Extracted folder not found"
  exit 1
fi

echo "📁 Detected extracted folder: $EXTRACTED_DIR"
echo ""

# ------------------------------------------------------------
# Safe update logic (preserve config + data)
# ------------------------------------------------------------
echo "♻️ Updating Aurono code (config + data preserved)..."

mkdir -p "$APP_DIR"
# Ensure current user owns the existing tree (if old files are root-owned)
sudo chown -R "$RUN_USER":"$RUN_GROUP" "$APP_DIR" 2>/dev/null || true

TMP_NEW="${APP_DIR}.new"
rm -rf "$TMP_NEW"
mv "$EXTRACTED_DIR" "$TMP_NEW"

# Remove everything except config + data
find "$APP_DIR" -mindepth 1 -maxdepth 1 \
  ! -name "config" \
  ! -name "data" \
  -exec rm -rf {} +

# Copy new code (excluding config/data)
rsync -a --exclude=config --exclude=data "$TMP_NEW"/ "$APP_DIR"/
rm -rf "$TMP_NEW"

echo "✅ Code updated"
echo ""

cd "$APP_DIR"
APP_ROOT="$(pwd)"

# ------------------------------------------------------------
# Prepare directory structure
# ------------------------------------------------------------
mkdir -p config
mkdir -p data
mkdir -p systemd
mkdir -p data/reports/daily
mkdir -p data/reports/weekly
mkdir -p data/reports/html

# ------------------------------------------------------------
# Setup config.yaml default if missing
# ------------------------------------------------------------
if [[ ! -f config/config.yaml ]]; then
  echo "🆕 Creating default config.yaml..."
  cat > config/config.yaml << 'EOF'
api_credentials: bitvavo_api_key.json
buy_drop_pct: -2.5
dashboard_host: 0.0.0.0
dashboard_port: 8000
db_path: ../data/trades.db
dev_sleep_hours: 4
email:
  enabled: false
  password: ""
  to: ""
  username: ""
exchange: bitvavo
invest_eur: 50.0
live_trading: true
log_path: ../data/aurono_log.txt
mode: live
pair: BTCEUR
sell_rise_pct: 2.1
log_level: INFO

EOF
else
  echo "📌 Keeping existing config.yaml"
fi

# ------------------------------------------------------------
# Python environment
# ------------------------------------------------------------
echo ""
echo "🐍 Preparing Python environment..."

if [[ ! -d "venv" ]]; then
  python3 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt >/dev/null

echo "✅ Python ready"
echo ""

# ------------------------------------------------------------
# Create trades.db if needed
# ------------------------------------------------------------
if [[ ! -f data/trades.db ]]; then
  echo "🗄 Creating trades.db..."
  venv/bin/python src/tools/create_trades_db.py
else
  echo "💾 trades.db exists"
fi

# ------------------------------------------------------------
# Write VERSION file
# ------------------------------------------------------------
echo "${INSTALL_VERSION}" > VERSION

# ------------------------------------------------------------
# Install cron jobs
# ------------------------------------------------------------
echo "⏰ Installing cron jobs for reports..."

CRON_DAILY="0 7 * * * cd $APP_ROOT && ./venv/bin/python src/run_daily_report.py"
CRON_WEEKLY="0 9 * * SUN cd $APP_ROOT && ./venv/bin/python src/run_weekly_report.py"

if [[ "$OS" == "Linux" ]]; then
  EXISTING=$(crontab -l 2>/dev/null || true)
  CLEAN=$(echo "$EXISTING" | grep -v run_daily_report.py | grep -v run_weekly_report.py || true)

  printf "%s\n%s\n%s\n" "$CLEAN" "$CRON_DAILY" "$CRON_WEEKLY" | crontab -
  echo "✅ Cron installed"
  echo ""
fi

# ------------------------------------------------------------
# Install systemd services (Raspberry Pi)
# ------------------------------------------------------------
if [[ "$OS" == "Linux" ]] && grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
  echo "🐧 Installing systemd services..."

  sudo cp systemd/aurono-dashboard.service /etc/systemd/system/aurono-dashboard.service
  sudo cp systemd/aurono-trader.service   /etc/systemd/system/aurono-trader.service

  sudo sed -i "s|__AURONO_USER__|$RUN_USER|g"         /etc/systemd/system/aurono-dashboard.service
  sudo sed -i "s|__AURONO_GROUP__|$RUN_GROUP|g"       /etc/systemd/system/aurono-dashboard.service
  sudo sed -i "s|__AURONO_APP_ROOT__|$APP_ROOT|g"     /etc/systemd/system/aurono-dashboard.service

  sudo sed -i "s|__AURONO_USER__|$RUN_USER|g"         /etc/systemd/system/aurono-trader.service
  sudo sed -i "s|__AURONO_GROUP__|$RUN_GROUP|g"       /etc/systemd/system/aurono-trader.service
  sudo sed -i "s|__AURONO_APP_ROOT__|$APP_ROOT|g"     /etc/systemd/system/aurono-trader.service

  sudo systemctl daemon-reload
  sudo systemctl enable aurono-dashboard.service aurono-trader.service
  sudo systemctl restart aurono-dashboard.service aurono-trader.service

  echo "▶️ Dashboard + Trader started"
  echo ""
fi

# ------------------------------------------------------------
# Install OTA updater (APP_ROOT + RUN_USER embedded)
# ------------------------------------------------------------
if [[ "$OS" == "Linux" ]] && grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
  echo "📡 Installing OTA updater..."

  sudo mkdir -p /usr/local/bin

  # NOTE: unquoted EOF so ${APP_ROOT} and ${RUN_USER} are expanded
  sudo bash -c "cat > /usr/local/bin/aurono-update" << EOF
#!/usr/bin/env python3
import os, re, shutil, subprocess, urllib.request, json, zipfile, pwd, grp
import yaml

OWNER = "Aurono-Labs"
REPO = "aurono-start"
API_URL = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"

# These are baked in by the installer for this device
INSTALL_DIR = "${APP_ROOT}"
RUNTIME_USER = "${RUN_USER}"

PARENT_DIR = os.path.dirname(INSTALL_DIR)
WORK_DIR = os.path.join(PARENT_DIR, "aurono-update")
VERSION_FILE = os.path.join(INSTALL_DIR, "VERSION")
BACKUP_DIR = os.path.join(PARENT_DIR, "aurono-poc_backup")

SERVICES = ["aurono-dashboard", "aurono-trader"]

def log(msg):
    print(f"[aurono-update] {msg}")

def parse_version(v: str):
    parts = v.strip().lstrip("vV").split(".")
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)

def newer(remote: str, local: str) -> bool:
    return parse_version(remote) > parse_version(local)

def read_local_version() -> str:
    if not os.path.exists(VERSION_FILE):
        return "0.0"
    return open(VERSION_FILE).read().strip()

def get_latest_release():
    req = urllib.request.Request(API_URL, headers={"User-Agent": "aurono-update"})
    data = urllib.request.urlopen(req, timeout=30).read()
    rel = json.loads(data.decode())

    if rel.get("draft") or rel.get("prerelease"):
        raise RuntimeError("Latest release is draft/prerelease")

    tag = rel.get("tag_name", "") or ""
    version = tag.lstrip("vV")
    return version, rel

# Accept any uploaded .zip asset from the release (not GitHub auto "source" links)
def find_asset(rel: dict):
    for asset in rel.get("assets", []):
        name = asset.get("name", "")
        if name.lower().endswith(".zip"):
            return asset["browser_download_url"], name
    raise RuntimeError("No .zip asset found in release assets")

def download_asset(url: str, target: str):
    req = urllib.request.Request(url, headers={"User-Agent": "aurono-update"})
    with urllib.request.urlopen(req, timeout=120) as r, open(target, "wb") as f:
        shutil.copyfileobj(r, f)

def extract_zip(zip_path: str, target_dir: str) -> str:
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir)
    os.makedirs(target_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(target_dir)
        names = z.namelist()

    # auto-detect top folder
    top_dirs = {n.split("/")[0] for n in names if "/" in n}
    if len(top_dirs) == 1:
        return os.path.join(target_dir, next(iter(top_dirs)))
    return target_dir

def copy_persistent(old_dir: str, new_dir: str):
    for folder in ("config", "data"):
        src = os.path.join(old_dir, folder)
        dst = os.path.join(new_dir, folder)
        if os.path.exists(src):
            log(f"Copying persistent directory: {folder}/")
            shutil.copytree(src, dst, dirs_exist_ok=True)

def fix_ownership(path: str):
    # Always chown to the runtime user baked into the installer
    user = RUNTIME_USER or "aurono"
    pw = pwd.getpwnam(user)
    uid, gid = pw.pw_uid, pw.pw_gid

    for root, dirs, files in os.walk(path):
        os.chown(root, uid, gid)
        for d in dirs:
            os.chown(os.path.join(root, d), uid, gid)
        for f in files:
            os.chown(os.path.join(root, f), uid, gid)

    log(f"Ownership corrected for {path} (user={user}, uid={uid}, gid={gid})")

def refresh_venv():
    venv_dir = os.path.join(INSTALL_DIR, "venv")
    python = os.path.join(venv_dir, "bin", "python")

    if not os.path.exists(python):
        log("venv missing → creating")
        subprocess.run(["python3", "-m", "venv", venv_dir], check=True)

    req_file = os.path.join(INSTALL_DIR, "requirements.txt")
    if os.path.exists(req_file):
        log("Installing Python dependencies…")
        subprocess.run([python, "-m", "pip", "install", "--upgrade", "pip"], check=False)
        subprocess.run([python, "-m", "pip", "install", "-r", req_file], check=True)

def stop_services():
    for s in SERVICES:
        subprocess.run(["sudo", "systemctl", "stop", f"{s}.service"], check=False)

def start_services():
    for s in SERVICES:
        subprocess.run(["sudo", "systemctl", "start", f"{s}.service"], check=False)

def migrate_config():
    cfg_path = os.path.join(INSTALL_DIR, "config", "config.yaml")

    if not os.path.exists(cfg_path):
        return

    try:
        with open(cfg_path, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        log(f"Config read failed, skipping migration: {e}")
        return

    defaults = {
        "log_level": "INFO",
    }

    changed = False
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = v
            changed = True

    if changed:
        try:
            with open(cfg_path, "w") as f:
                yaml.safe_dump(cfg, f, sort_keys=False)
            log("Config migrated: added missing keys")
        except Exception as e:
            log(f"Config write failed: {e}")


def main():
    local = read_local_version()
    log(f"Installed: {local}")

    try:
        latest, meta = get_latest_release()
    except Exception as e:
        log(f"GitHub error: {e}")
        return

    log(f"Remote: {latest}")

    if not newer(latest, local):
        log("No update needed.")
        return

    try:
        url, name = find_asset(meta)
    except Exception as e:
        log(f"Asset error: {e}")
        return

    log(f"Found asset: {name}")

    os.makedirs(WORK_DIR, exist_ok=True)
    zip_path = os.path.join(WORK_DIR, "aurono-update.zip")

    try:
        log("Downloading asset...")
        download_asset(url, zip_path)
    except Exception as e:
        log(f"Download failed: {e}")
        return

    try:
        new_root = extract_zip(zip_path, os.path.join(WORK_DIR, "new"))
        log(f"Extracted: {new_root}")
    except Exception as e:
        log(f"Extraction error: {e}")
        return

    copy_persistent(INSTALL_DIR, new_root)


    try:
        stop_services()

        if os.path.exists(BACKUP_DIR):
            shutil.rmtree(BACKUP_DIR)
        if os.path.exists(INSTALL_DIR):
            shutil.move(INSTALL_DIR, BACKUP_DIR)

        shutil.move(new_root, INSTALL_DIR)
        fix_ownership(INSTALL_DIR)
        refresh_venv()
        
        # 🔧 Config migration (additive, idempotent)
        migrate_config()

        with open(os.path.join(INSTALL_DIR, "VERSION"), "w") as f:
            f.write(latest)

        start_services()
        log(f"Update successful → v{latest}")

    except Exception as e:
        log(f"Swap failed: {e}")
        log(f"Backup preserved at {BACKUP_DIR}")

if __name__ == "__main__":
    main()
EOF

  sudo chmod +x /usr/local/bin/aurono-update

  sudo bash -c "cat > /etc/systemd/system/aurono-update.timer" << 'EOF'
[Unit]
Description=Aurono OTA Update Check

[Timer]
OnCalendar=daily
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
EOF

  sudo bash -c "cat > /etc/systemd/system/aurono-update.service" << 'EOF'
[Unit]
Description=Aurono OTA Updater

[Service]
Type=oneshot
ExecStart=/usr/local/bin/aurono-update
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable --now aurono-update.timer

  echo "✅ OTA updater installed"
fi

# ------------------------------------------------------------
# macOS: auto-launch dashboard
# ------------------------------------------------------------
if [[ "$OS" == "macOS" ]]; then
  venv/bin/python src/dashboard.py &
  disown
  echo "➡️ Dashboard started at http://localhost:8000"
fi

echo ""
echo "=============================================="
echo " 🎉 Aurono Start v${INSTALL_VERSION} installed "
echo "=============================================="
echo ""


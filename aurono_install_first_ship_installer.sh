#!/bin/bash
# ============================================================
#  Aurono Start – Universal Installer (macOS + Linux/RPi)
#  Version: v4.13 — dynamic systemd + OTA + cron + fixed chown
#  Author: Aurono Labs
# ============================================================

set -euo pipefail

INSTALL_VERSION="4.13"
REPO_URL="https://github.com/Aurono-Labs/aurono-start/archive/refs/tags/v4.13.zip"
APP_DIR="aurono-poc"

echo ""
echo "=============================================="
echo "     Aurono Start — Universal Installer       
                v${INSTALL_VERSION}           
=============================================="
echo ""

# ------------------------------------------------------------
# Determine correct runtime user early (important!)
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
# Stop any running services
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
# Safe update logic (preserved config + data)
# ------------------------------------------------------------
echo "♻️ Updating Aurono code (config + data preserved)..."

mkdir -p "$APP_DIR"
sudo chown -R "$RUN_USER":"$RUN_GROUP" "$APP_DIR"

TMP_NEW="${APP_DIR}.new"
rm -rf "$TMP_NEW"
mv "$EXTRACTED_DIR" "$TMP_NEW"

# Remove all files EXCEPT config & data
find "$APP_DIR" -mindepth 1 -maxdepth 1 \
  ! -name "config" \
  ! -name "data" \
  -exec rm -rf {} +

rsync -a --exclude=config --exclude=data "$TMP_NEW"/ "$APP_DIR"/
rm -rf "$TMP_NEW"

echo "✅ Code updated"
echo ""

cd "$APP_DIR"
APP_ROOT="$(pwd)"

# ------------------------------------------------------------
# Prepare directory structure
# ------------------------------------------------------------
mkdir -p config data systemd
mkdir -p data/reports/{daily,weekly,html}

# ------------------------------------------------------------
# Default config.yaml if missing
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
# Install CRON jobs
# ------------------------------------------------------------
echo "⏰ Installing cron jobs..."

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
# Install systemd services
# ------------------------------------------------------------
if [[ "$OS" == "Linux" ]] && grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
  echo "🐧 Installing systemd services..."

  sudo cp systemd/aurono-dashboard.service /etc/systemd/system/aurono-dashboard.service
  sudo cp systemd/aurono-trader.service /etc/systemd/system/aurono-trader.service

  sudo sed -i "s|__AURONO_USER__|$RUN_USER|g" /etc/systemd/system/aurono-dashboard.service
  sudo sed -i "s|__AURONO_GROUP__|$RUN_GROUP|g" /etc/systemd/system/aurono-dashboard.service
  sudo sed -i "s|__AURONO_APP_ROOT__|$APP_ROOT|g" /etc/systemd/system/aurono-dashboard.service

  sudo sed -i "s|__AURONO_USER__|$RUN_USER|g" /etc/systemd/system/aurono-trader.service
  sudo sed -i "s|__AURONO_GROUP__|$RUN_GROUP|g" /etc/systemd/system/aurono-trader.service
  sudo sed -i "s|__AURONO_APP_ROOT__|$APP_ROOT|g" /etc/systemd/system/aurono-trader.service

  sudo systemctl daemon-reload
  sudo systemctl enable aurono-dashboard.service aurono-trader.service
  sudo systemctl restart aurono-dashboard.service aurono-trader.service

  echo "▶️ Dashboard + Trader started"
  echo ""
fi

# ------------------------------------------------------------
# Install OTA updater (with fixed ownership)
# ------------------------------------------------------------
if [[ "$OS" == "Linux" ]] && grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then

  echo "📡 Installing OTA updater..."

  sudo mkdir -p /usr/local/bin

  sudo bash -c "cat > /usr/local/bin/aurono-update" << EOF
#!/usr/bin/env python3
import os, re, shutil, subprocess, urllib.request, json, zipfile

OWNER = "Aurono-Labs"
REPO = "aurono-start"
API_URL = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"

INSTALL_DIR = "${APP_ROOT}"
PARENT_DIR = os.path.dirname(INSTALL_DIR)
WORK_DIR = os.path.join(PARENT_DIR, "aurono-update")
VERSION_FILE = os.path.join(INSTALL_DIR, "VERSION")
BACKUP_DIR = os.path.join(PARENT_DIR, "aurono-poc_backup")
SERVICES = ["aurono-dashboard", "aurono-trader"]

def log(msg): print(f"[aurono-update] {msg}")

def parse_version(v):
    parts = re.split(r"[.]", v.lstrip("vV").strip())
    return tuple(int(p) for p in parts)

def newer(remote, local): return parse_version(remote) > parse_version(local)

def read_local_version():
    if not os.path.exists(VERSION_FILE):
        return "0.0"
    return open(VERSION_FILE).read().strip()

def fix_ownership(path: str):
    import pwd
    user = os.environ.get("SUDO_USER") or "aurono"
    pw = pwd.getpwnam(user)
    uid, gid = pw.pw_uid, pw.pw_gid
    for root, dirs, files in os.walk(path):
        os.chown(root, uid, gid)
        for d in dirs: os.chown(os.path.join(root, d), uid, gid)
        for f in files: os.chown(os.path.join(root, f), uid, gid)
    log(f"Ownership corrected for {path} (user={user})")

def get_latest_release():
    req = urllib.request.Request(API_URL, headers={"User-Agent": "aurono-update"})
    data = urllib.request.urlopen(req).read()
    rel = json.loads(data.decode())
    return rel["tag_name"].lstrip("v"), rel

def find_asset(rel, version):
    for asset in rel["assets"]:
        if asset["name"] == f"{REPO}-{version}.zip":
            return asset["browser_download_url"], asset["name"]
    raise RuntimeError("No matching asset found")

def download_asset(url, target):
    req = urllib.request.Request(url, headers={"User-Agent": "aurono-update"})
    with urllib.request.urlopen(req) as r, open(target, "wb") as f:
        shutil.copyfileobj(r, f)

def extract(zip_path, target):
    if os.path.exists(target): shutil.rmtree(target)
    os.makedirs(target, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(target)
        names = z.namelist()
    top = {n.split("/")[0] for n in names if "/" in n}
    return os.path.join(target, next(iter(top)))

def main():
    local = read_local_version()
    log(f"Installed: {local}")

    latest, meta = get_latest_release()
    log(f"Remote: {latest}")

    if not newer(latest, local):
        log("No update needed.")
        return

    url, name = find_asset(meta, latest)
    log(f"Found asset: {name}")

    os.makedirs(WORK_DIR, exist_ok=True)
    zip_path = os.path.join(WORK_DIR, "update.zip")
    download_asset(url, zip_path)

    new_root = extract(zip_path, os.path.join(WORK_DIR, "new"))
    log(f"Extracted to: {new_root}")

    # Move config/data
    for folder in ("config", "data"):
        src = os.path.join(INSTALL_DIR, folder)
        dst = os.path.join(new_root, folder)
        if os.path.exists(src): shutil.copytree(src, dst, dirs_exist_ok=True)

    # Swap directories
    for s in SERVICES: subprocess.run(["sudo","systemctl","stop",f"{s}.service"])
    if os.path.exists(BACKUP_DIR): shutil.rmtree(BACKUP_DIR)
    if os.path.exists(INSTALL_DIR): shutil.move(INSTALL_DIR, BACKUP_DIR)
    shutil.move(new_root, INSTALL_DIR)

    # Fix ownership
    fix_ownership(INSTALL_DIR)

    with open(os.path.join(INSTALL_DIR,"VERSION"),"w") as f:
        f.write(latest)

    for s in SERVICES: subprocess.run(["sudo","systemctl","start",f"{s}.service"])
    log(f"Update successful → v{latest}")

if __name__=="__main__":
    main()
EOF

  sudo chmod +x /usr/local/bin/aurono-update

  # timer + service
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
# macOS local autostart
# ------------------------------------------------------------
if [[ "$OS" == "macOS" ]]; then
  venv/bin/python src/dashboard.py &
  disown
  echo "➡ Dashboard running at http://localhost:8000"
fi

echo ""
echo "=============================================="
echo " 🎉 Aurono Start v${INSTALL_VERSION} installed "
echo "=============================================="
echo ""


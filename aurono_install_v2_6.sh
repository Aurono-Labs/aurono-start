#!/bin/bash
# ============================================================
#  Aurono Start – Universal Installer (macOS + Linux/RPi)
#  Downloads latest code from GitHub and sets up environment
#  Author: Aurono Labs — Eppo Edzes
# ============================================================

set -euo pipefail

REPO_URL="https://github.com/Aurono-Labs/aurono-start/archive/refs/tags/v2.6.zip"
APP_DIR="aurono-poc"

echo ""
echo "=============================================="
echo "     🚀 Aurono Start — Universal Installer     "
echo "=============================================="
echo ""

# ------------------------------------------------------------
# Detect OS (macOS or Linux/RPi)
# ------------------------------------------------------------
PLATFORM=$(uname | tr '[:upper:]' '[:lower:]')
if [[ "$PLATFORM" == "darwin" ]]; then OS="macOS"; else OS="Linux"; fi
echo "Detected OS: $OS"
echo ""

# ------------------------------------------------------------
# Ensure dependencies
# ------------------------------------------------------------
echo "📦 Checking dependencies (python3, curl, unzip)..."
for dep in python3 curl unzip; do
  command -v "$dep" >/dev/null || { echo "❌ Missing: $dep"; exit 1; }
done
echo "✅ Dependencies OK"
echo ""

# ------------------------------------------------------------
# Back up existing installation
# ------------------------------------------------------------
if [ -d "$APP_DIR" ]; then
    ts=$(date +"%Y%m%d_%H%M%S")
    echo "📁 Existing installation detected → creating backup ${APP_DIR}_backup_$ts"
    mv "$APP_DIR" "${APP_DIR}_backup_$ts"
fi

# ------------------------------------------------------------
# Download & unpack the latest code
# ------------------------------------------------------------
echo "📥 Downloading Aurono Start from GitHub..."
curl -L "$REPO_URL" -o aurono-latest.zip

echo "📦 Unpacking..."
unzip -o aurono-latest.zip >/dev/null
rm aurono-latest.zip

# Detect the extracted folder (works for main.zip and versioned tags)
EXTRACTED_DIR=$(find . -maxdepth 1 -type d -name "aurono-start-*" | head -n 1)

if [ -z "$EXTRACTED_DIR" ]; then
  echo "❌ ERROR: Could not find extracted GitHub source folder. Aborting."
  exit 1
fi

echo "📁 Detected extracted folder: $EXTRACTED_DIR"

mv "$EXTRACTED_DIR" "$APP_DIR"

# ------------------------------------------------------------
# Ensure required directories exist BEFORE restoring files
# ------------------------------------------------------------
mkdir -p $APP_DIR/config
mkdir -p $APP_DIR/data
mkdir -p $APP_DIR/systemd

# ------------------------------------------------------------
# Restore user-specific files from previous installation
# ------------------------------------------------------------

# Look for backups in the HOME directory
BACKUP_DIRS=$(ls -d "$HOME"/aurono-poc_backup_* 2>/dev/null || true)

if [ -n "$BACKUP_DIRS" ]; then
  LAST_BACKUP=$(echo "$BACKUP_DIRS" | tail -n 1)
  echo "♻️ Restoring user data from backup: $LAST_BACKUP"

  # Restore config.yaml
  if [ -f "$LAST_BACKUP/config/config.yaml" ]; then
      cp "$LAST_BACKUP/config/config.yaml" "$APP_DIR/config/config.yaml"
      echo "✔ Restored config.yaml"
  fi

  # Restore trades.db
  if [ -f "$LAST_BACKUP/data/trades.db" ]; then
      cp "$LAST_BACKUP/data/trades.db" "$APP_DIR/data/trades.db"
      echo "✔ Restored trades.db"
  fi

  # Restore log file
  if [ -f "$LAST_BACKUP/data/aurono_log.txt" ]; then
      cp "$LAST_BACKUP/data/aurono_log.txt" "$APP_DIR/data/aurono_log.txt"
      echo "✔ Restored aurono_log.txt"
  fi

  # Restore API keys
  if [ -f "$LAST_BACKUP/bitvavo_api_key.json" ]; then
      cp "$LAST_BACKUP/bitvavo_api_key.json" "$APP_DIR/bitvavo_api_key.json"
      echo "✔ Restored bitvavo_api_key.json"
  fi

  if [ -f "$LAST_BACKUP/kraken_api_key.json" ]; then
      cp "$LAST_BACKUP/kraken_api_key.json" "$APP_DIR/kraken_api_key.json"
      echo "✔ Restored kraken_api_key.json"
  fi

  # Restore DB backups
  cp "$LAST_BACKUP"/data/trades_backup_*.db "$APP_DIR/data/" 2>/dev/null || true
  echo "✔ Restored DB backups"

else
  echo "ℹ️ No previous installation found — clean install"
fi

echo "✅ Code installed into $APP_DIR/"
echo ""

# ------------------------------------------------------------
# Ensure config + data directories exist
# ------------------------------------------------------------
mkdir -p $APP_DIR/config
mkdir -p $APP_DIR/data
mkdir -p $APP_DIR/systemd

# ------------------------------------------------------------
# Create default config.yaml if missing
# ------------------------------------------------------------
CONFIG="$APP_DIR/config/config.yaml"

if [ ! -f "$CONFIG" ]; then
  echo "🆕 Creating config.yaml..."
  cat > "$CONFIG" << 'EOF'
mode: "dev"
dev_sleep_hours: 4

exchange: "bitvavo"
api_credentials: "bitvavo_api_key.json"

log_path: "../data/aurono_log.txt"
db_path: "../data/trades.db"

dashboard_host: "0.0.0.0"
dashboard_port: 8000

live_trading: false
EOF
else
  echo "📌 Keeping existing config.yaml"
fi

# ------------------------------------------------------------
# Create API credential stubs if missing
# ------------------------------------------------------------
if [ ! -f "$APP_DIR/bitvavo_api_key.json" ]; then
cat > "$APP_DIR/bitvavo_api_key.json" << 'EOF'
{ "api_key": "YOUR_BITVAVO_API_KEY", "api_secret": "YOUR_BITVAVO_API_SECRET" }
EOF
fi

if [ ! -f "$APP_DIR/kraken_api_key.json" ]; then
cat > "$APP_DIR/kraken_api_key.json" << 'EOF'
{ "api_key": "YOUR_KRAKEN_API_KEY", "api_secret": "YOUR_KRAKEN_API_SECRET" }
EOF
fi

echo "🔐 API key files ready"
echo ""

# ------------------------------------------------------------
# Ensure aurono_log.txt exists
# ------------------------------------------------------------
LOG_FILE="$APP_DIR/data/aurono_log.txt"
if [ ! -f "$LOG_FILE" ]; then
  echo "📝 Creating new aurono_log.txt"
  echo "=== Aurono Log $(date '+%Y-%m-%d %H:%M:%S') ===" > "$LOG_FILE"
else
  echo "📜 Keeping existing log file"
fi

# ------------------------------------------------------------
# Ensure trades.db exists or create a fresh one
# ------------------------------------------------------------
DB="$APP_DIR/data/trades.db"
if [ ! -f "$DB" ]; then
  echo "🗄 Creating trades.db..."
  python3 $APP_DIR/src/tools/create_trades_db.py
else
  echo "💾 Keeping existing trades.db"
fi

# ------------------------------------------------------------
# Setup Python venv + install requirements
# ------------------------------------------------------------
echo ""
echo "🐍 Preparing Python environment..."
cd $APP_DIR

if [ ! -d "venv" ]; then
  python3 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip >/dev/null
echo "📦 Installing Python dependencies..."
pip install -r requirements.txt >/dev/null

echo "✅ Python ready"
echo ""

# ------------------------------------------------------------
# Install systemd services on Raspberry Pi
# ------------------------------------------------------------
if [[ "$OS" == "Linux" ]]; then
  if grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
    echo "🐧 Raspberry Pi detected → installing systemd services..."

    sudo cp systemd/aurono-dashboard.service /etc/systemd/system/
    sudo cp systemd/aurono-trader.service   /etc/systemd/system/

    sudo systemctl daemon-reload
    sudo systemctl enable aurono-dashboard.service
    sudo systemctl enable aurono-trader.service

    echo ""
    echo "➡️ Start services with:"
    echo "   sudo systemctl start aurono-dashboard"
    echo "   sudo systemctl start aurono-trader"
    echo ""
  fi
fi

# ------------------------------------------------------------
# Start dashboard automatically on macOS
# ------------------------------------------------------------
if [[ "$OS" == "macOS" ]]; then
  echo "🌐 Launching dashboard on macOS..."
  python3 src/dashboard.py &
  disown
  echo "➡️ Open http://localhost:8000"
  echo ""
fi

echo ""
echo "=============================================="
echo " 🎉 Aurono Start installation complete!        "
echo "=============================================="
echo ""


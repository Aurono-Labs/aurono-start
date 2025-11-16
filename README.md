# Aurono PoC — PoC-v2.2 (macOS + Linux/RPi Ready)
Author: Aurono Labs — Eppo Edzes

## Quick Install
```bash

# Run it (no chmod needed)
bash aurono_poc_setup_v2_2.sh

# Then run the post-setup
bash run_after_setup.sh
```

Or if you prefer making it executable:
```bash
# Download and make executable
curl -O https://your-repo.com/aurono_poc_setup_v2_2.sh
chmod +x aurono_poc_setup_v2_2.sh

# Run it
./aurono_poc_setup_v2_2.sh

# Post-setup (already executable from installer)
./run_after_setup.sh
```

## Features
Clean folder tree with sys.path fix for service mode.
Full path enforcement via utils.root_path().
Toggle Dev / Live mode via dashboard.
Service template files under systemd/ for Linux/RPi.
Works on macOS (standard install) and Linux/RPi (with optional service install).

## Manual steps (macOS)
cd aurono-poc
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
if [ -d "aurono-poc" ]; then
  cd aurono-poc
fi

python3 src/tools/create_trades_db.py
python3 src/dashboard.py
open http://localhost:8000

## Service install (Linux / Raspberry Pi)
sudo cp systemd/aurono-dashboard.service /etc/systemd/system/
sudo cp systemd/aurono-trader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable aurono-dashboard.service
sudo systemctl enable aurono-trader.service
sudo systemctl start aurono-dashboard.service
sudo systemctl start aurono-trader.service


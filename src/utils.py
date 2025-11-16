import sys
from pathlib import Path
# Add project root and src to sys.path for service compatibility
ROOT = Path(__file__).resolve().parent.parent
SRC  = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

import os, yaml, json
from datetime import datetime
from decimal import Decimal

def root_path(*parts):
    return ROOT.joinpath(*parts)

def load_config():
    with open(root_path("config","config.yaml"), "r") as f:
        return yaml.safe_load(f)

def save_config(cfg: dict):
    p = root_path("config","config.yaml")
    with open(p, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)

def current_config():
    return load_config()

def log_event(msg:str):
    cfg = current_config()
    logp = root_path("data", Path(cfg["log_path"]).name)
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    os.makedirs(logp.parent, exist_ok=True)
    with open(logp, "a") as f:
        f.write(line + "\\n")

def to_decimal(v):
    try: return Decimal(str(v))
    except Exception: return Decimal("0.0")

def get_db_path():
    cfg = current_config()
    return root_path("data", Path(cfg["db_path"]).name)

def load_api_keys():
    cfg = current_config()
    cred = root_path(Path(cfg["api_credentials"]))
    with open(cred) as f:
        c = json.load(f)
    return c.get("api_key"), c.get("api_secret")

"""Load the private project configuration from the repository-root config.json."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.json"
DEFAULTS = {
    "database_path": "storage/ashathub.sqlite3",
    "mysql_source_url": "",
    "oidc_issuer": "https://www.agpstudios.org/api/oauth",
    "oidc_private_key_file": "",
    "payment_encryption_key": "",
    "paws_health_url": "http://127.0.0.1:3003/api/health",
    "paws_online_url": "http://127.0.0.1:3003/api/online-count",
    "everenfell_online_url": "",
    "world_of_ra_mint_url": "",
    "world_of_ra_mint_key": "",
    "oidc_clients": {},
    "port": 3110,
}


def load():
    if not CONFIG_PATH.exists():
        return DEFAULTS.copy()
    values = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ValueError(f"{CONFIG_PATH} must contain a JSON object")
    unknown = values.keys() - DEFAULTS.keys()
    if unknown:
        raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")
    return DEFAULTS | values


def path(value):
    result = Path(value)
    return result if result.is_absolute() else ROOT / result


CONFIG = load()

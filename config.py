"""Configuration management.

Stores all settings (API credentials, mappings, sync preferences) in a local
JSON file. No .env needed — everything is configured through the dashboard.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "arena": {
        "api_url": "https://api.arenasolutions.com/v1",
        "email": "",
        "password": "",
        "workspace_id": "",
    },
    "odoo": {
        "url": "",
        "db": "",
        "user": "",
        "password": "",
    },
    "sync": {
        "interval_minutes": 15,
        "auto_sync": False,
    },
    "mapping": {
        "categories": {},   # Arena category name → Odoo categ_id (manual overrides; auto-match used first)
        "uom": {},          # Arena UoM name → Odoo uom.uom ID (manual overrides; auto-match used first)
        "default_category_id": 1,   # Fallback Odoo category if no match found (1 = "All")
        "default_uom_id": 1,        # Fallback UoM if no match found (1 = "Units")
    },
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
            # Merge with defaults so new keys are always present
            config = _deep_merge(DEFAULT_CONFIG, saved)
            return config
        except Exception as e:
            logger.error("Failed to load config: %s", e)
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    logger.info("Config saved")


def _deep_merge(default: dict, override: dict) -> dict:
    result = default.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def is_arena_configured(config: dict) -> bool:
    a = config.get("arena", {})
    return bool(a.get("email") and a.get("password") and a.get("workspace_id"))


def is_odoo_configured(config: dict) -> bool:
    o = config.get("odoo", {})
    return bool(o.get("url") and o.get("db") and o.get("user") and o.get("password"))

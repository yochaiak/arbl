import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API_ORIGIN = "http://127.0.0.1:8400"
ADSCORE_ENGINE_ORIGINS = frozenset({"https://c.adsco.re", "https://adsco.re", "https://162.252.214.4"})
ADSCORE_PING_HOST = re.compile(r"[a-z0-9]{1,63}\.(l4|n4|s4)\.adsco\.re\Z")
CLOAKBROWSER_LICENSE_ORIGIN = "https://cloakbrowser.dev"
CLOAKBROWSER_GEOIP_ORIGINS = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me",
)
CONTROLLED_ORIGIN = os.getenv("ABRL_CONTROLLED_ORIGIN", "").rstrip("/")
WEB_ORIGIN = os.getenv("ABRL_WEB_ORIGIN", "http://localhost:8430")
DATABASE_URL = os.getenv("ABRL_DATABASE_URL", "sqlite:///.abrl/lab.sqlite3")
REDIS_URL = os.getenv("ABRL_REDIS_URL", "redis://127.0.0.1:6379/0")
OPERATOR_TOKEN = os.getenv("ABRL_OPERATOR_TOKEN", "")
CLICKHOUSE_URL = os.getenv("ABRL_CLICKHOUSE_URL", "")
CLICKHOUSE_PASSWORD = os.getenv("ABRL_CLICKHOUSE_PASSWORD", "")
LEASE_SECONDS = 30
DISPATCH_SECONDS = 30

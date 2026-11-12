import base64
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
path = root / ".env"
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    existing = path.read_text()
    additions = {
        "ABRL_PROFILE_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "ABRL_CORRELATION_SECRET": secrets.token_hex(32),
        "ABRL_SESSION_SECRET": secrets.token_hex(32),
    }
    missing = {key: value for key, value in additions.items() if key + "=" not in existing}
    if missing:
        with path.open("a") as output:
            output.write("\n" + "\n".join(key + "=" + value for key, value in missing.items()) + "\n")
        path.chmod(0o600)
    print("Kept existing .env settings; initialized any missing local encryption/session keys.")
else:
    db, redis, clickhouse, artifacts, operator, verifier = [secrets.token_hex(24) for _ in range(6)]
    with os.fdopen(fd, "w") as output:
        output.write(f"""ABRL_OPERATOR_TOKEN={operator}
ABRL_DB_PASSWORD={db}
ABRL_DATABASE_URL=postgresql+psycopg://abrl:{db}@127.0.0.1:55432/abrl
ABRL_REDIS_PASSWORD={redis}
ABRL_REDIS_URL=redis://:{redis}@127.0.0.1:56379/0
ABRL_CLICKHOUSE_URL=http://127.0.0.1:58123/
ABRL_CLICKHOUSE_PASSWORD={clickhouse}
ABRL_ARTIFACT_URL=http://127.0.0.1:59000
ABRL_ARTIFACT_PASSWORD={artifacts}
ABRL_WEB_ORIGIN=http://localhost:8430
ABRL_VERIFIER_TOKEN={verifier}
ABRL_PROFILE_ENCRYPTION_KEY={base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}
ABRL_CORRELATION_SECRET={secrets.token_hex(32)}
ABRL_SESSION_SECRET={secrets.token_hex(32)}
""")
    print("Created .env (owner read/write only).")

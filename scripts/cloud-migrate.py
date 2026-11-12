#!/usr/bin/env python3
"""Apply additive migrations and grant only the scaler's required table read."""

import os
import subprocess
import sys
import time

from sqlalchemy import create_engine, text


def main():
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    engine = create_engine(os.environ["ABRL_DATABASE_URL"], pool_pre_ping=True)
    deadline = time.monotonic() + 120
    try:
        while time.monotonic() < deadline:
            with engine.begin() as conn:
                if conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname='abrl_scaler'")).scalar():
                    conn.execute(text("GRANT USAGE ON SCHEMA public TO abrl_scaler"))
                    conn.execute(text("GRANT SELECT ON TABLE public.browser_sessions TO abrl_scaler"))
                    print("Schema migrated; SELECT-only scaler grant applied.")
                    return
            time.sleep(2)
        raise SystemExit("CNPG scaler role was not reconciled before migration deadline")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

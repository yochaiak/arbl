"""Validate all migrations in a new temporary PostgreSQL database, then remove it."""

import os
import subprocess
import uuid

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from abrl.config import DATABASE_URL

name = "abrl_migration_" + uuid.uuid4().hex[:10]
base = make_url(DATABASE_URL)
conn = psycopg.connect(
    base.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False),
    autocommit=True,
)
try:
    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env = {**os.environ, "ABRL_DATABASE_URL": base.set(database=name).render_as_string(hide_password=False)}
    subprocess.run(["uv", "run", "alembic", "upgrade", "head"], check=True, env=env, capture_output=True)
    subprocess.run(["uv", "run", "alembic", "check"], check=True, env=env, capture_output=True)
    print("Fresh PostgreSQL migrations and metadata consistency: PASS")
finally:
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
    conn.close()

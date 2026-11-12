import base64
import json

import pytest

# Reuse the isolated platform DB and its authorization fixture; no local credentials.
from sqlalchemy import select
from test_platform import client, create
from test_platform import db as db

from abrl import session_runner
from abrl.command_secrets import (
    CommandSecretError,
    command_fingerprint,
    discard_command,
    open_command,
    seal_command,
)
from abrl.platform_models import BrowserSession, SessionCommand, SessionEvent


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("ABRL_COMMAND_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"c" * 32).decode())
    return {"session_id": "session", "actor_id": "operator", "epoch": 7, "idempotency_key": "command-001"}


def payload(**changes):
    return {"command": "type", "text": "private typed secret", "idempotency_key": "command-001", **changes}


def test_command_encrypts_input_and_randomizes_ciphertext_without_changing_idempotency(key):
    first, digest = seal_command(payload(), **key)
    second, other = seal_command(payload(), **key)
    assert "private typed secret" not in json.dumps(first)
    assert first["encrypted_body"] != second["encrypted_body"] and digest == other
    assert open_command(first, **key) == payload()
    assert open_command(second, **key) == payload()
    assert discard_command(first) == {"command": "type", "consumed": True}
    with pytest.raises(CommandSecretError, match="discarded"):
        open_command(discard_command(first), **key)


def test_command_fingerprint_is_keyed_and_actor_and_session_bound(key, monkeypatch):
    binding = {k: key[k] for k in ("session_id", "actor_id")}
    first = command_fingerprint(payload(), **binding)
    assert first != command_fingerprint(payload(text="changed"), **binding)
    assert first != command_fingerprint(payload(), **{**binding, "actor_id": "other"})
    assert first != command_fingerprint(payload(), **{**binding, "session_id": "other"})
    monkeypatch.setenv("ABRL_COMMAND_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"d" * 32).decode())
    assert first != command_fingerprint(payload(), **binding)


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "different"),
        ("actor_id", "different"),
        ("epoch", 8),
        ("idempotency_key", "different"),
    ],
)
def test_ciphertext_cannot_move_to_another_command_identity(key, field, value):
    envelope, _ = seal_command(payload(), **key)
    with pytest.raises(CommandSecretError, match="authentication"):
        open_command(envelope, **{**key, field: value})


def test_ciphertext_and_readable_command_are_authenticated(key):
    envelope, _ = seal_command(payload(), **key)
    with pytest.raises(CommandSecretError, match="authentication"):
        open_command({**envelope, "command": "stop"}, **key)
    ciphertext = bytearray(base64.urlsafe_b64decode(envelope["encrypted_body"]))
    ciphertext[-1] ^= 1
    with pytest.raises(CommandSecretError, match="authentication"):
        open_command({**envelope, "encrypted_body": base64.urlsafe_b64encode(ciphertext).decode()}, **key)


def test_profile_key_fallback_and_missing_key_fail_closed(monkeypatch):
    monkeypatch.delenv("ABRL_COMMAND_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("ABRL_PROFILE_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"p" * 32).decode())
    binding = {
        "session_id": "session",
        "actor_id": "operator",
        "epoch": 1,
        "idempotency_key": "command-001",
    }
    envelope, _ = seal_command(payload(), **binding)
    assert open_command(envelope, **binding) == payload()
    monkeypatch.delenv("ABRL_PROFILE_ENCRYPTION_KEY")
    with pytest.raises(CommandSecretError, match="32-byte"):
        seal_command(payload(), **binding)
    # Historical pending plaintext is readable without inventing/requiring its old key.
    assert open_command(payload(), **binding) == payload()


def manual_session(db):
    create(db)
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    with db.begin() as connection:
        connection.get(BrowserSession, work.id).status = "awaiting_operator"
    return work


def test_queued_database_body_is_encrypted_then_wiped_with_stable_idempotency(db):
    work = manual_session(db)
    endpoint = f"/api/v1/sessions/{work.id}/commands"
    typed = {"command": "type", "text": "private typed secret", "idempotency_key": "private-type-001"}
    with client() as api:
        assert (
            api.post(
                endpoint, json={"command": "claim_control", "idempotency_key": "claim-001"}
            ).status_code
            == 200
        )
        assert api.post(endpoint, json=typed).status_code == 200
        assert api.post(endpoint, json=typed).status_code == 200
        with db() as connection:
            commands = connection.scalars(select(SessionCommand)).all()
            assert len(commands) == 2
            assert all("private typed secret" not in json.dumps(command.payload) for command in commands)
            command = next(command for command in commands if command.payload["command"] == "type")
            assert command.request_hash and command.payload.get("encrypted_body")
            digest = command.request_hash
            assert "private typed secret" not in json.dumps(
                [event.detail for event in connection.scalars(select(SessionEvent))]
            )
        assert session_runner.poll_control(work) == {"command": "type", "text": "private typed secret"}
        with db() as connection:
            command = connection.scalar(
                select(SessionCommand).where(SessionCommand.idempotency_key == "private-type-001")
            )
            assert command.payload == {"command": "type", "consumed": True}
            assert command.consumed_at and command.request_hash == digest
        assert api.post(endpoint, json=typed).status_code == 200
        assert api.post(endpoint, json={**typed, "text": "different secret"}).status_code == 409
        assert session_runner.poll_control(work) is None


def test_manual_command_rejects_expired_runtime_before_storing_input(db):
    work = manual_session(db)
    with db.begin() as connection:
        connection.get(BrowserSession, work.id).lease_until = "2000-01-01T00:00:00+00:00"
    with client() as api:
        response = api.post(
            f"/api/v1/sessions/{work.id}/commands",
            json={"command": "claim_control", "idempotency_key": "expired-claim"},
        )
        assert response.status_code == 409
    with db() as connection:
        assert not connection.scalars(select(SessionCommand)).all()


def test_historical_plaintext_is_consumed_wiped_and_remains_idempotent(db):
    work = manual_session(db)
    endpoint = f"/api/v1/sessions/{work.id}/commands"
    typed = {"command": "type", "text": "legacy private input", "idempotency_key": "legacy-type-001"}
    with client() as api:
        assert (
            api.post(
                endpoint, json={"command": "claim_control", "idempotency_key": "legacy-claim"}
            ).status_code
            == 200
        )
        with db.begin() as connection:
            row = connection.get(BrowserSession, work.id)
            connection.add(
                SessionCommand(
                    session_id=work.id,
                    actor_id=row.controller_id,
                    idempotency_key=typed["idempotency_key"],
                    epoch=work.epoch,
                    payload=typed,
                )
            )
        assert session_runner.poll_control(work) == {"command": "type", "text": "legacy private input"}
        with db() as connection:
            command = connection.scalar(
                select(SessionCommand).where(SessionCommand.idempotency_key == typed["idempotency_key"])
            )
            assert command.request_hash and command.consumed_at
            assert "legacy private input" not in json.dumps(command.payload)
        assert api.post(endpoint, json=typed).status_code == 200


def test_releasing_control_discards_queued_input_but_continue_preserves_order(db):
    work = manual_session(db)
    endpoint = f"/api/v1/sessions/{work.id}/commands"
    with client() as api:
        assert (
            api.post(
                endpoint, json={"command": "claim_control", "idempotency_key": "claim-001"}
            ).status_code
            == 200
        )
        assert (
            api.post(
                endpoint, json={"command": "type", "text": "obsolete input", "idempotency_key": "type-001"}
            ).status_code
            == 200
        )
        assert (
            api.post(
                endpoint, json={"command": "release_control", "idempotency_key": "release-001"}
            ).status_code
            == 200
        )
        assert (
            api.post(
                endpoint, json={"command": "claim_control", "idempotency_key": "claim-002"}
            ).status_code
            == 200
        )
        assert session_runner.poll_control(work) is None
        assert (
            api.post(
                endpoint, json={"command": "type", "text": "ordered input", "idempotency_key": "type-002"}
            ).status_code
            == 200
        )
        assert (
            api.post(endpoint, json={"command": "continue", "idempotency_key": "continue-001"}).status_code
            == 200
        )
        assert session_runner.poll_control(work) == {"command": "type", "text": "ordered input"}
        assert session_runner.poll_control(work) == {"command": "continue"}


def test_missing_command_key_returns_safe_error_without_storing_or_logging_input(db, monkeypatch):
    work = manual_session(db)
    monkeypatch.delenv("ABRL_COMMAND_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("ABRL_PROFILE_ENCRYPTION_KEY", raising=False)
    with client() as api:
        response = api.post(
            f"/api/v1/sessions/{work.id}/commands",
            json={"command": "type", "text": "never persist this", "idempotency_key": "missing-key-001"},
        )
        assert response.status_code == 503
        assert "never persist" not in response.text
    with db() as connection:
        assert not connection.scalars(select(SessionCommand)).all()


def test_stop_discards_pending_manual_input_without_replay(db):
    work = manual_session(db)
    endpoint = f"/api/v1/sessions/{work.id}/commands"
    with client() as api:
        assert (
            api.post(
                endpoint, json={"command": "claim_control", "idempotency_key": "stop-claim"}
            ).status_code
            == 200
        )
        typed = {"command": "type", "text": "cancelled input", "idempotency_key": "stop-input"}
        assert api.post(endpoint, json=typed).status_code == 200
        assert (
            api.post(endpoint, json={"command": "stop", "idempotency_key": "stop-now"}).status_code == 200
        )
        assert session_runner.poll_control(work) == {"command": "stop"}
        with db() as connection:
            commands = connection.scalars(select(SessionCommand)).all()
            assert all(command.consumed_at and command.request_hash for command in commands)
            assert all(
                command.payload.get("consumed") and "encrypted_body" not in command.payload
                for command in commands
            )
        assert api.post(endpoint, json=typed).status_code == 200

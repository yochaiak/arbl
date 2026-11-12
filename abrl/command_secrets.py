"""Authenticated, transient manual-command bodies and secret-safe idempotency.

Only the command name remains readable in the queue/audit record. API and worker
must share a stable 32-byte key; a separate command key can isolate this purpose
from durable profile encryption. Consumed bodies are replaced with tombstones.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_COMMAND_BYTES = 32 * 1024


class CommandSecretError(ValueError):
    """Safe message, without a payload, key, or vendor error text."""


def _key(purpose: str) -> bytes:
    text = os.getenv("ABRL_COMMAND_ENCRYPTION_KEY") or os.getenv("ABRL_PROFILE_ENCRYPTION_KEY")
    try:
        master = base64.b64decode(text.encode(), altchars=b"-_", validate=True) if text else b""
    except (ValueError, TypeError, UnicodeError):
        raise CommandSecretError("Configure a valid 32-byte manual-command encryption key") from None
    if len(master) != 32:
        raise CommandSecretError("Configure a valid 32-byte manual-command encryption key")
    return hmac.digest(master, ("abrl-command-v1:" + purpose).encode(), "sha256")


def _canonical(value: dict) -> bytes:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    except (TypeError, ValueError, UnicodeError):
        raise CommandSecretError("Invalid manual-command payload") from None
    if len(encoded) > MAX_COMMAND_BYTES:
        raise CommandSecretError("Manual-command payload exceeds its size budget")
    return encoded


def command_fingerprint(payload: dict, *, session_id: str, actor_id: str) -> str:
    body = _canonical({"session_id": session_id, "actor_id": actor_id, "payload": payload})
    return hmac.new(_key("idempotency"), body, hashlib.sha256).hexdigest()


def _associated(*, session_id: str, actor_id: str, epoch: int, idempotency_key: str, command: str) -> bytes:
    return _canonical(
        {
            "format": "abrl-command-v1",
            "session_id": session_id,
            "actor_id": actor_id,
            "epoch": epoch,
            "idempotency_key": idempotency_key,
            "command": command,
        }
    )


def seal_command(
    payload: dict, *, session_id: str, actor_id: str, epoch: int, idempotency_key: str
) -> tuple[dict, str]:
    if not isinstance(payload.get("command"), str) or payload.get("idempotency_key") != idempotency_key:
        raise CommandSecretError("Invalid manual-command identity")
    nonce = os.urandom(12)
    associated = _associated(
        session_id=session_id,
        actor_id=actor_id,
        epoch=epoch,
        idempotency_key=idempotency_key,
        command=payload["command"],
    )
    ciphertext = AESGCM(_key("encryption")).encrypt(nonce, _canonical(payload), associated)
    return {
        "command": payload["command"],
        "encrypted_version": 1,
        "encrypted_body": base64.urlsafe_b64encode(nonce + ciphertext).decode(),
    }, command_fingerprint(payload, session_id=session_id, actor_id=actor_id)


def open_command(
    envelope: dict, *, session_id: str, actor_id: str, epoch: int, idempotency_key: str
) -> dict:
    if envelope.get("consumed"):
        raise CommandSecretError("Manual-command body was already discarded")
    if "encrypted_version" not in envelope and "encrypted_body" not in envelope:
        # Additive migration: old pending rows remain readable, then the consumer
        # wipes their body using the same contract as newly encrypted commands.
        return dict(envelope)
    try:
        if envelope.get("encrypted_version") != 1:
            raise ValueError()
        encoded = envelope["encrypted_body"]
        if not isinstance(encoded, str) or len(encoded) > 2 * MAX_COMMAND_BYTES:
            raise ValueError()
        ciphertext = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(ciphertext) < 28:
            raise ValueError()
        associated = _associated(
            session_id=session_id,
            actor_id=actor_id,
            epoch=epoch,
            idempotency_key=idempotency_key,
            command=envelope["command"],
        )
        body = AESGCM(_key("encryption")).decrypt(ciphertext[:12], ciphertext[12:], associated)
        payload = json.loads(body)
        if (
            not isinstance(payload, dict)
            or payload.get("command") != envelope["command"]
            or payload.get("idempotency_key") != idempotency_key
        ):
            raise ValueError()
        return payload
    except CommandSecretError:
        raise
    except Exception:
        raise CommandSecretError("Manual-command authentication failed") from None


def discard_command(envelope: dict) -> dict:
    return {"command": envelope.get("command"), "consumed": True}

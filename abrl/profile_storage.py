"""Immutable, authenticated profile snapshots and same-host ownership locks.

Database ownership epochs remain the cross-worker authority. The filesystem
lock is an additional guard, never a substitute for confirming a lost runtime
has stopped before restoring its profile elsewhere.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SKIP = {".abrl.lock", "SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"}
MAX_BYTES = 2 * 1024**3
MAX_FILES = 100_000


class ProfileStorageError(ValueError):
    pass


class ProfileLock:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.fd = None

    def acquire(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(self.directory / ".abrl.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise ProfileStorageError("Profile already has a local runtime owner") from exc
        return self

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_exc):
        self.release()


def _component(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ProfileStorageError("Invalid profile or generation identifier")
    return value


class ProfileStore:
    """AES-256-GCM snapshots with metadata bound as authenticated associated data.

    The key is externally managed. `key` accepts 32 raw bytes or URL-safe base64.
    Encrypted generation files can be copied to object storage without exposing
    browser state. Applications persist the returned manifest before releasing
    the profile's distributed ownership lease.
    """

    def __init__(self, root: str | Path, key: bytes | str):
        if isinstance(key, str):
            try:
                key = base64.urlsafe_b64decode(key)
            except (ValueError, TypeError) as exc:
                raise ProfileStorageError("Profile encryption key must encode 32 bytes") from exc
        if len(key) != 32:
            raise ProfileStorageError("Profile encryption key must contain 32 bytes")
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cipher = AESGCM(key)

    def save(
        self,
        profile_id: str,
        directory: str | Path,
        *,
        clean_shutdown: bool,
        compatibility: dict,
        generation: str | None = None,
    ) -> dict:
        if not clean_shutdown:
            raise ProfileStorageError("Profile snapshot requires confirmed clean browser shutdown")
        _component(profile_id)
        generation = _component(generation or uuid4().hex)
        source = Path(directory).resolve()
        if not source.is_dir():
            raise ProfileStorageError("Profile source directory does not exist")
        with ProfileLock(source):
            buffer = io.BytesIO()
            total, count = 0, 0
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(source.rglob("*")):
                    relative = path.relative_to(source)
                    if any(p in _SKIP for p in relative.parts):
                        continue
                    if path.is_symlink():
                        raise ProfileStorageError("Profile snapshot cannot contain symbolic links")
                    if path.is_dir():
                        continue
                    if not stat.S_ISREG(path.stat().st_mode):
                        raise ProfileStorageError("Profile snapshot must contain only regular files")
                    total += path.stat().st_size
                    count += 1
                    if total > MAX_BYTES or count > MAX_FILES:
                        raise ProfileStorageError("Profile snapshot exceeds its size or file budget")
                    archive.write(path, str(relative))
        payload = buffer.getvalue()
        manifest = {
            "profile_id": profile_id,
            "generation": generation,
            "format": 1,
            "compatibility": compatibility,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "uncompressed_bytes": total,
            "files": count,
            "cipher": "AES-256-GCM",
        }
        associated = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        encrypted = nonce + self.cipher.encrypt(nonce, payload, associated)
        folder = self.root / profile_id
        folder.mkdir(mode=0o700, exist_ok=True)
        destination = folder / f"{generation}.abrl"
        # One exclusive file contains metadata and ciphertext, eliminating a
        # manifest/data rename window. Existing generations are never replaced.
        envelope = len(associated).to_bytes(4, "big") + associated + encrypted
        temporary = folder / f".{uuid4().hex}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(envelope)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise ProfileStorageError("Snapshot generation is immutable") from exc
            directory_fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            **manifest,
            "path": str(destination),
            "encrypted_sha256": hashlib.sha256(envelope).hexdigest(),
        }

    def restore(
        self, profile_id: str, generation: str, directory: str | Path, *, compatibility: dict
    ) -> dict:
        source = self.root / _component(profile_id) / f"{_component(generation)}.abrl"
        if source.stat().st_size > MAX_BYTES + 1024**2:
            raise ProfileStorageError("Encrypted snapshot exceeds size budget")
        envelope = source.read_bytes()
        length = int.from_bytes(envelope[:4], "big")
        if not 1 <= length <= 65536 or len(envelope) < length + 32:
            raise ProfileStorageError("Invalid snapshot envelope")
        associated, encrypted = envelope[4 : 4 + length], envelope[4 + length :]
        try:
            manifest = json.loads(associated)
            payload = self.cipher.decrypt(encrypted[:12], encrypted[12:], associated)
        except Exception as exc:
            raise ProfileStorageError("Snapshot authentication failed") from exc
        if manifest.get("profile_id") != profile_id or manifest.get("generation") != generation:
            raise ProfileStorageError("Snapshot identity does not match")
        if manifest.get("compatibility") != compatibility:
            raise ProfileStorageError("Profile browser/platform compatibility boundary differs")
        if hashlib.sha256(payload).hexdigest() != manifest.get("sha256"):
            raise ProfileStorageError("Snapshot checksum differs")
        destination = Path(directory).resolve()
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists():
            raise ProfileStorageError(
                "Restore requires a new directory; existing state is never overwritten"
            )
        staging = Path(tempfile.mkdtemp(prefix=".abrl-restore-", dir=destination.parent))
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_BYTES:
                    raise ProfileStorageError("Snapshot exceeds extraction budget")
                for item in infos:
                    name = PurePosixPath(item.filename)
                    mode = item.external_attr >> 16
                    if (
                        name.is_absolute()
                        or ".." in name.parts
                        or "\\" in item.filename
                        or stat.S_ISLNK(mode)
                    ):
                        raise ProfileStorageError("Unsafe snapshot entry")
                    target = staging.joinpath(*name.parts)
                    if item.is_dir():
                        target.mkdir(mode=0o700, parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with target.open("xb") as output, archive.open(item) as content:
                        shutil.copyfileobj(content, output)
                    target.chmod(0o600)
            os.rename(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return manifest

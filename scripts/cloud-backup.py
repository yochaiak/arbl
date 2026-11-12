#!/usr/bin/env python3
"""Authenticated chunk encryption and cross-location object backup. Run with `uv run`.

No server-side CopyObject is used: Hetzner SSE-C objects cannot use that path.
Keys are supplied through ABRL_BACKUP_ENCRYPTION_KEY (base64 32 bytes).
"""

import argparse
import base64
import hashlib
import io
import json
import os
import struct
import tempfile
from pathlib import Path

MAGIC = b"ABRLENC1"
CHUNK = 8 * 1024 * 1024


def encrypt_stream(source, destination, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aes = AESGCM(key)
    identifier = os.urandom(16)
    destination.write(MAGIC + identifier)
    index = 0
    digest = hashlib.sha256()
    total = 0
    while True:
        block = source.read(CHUNK)
        if not block:
            break
        digest.update(block)
        total += len(block)
        nonce = os.urandom(12)
        encrypted = aes.encrypt(nonce, block, MAGIC + identifier + struct.pack(">Q", index))
        destination.write(struct.pack(">I", len(encrypted)) + nonce + encrypted)
        index += 1
    # Authenticated terminal frame prevents silent truncation, including truncation
    # precisely at a chunk boundary or substitution from a different object.
    trailer = json.dumps(
        {"sha256": digest.hexdigest(), "bytes": total, "chunks": index}, sort_keys=True
    ).encode()
    nonce = os.urandom(12)
    encrypted = aes.encrypt(nonce, trailer, MAGIC + identifier + b"END" + struct.pack(">Q", index))
    destination.write(struct.pack(">I", 0) + nonce + struct.pack(">I", len(encrypted)) + encrypted)
    return {"sha256": digest.hexdigest(), "bytes": total, "chunks": index, "format": MAGIC.decode()}


def read_exact(stream, length):
    data = stream.read(length)
    if len(data) != length:
        raise ValueError("Truncated encrypted backup")
    return data


def decrypt_stream(source, destination, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if read_exact(source, 8) != MAGIC:
        raise ValueError("Unsupported encrypted backup format")
    identifier = read_exact(source, 16)
    aes = AESGCM(key)
    digest = hashlib.sha256()
    total, index = 0, 0
    while True:
        size = struct.unpack(">I", read_exact(source, 4))[0]
        nonce = read_exact(source, 12)
        if size == 0:
            size = struct.unpack(">I", read_exact(source, 4))[0]
            if size > 4096:
                raise ValueError("Invalid terminal frame")
            trailer = json.loads(
                aes.decrypt(
                    nonce, read_exact(source, size), MAGIC + identifier + b"END" + struct.pack(">Q", index)
                )
            )
            if trailer != {"sha256": digest.hexdigest(), "bytes": total, "chunks": index} or source.read(1):
                raise ValueError("Encrypted backup integrity mismatch")
            return trailer
        if size > CHUNK + 16 or size < 16:
            raise ValueError("Invalid encrypted chunk size")
        block = aes.decrypt(nonce, read_exact(source, size), MAGIC + identifier + struct.pack(">Q", index))
        destination.write(block)
        digest.update(block)
        total += len(block)
        index += 1


def client(prefix):
    import boto3

    endpoint = os.environ[prefix + "_ENDPOINT"]
    if not endpoint.startswith("https://"):
        raise ValueError("Object-storage endpoint must use HTTPS")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ[prefix + "_ACCESS_KEY"],
        aws_secret_access_key=os.environ[prefix + "_SECRET_KEY"],
    )


def mirror(source, destination, source_bucket, destination_bucket, key, source_sse=None):
    """Encrypted immutable generations. Never delete backups when source objects expire."""
    count = 0
    for page in source.get_paginator("list_objects_v2").paginate(Bucket=source_bucket):
        for obj in page.get("Contents", []):
            # Include source ETag + size in generation so modified objects never overwrite history.
            generation = hashlib.sha256(
                (source_bucket + "/" + obj["Key"] + obj["ETag"] + str(obj["Size"])).encode()
            ).hexdigest()
            destination_key = "objects/" + generation + ".abrlenc"
            try:
                existing = destination.head_object(Bucket=destination_bucket, Key=destination_key)
            except Exception as exc:
                if getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                    raise
            else:
                try:
                    destination.head_object(
                        Bucket=destination_bucket, Key="manifests/" + generation + ".abrlenc"
                    )
                except Exception as exc:
                    if (
                        getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
                        != 404
                    ):
                        raise
                else:
                    continue
                # A process can stop after the object upload but before its manifest.
                # Recover the missing manifest without overwriting the immutable object.
                digest = existing.get("Metadata", {}).get("plaintext-sha256")
                if not digest:
                    raise ValueError("Existing encrypted object lacks checksum metadata")
                payload = json.dumps(
                    {
                        "source_key": obj["Key"],
                        "source_bucket": source_bucket,
                        "source_etag": obj["ETag"],
                        "object": destination_key,
                        "sha256": digest,
                        "bytes": obj["Size"],
                        "chunks": (obj["Size"] + CHUNK - 1) // CHUNK,
                        "format": MAGIC.decode(),
                    }
                ).encode()
                repaired = io.BytesIO()
                encrypt_stream(io.BytesIO(payload), repaired, key)
                destination.put_object(
                    Bucket=destination_bucket,
                    Key="manifests/" + generation + ".abrlenc",
                    Body=repaired.getvalue(),
                )
                count += 1
                continue
            response = source.get_object(
                Bucket=source_bucket, Key=obj["Key"], IfMatch=obj["ETag"], **(source_sse or {})
            )
            with tempfile.TemporaryFile() as ciphertext:
                meta = encrypt_stream(response["Body"], ciphertext, key)
                ciphertext.seek(0)
                destination.upload_fileobj(
                    ciphertext,
                    destination_bucket,
                    destination_key,
                    ExtraArgs={"Metadata": {"format": MAGIC.decode(), "plaintext-sha256": meta["sha256"]}},
                )
            # Encrypt names/manifests too: object names can carry private metadata.
            payload = json.dumps(
                {
                    "source_key": obj["Key"],
                    "source_bucket": source_bucket,
                    "source_etag": obj["ETag"],
                    "object": destination_key,
                    **meta,
                }
            ).encode()
            encrypted_meta = io.BytesIO()
            encrypt_stream(io.BytesIO(payload), encrypted_meta, key)
            destination.put_object(
                Bucket=destination_bucket,
                Key="manifests/" + generation + ".abrlenc",
                Body=encrypted_meta.getvalue(),
            )
            count += 1
    return count


def sse_parameters(name):
    value = os.getenv(name)
    if not value:
        return {}
    key = base64.b64decode(value, validate=True)
    if len(key) != 32:
        raise ValueError(name + " must decode to 32 bytes")
    return {"SSECustomerAlgorithm": "AES256", "SSECustomerKey": key}


class HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, count=-1):
        data = self.stream.read(count)
        self.digest.update(data)
        return data


def mirror_native_sse(
    source, destination, source_bucket, destination_bucket, source_sse, destination_sse, prefix=""
):
    """Retains original Barman paths for direct encrypted PITR from the DR bucket.

    The destination must have versioning enabled. No deletion propagates. Repeated
    copies compare source ETag metadata; a full readback verifies every new copy.
    """
    if not destination_sse:
        raise ValueError("Native backup replication requires a destination SSE-C key")
    if destination.get_bucket_versioning(Bucket=destination_bucket).get("Status") != "Enabled":
        raise ValueError("Native DR bucket must have versioning enabled")
    count = 0
    for page in source.get_paginator("list_objects_v2").paginate(Bucket=source_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            try:
                existing = destination.head_object(
                    Bucket=destination_bucket, Key=obj["Key"], **destination_sse
                )
                if existing.get("Metadata", {}).get("source-etag") == obj["ETag"]:
                    receipt_key = (
                        ".abrl-verified/"
                        + hashlib.sha256(
                            (source_bucket + "/" + obj["Key"] + obj["ETag"]).encode()
                        ).hexdigest()
                    )
                    try:
                        receipt = destination.get_object(
                            Bucket=destination_bucket, Key=receipt_key, **destination_sse
                        )
                    except Exception as exc:
                        if (
                            getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
                            != 404
                        ):
                            raise
                    else:
                        proof = json.loads(receipt["Body"].read())
                        if proof.get("source_etag") == obj["ETag"]:
                            continue
            except Exception as exc:
                if getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                    raise
            response = source.get_object(
                Bucket=source_bucket, Key=obj["Key"], IfMatch=obj["ETag"], **source_sse
            )
            reader = HashReader(response["Body"])
            destination.upload_fileobj(
                reader,
                destination_bucket,
                obj["Key"],
                ExtraArgs={
                    **destination_sse,
                    "Metadata": {"source-etag": obj["ETag"], "source-bucket": source_bucket},
                },
            )
            copied = destination.get_object(Bucket=destination_bucket, Key=obj["Key"], **destination_sse)
            digest = hashlib.sha256()
            while block := copied["Body"].read(CHUNK):
                digest.update(block)
            if digest.digest() != reader.digest.digest():
                raise ValueError("Cross-location backup readback checksum mismatch")
            receipt_key = (
                ".abrl-verified/"
                + hashlib.sha256((source_bucket + "/" + obj["Key"] + obj["ETag"]).encode()).hexdigest()
            )
            destination.put_object(
                Bucket=destination_bucket,
                Key=receipt_key,
                Body=json.dumps({"source_etag": obj["ETag"], "sha256": digest.hexdigest()}).encode(),
                **destination_sse,
            )
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["encrypt", "decrypt", "mirror"])
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--source-bucket")
    parser.add_argument("--destination-bucket")
    parser.add_argument(
        "--native-sse", action="store_true", help="Keep native Barman paths using destination SSE-C"
    )
    parser.add_argument(
        "--prefix", default="", help="Restrict native replication to a source prefix, e.g. WAL-only"
    )
    args = parser.parse_args()
    key = base64.b64decode(os.getenv("ABRL_BACKUP_ENCRYPTION_KEY", ""), validate=True)
    if not args.native_sse and len(key) != 32:
        parser.error("ABRL_BACKUP_ENCRYPTION_KEY must decode to 32 bytes")
    if args.operation == "mirror":
        if not args.source_bucket or not args.destination_bucket:
            parser.error("mirror requires both bucket names")
        source_sse = sse_parameters("ABRL_BACKUP_SOURCE_SSE_KEY")
        if args.native_sse:
            count = mirror_native_sse(
                client("ABRL_BACKUP_SOURCE"),
                client("ABRL_BACKUP_DESTINATION"),
                args.source_bucket,
                args.destination_bucket,
                source_sse,
                sse_parameters("ABRL_BACKUP_DESTINATION_SSE_KEY"),
                args.prefix,
            )
        else:
            count = mirror(
                client("ABRL_BACKUP_SOURCE"),
                client("ABRL_BACKUP_DESTINATION"),
                args.source_bucket,
                args.destination_bucket,
                key,
                source_sse,
            )
        print(json.dumps({"encrypted_objects_copied": count}))
    else:
        if not args.input or not args.output:
            parser.error("encrypt/decrypt requires --input and --output")
        output = Path(args.output)
        if output.exists():
            parser.error("Refusing to overwrite an existing output")
        # On any authentication error remove the partial plaintext, never present it as restored.
        try:
            with open(args.input, "rb") as source, open(output, "xb") as dest:
                os.chmod(output, 0o600)
                result = (encrypt_stream if args.operation == "encrypt" else decrypt_stream)(
                    source, dest, key
                )
            print(json.dumps(result))
        except Exception:
            output.unlink(missing_ok=True)
            raise


if __name__ == "__main__":
    main()

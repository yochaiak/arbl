# Recovery and operational evidence

The target is single control/data-host recovery within two minutes, no loss of acknowledged PostgreSQL metadata under required synchronous replication, regional RPO <=15 minutes and RTO <=4 hours. These are acceptance targets until measured in a deployed environment. One primary location provides host-level HA; it does not provide uninterrupted regional failover.

## Stop admission and preserve evidence

Pause affected schedules/batches and stop new admission before maintenance. Existing browser Jobs are non-evictable for autoscaler scale-down and must finish or be explicitly cancelled. Cancellation may lead to `needs_reconciliation` after an ambiguous external action; never replay such work automatically. Save the immutable trial/attempt/session evidence before clearing operational state.

If a worker loses connectivity, keep its persistent profile quarantined and its proxy capacity reserved. A heartbeat timeout proves loss of contact, not termination. Confirm the old pod UID and browser/vendor runtime have stopped and the network route is closed before recovery. The cloud controller issues UID-preconditioned deletions and waits for disappearance; inability to prove shutdown remains a quarantine condition. Do not force-delete a profile lock merely to free a slot.

## One-node failures

- **Control node:** confirm two K3s/etcd voters remain healthy, API requests continue and scheduler leadership moves without duplicate occurrences. Replace the failed server using the recorded version and externally retained join token. Do not `cluster-reset` a healthy quorum.
- **PostgreSQL node:** inspect CNPG conditions and `pg_stat_replication`. Required synchronous durability intentionally stops acknowledging writes if no synchronous standby exists. Let CNPG promote a valid standby; compare acknowledged probe IDs before/after and measure recovery from the failure timestamp.
- **Redis node:** query all three Sentinels with the configured password; verify agreement on `abrl-master`. A restarted Redis instance asks Sentinel before taking a primary role. Redis is not the authority for runs; durable pending work must be re-dispatched from PostgreSQL after recovery.
- **ClickHouse/Keeper node:** confirm Keeper still has quorum and the remaining ClickHouse replicas can query `abrl.events FINAL`. Stop analytics delivery if necessary; retain PostgreSQL outbox records. Resume and compare distinct event sequence IDs to the authoritative outbox, including redelivery.
- **Browser node:** stop admission to the node, confirm runtime fencing, preserve incomplete attempts, then restore encrypted snapshots only within a tested browser/OS boundary. Do not count recovered attempts as new research samples.

Perform each failure in a dedicated rehearsal project. Record the failure action, timestamps, outstanding acknowledged IDs, occurrence keys, profile owners, proxy leases, API latency and return-to-ready time. Pod deletion alone is not a host-failure rehearsal; a real node power-off and a separate network partition are required gates.

## Encrypted backup configuration

The Barman ObjectStore references `abrl-backup-s3` keys `ACCESS_KEY_ID`, `ACCESS_SECRET_KEY`, `SSE_CUSTOMER_KEY`. The last value is a base64 32-byte AES key. Current Barman Cloud v0.16 supports this for every archive and recovery operation. Enable daily physical base backups and continuous WAL archiving (`archive_timeout=60s`). Confirm that base backups complete, `ContinuousArchiving=True`, and downloading a stored object without the customer key fails.

The cross-location CronJob Secret uses:

```text
ABRL_BACKUP_SOURCE_ENDPOINT=https://fsn1.your-objectstorage.com
ABRL_BACKUP_SOURCE_ACCESS_KEY=<source access key>
ABRL_BACKUP_SOURCE_SECRET_KEY=<source secret>
ABRL_BACKUP_SOURCE_SSE_KEY=<base64 32-byte source key, for Barman objects>
ABRL_BACKUP_DESTINATION_ENDPOINT=https://hel1.your-objectstorage.com
ABRL_BACKUP_DESTINATION_ACCESS_KEY=<destination access key>
ABRL_BACKUP_DESTINATION_SECRET_KEY=<destination secret>
ABRL_BACKUP_DESTINATION_SSE_KEY=<base64 32-byte DR key, native mirror>
ABRL_BACKUP_ENCRYPTION_KEY=<independent base64 32-byte key, chunk mirror>
```

Configure one native Barman source bucket per destination namespace/path; custom encrypted object mirrors include the source bucket in the generation identity. Destination Barman bucket versioning is required. Cross-location copy downloads then uploads, because Hetzner does not provide built-in replication or SSE-C CopyObject. Never propagate source retention deletion into the DR bucket. Full mirrors run every five minutes and an independent WAL-only mirror every minute. Monitor replication job completion and oldest uncopied WAL; these schedules alone do not prove the 15-minute RPO.

For source state objects protected by Terraform SSE-C, provide `ABRL_BACKUP_SOURCE_SSE_KEY` when mirroring to a separate encrypted history bucket. Do not version the active state/lock bucket. Recovery restores a chosen verified state generation; release stale lockfiles only after confirming no Terraform process owns the lock.

## Restore PostgreSQL into a new environment

1. Recreate the fixed foundation in the recovery location using reviewed Terraform inputs, current quota evidence and the retained state/recovery keys. Install matching K3s, CNPG and Barman plugin versions. Keep schedulers/admission stopped.
2. Install a Secret with the DR bucket credentials and its SSE-C key. Generate a new cluster manifest:

```sh
python3 scripts/cloud-restore.py --name abrl-rehearsal-restore \
  --endpoint https://hel1.your-objectstorage.com \
  --destination s3://abrl-dr-encrypted/backups/ \
  --secret abrl-dr-backup-s3 --output /tmp/abrl-restore.json
kubectl apply -f /tmp/abrl-restore.json
kubectl -n abrl wait cluster/abrl-rehearsal-restore --for=condition=Ready --timeout=3600s
```

3. The script refuses to reuse the source cluster name. Optional `--target-time` requests point-in-time recovery. It deliberately does not configure archiving back into the source path. Set a **new** archive destination/key path before allowing writes in the recovered system.
4. Compare known acknowledged IDs, immutable definitions, trial counts, observation signatures/verdicts, outbox sequence, schedules and profile generations. Measure newest recoverable committed timestamp (RPO), and elapsed time until the verified application can admit new work (RTO).
5. Restore artifacts/profile snapshots and validate checksums/decryption. For chunk-encrypted mirror objects, decrypt the encrypted manifest to obtain the original key and SHA-256, then decrypt its object. `cloud-backup.py decrypt` refuses corruption, wrong keys and truncation and removes partial plaintext after an error. Preserve mode 0600 for recovered files.
6. Confirm old region/runtime fencing before enabling schedules or persistent-profile ownership. Select the recovered database DSN, verify OIDC and secrets, run migration/health checks, and admit a small owned-fixture batch before broader load.

## Release evidence required

Store timestamped Terraform plan/apply identity, operator/image versions, readiness JSON, fresh migration output, browser isolation results, actual 100/250/500 load results, provider/licensed adapter limits, failure-injection evidence, WAL replication lag and restore reconciliation. Include failures and elapsed times. Do not classify a check as “blocked by credentials” when implementation is still absent; keep those statuses separate in the requirement ledger.

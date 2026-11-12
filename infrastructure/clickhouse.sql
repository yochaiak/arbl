CREATE DATABASE IF NOT EXISTS abrl;
CREATE TABLE IF NOT EXISTS abrl.events (
    sequence UInt64,
    run_id String,
    kind LowCardinality(String),
    detail String,
    occurred_at String
) ENGINE = ReplacingMergeTree ORDER BY sequence;

CREATE TABLE IF NOT EXISTS abrl.session_events
(
    sequence UInt64,
    project_id String,
    session_id String,
    kind String,
    detail String,
    created_at String
)
ENGINE = ReplacingMergeTree
ORDER BY (project_id, sequence);

CREATE DATABASE IF NOT EXISTS abrl;
CREATE TABLE IF NOT EXISTS abrl.events (
    sequence UInt64,
    run_id String,
    kind LowCardinality(String),
    detail String,
    occurred_at String
) ENGINE = ReplacingMergeTree ORDER BY sequence;

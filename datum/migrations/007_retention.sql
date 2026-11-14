-- Log lines stay 30 days. The daily stats keep the volume of each day. 008_samples_rollup.sql owns the TTL of samples.
ALTER TABLE datum.logs MODIFY TTL toDateTime(ts) + INTERVAL 30 DAY;

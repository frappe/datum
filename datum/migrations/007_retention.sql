-- Samples stay 90 days and log lines 30 days. The daily stats keep the volume of each day.
ALTER TABLE datum.samples MODIFY TTL toDateTime(ts) + INTERVAL 90 DAY;
ALTER TABLE datum.logs MODIFY TTL toDateTime(ts) + INTERVAL 30 DAY;

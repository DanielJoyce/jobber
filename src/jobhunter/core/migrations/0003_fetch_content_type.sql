-- 0003_fetch_content_type: keep the response Content-Type on each fetch_log row, so ttl hits and
-- 304s (served from the cache, with no headers on the wire) can still choose the right charset.
ALTER TABLE fetch_log ADD COLUMN content_type TEXT;

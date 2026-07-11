# API Rate Limiting

> **Status:** Active — added 2026-07-06 (Phase 6a)
> **Audience:** Operators, administrators, self-hosted users

## Overview

DocMind ships with an in-memory per-IP rate limiter that caps the number of
requests a single client can make within a rolling 60-second window. When the
limit is exceeded, the server responds with HTTP 429 and a `Retry-After`
header so well-behaved clients can back off.

Rate limiting is **disabled by default** — matching the open behaviour of a
self-hosted single-user deployment. Enable it when you expose DocMind to
multiple users or the public internet.

## Quick start

```bash
# Enable rate limiting (default: 60 requests per minute per IP)
export DOCMIND_RATE_LIMIT_ENABLED=true

# Optional: adjust the limit
export DOCMIND_RATE_LIMIT_REQUESTS_PER_MINUTE=120
```

No external dependencies. No Redis. No configuration files. The limiter runs
entirely in-process with sub-millisecond overhead.

## Behaviour

### The sliding window

The rate limiter uses a **per-IP sliding window**. Every client (identified
by its remote IP address) gets a bucket that holds the timestamps of their
requests from the last 60 seconds. On each request:

1. Timestamps older than 60 seconds are pruned from the bucket.
2. If the bucket has fewer entries than the configured limit, the request is
   allowed and a new timestamp is appended.
3. If the bucket is full, the request is rejected.

When the oldest request in a full bucket expires, a new slot opens and that
client can resume. No hard reset at minute boundaries — it's a true rolling
window.

### The 429 response

When a client exceeds the limit, the server returns:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 42
Content-Type: application/json

{
  "error": "RATE_LIMIT",
  "message": "Too many requests. Please slow down.",
  "retry_after": 42
}
```

| Field           | Description                                                |
|-----------------|------------------------------------------------------------|
| `error`         | Always `"RATE_LIMIT"` — clients can match on this string.  |
| `message`       | Human-readable description of the error.                   |
| `retry_after`   | Seconds until the next request slot opens.                 |
| `Retry-After`   | Same value as a standard HTTP header.                      |

The `retry_after` value is always an integer between 1 and 60. It is
computed from the time remaining until the bucket's oldest entry expires,
rounded up to the nearest second. It is expressed as delta-seconds (not
HTTP-date), which is valid per RFC 7231 §7.1.3.

### Client guidance

Well-behaved HTTP clients should:

1. Check for a 429 status code on every response.
2. Read the `Retry-After` header (or the `retry_after` field from the JSON
   body).
3. Wait that many seconds before retrying.
4. Implement exponential backoff with jitter if the limit is hit
   repeatedly.

Example in Python:

```python
import time
import httpx

def get_with_retry(url, max_retries=3):
    for attempt in range(max_retries):
        resp = httpx.get(url)
        if resp.status_code != 429:
            return resp
        retry_after = int(resp.headers.get("retry-after", 5))
        time.sleep(retry_after)
    raise Exception("Rate limit exceeded after max retries")
```

### Monitoring

Rate limit rejections are logged at WARNING level:

```
Rate limit exceeded for client 192.168.1.42 on /api/v1/documents (max 60/min)
```

Monitor these log lines to detect:
- Abusive clients hammering the API
- Legitimate clients that need a higher limit
- Configuration errors (limit set too low)

## Exempt paths

The following paths are **never rate limited**, even when the feature is
enabled:

| Path              | Reason                        |
|-------------------|-------------------------------|
| `/health`         | Health checks need unfettered access |
| `/login`, `/logout` | Auth flows must never be throttled |
| `/docs`, `/redoc`, `/openapi.json` | API documentation |
| `/static/*`       | Static assets (CSS, JS, images) |

This mirrors the auth middleware's notion of public paths. Rate limiting
and authentication share the same exemption list so behaviour is
consistent.

## Configuration reference

Rate limiting is configured through **environment variables**. There
are no YAML settings for rate limiting — env vars are the canonical config
source to keep the deployment surface simple.

| Variable                                  | Default | Description                                                  |
|-------------------------------------------|---------|--------------------------------------------------------------|
| `DOCMIND_RATE_LIMIT_ENABLED`              | `false` | Enable or disable the rate limiter.                          |
| `DOCMIND_RATE_LIMIT_REQUESTS_PER_MINUTE`  | `60`    | Max requests per client IP per sliding 60-second window.     |
| `DOCMIND_RATE_LIMIT_TRUSTED_PROXY_IPS`    | (empty) | Comma-separated list of trusted reverse proxy IPs.           |

### Determining the right limit

The default of 60 requests/minute is conservative for a single user
browsing the UI (each page load typically makes 1–3 API calls). For
reference:

| Deployment scenario              | Suggested RPM |
|----------------------------------|---------------|
| Single user, localhost           | Disabled      |
| Small team (2–5 users)           | 60            |
| Medium team (6–20 users)         | 120           |
| Large team or public exposure    | 300+          |
| API behind a reverse proxy       | See proxy section below |

The rate limiter is per-IP, so in practice each user gets their own
budget. A setting of 60 RPM means each individual browser or API client
can make up to one request per second on average, with bursts up to 60
in rapid succession followed by a cooldown.

## Reverse proxy considerations

When DocMind sits behind a reverse proxy (nginx, Caddy, Traefik, etc.),
the rate limiter sees the proxy's IP address — not the end user's —
unless you configure trusted proxy IP handling.

### The problem

By default, `request.client.host` returns the direct TCP peer IP. When
behind a proxy, this is always the proxy's IP, so all users share a
single rate-limit bucket. That effectively limits your entire user base
to the configured RPM combined.

### The solution: trusted_proxy_ips

DocMind's rate limiter does **not** blindly trust the `X-Forwarded-For`
header (which is trivially spoofable). Instead, it only consults
`X-Forwarded-For` when the direct peer IP is in the configured
`trusted_proxy_ips` list. This prevents IP spoofing attacks that could
bypass rate limiting.

Configure the proxy's IP address(es):

```bash
# Single proxy
export DOCMIND_RATE_LIMIT_TRUSTED_PROXY_IPS=10.0.0.1

# Multiple proxies (comma-separated)
export DOCMIND_RATE_LIMIT_TRUSTED_PROXY_IPS=10.0.0.1,10.0.0.2,192.168.1.1
```

When a request arrives from a trusted proxy IP, the rate limiter uses
the **rightmost** entry in the `X-Forwarded-For` header — this is the
IP appended by the trusted proxy itself and represents the real client.
The leftmost entries are client-controlled and may be spoofed, so they
are never used.

### Proxy configuration

Configure your reverse proxy to set `X-Forwarded-For` with the real
client IP. The proxy should strip or overwrite any incoming
`X-Forwarded-For` header from the client before appending the real IP.

**nginx:**

```nginx
location / {
    proxy_pass http://127.0.0.1:9980;
    proxy_set_header X-Forwarded-For $remote_addr;
    # $remote_addr is the real client IP as seen by nginx
}
```

**Caddy:**

```
reverse_proxy 127.0.0.1:9980 {
    header_up X-Forwarded-For {remote_host}
}
```

### Security warning

**Only list IPs you control in `trusted_proxy_ips`.** If you list an
untrusted IP, any client connecting from that IP can spoof their
`X-Forwarded-For` header to get a fresh rate-limit bucket on every
request, effectively bypassing rate limiting.

When `trusted_proxy_ips` is empty (the default), `X-Forwarded-For` is
never consulted and `request.client.host` is used directly. This is the
most secure configuration for direct (non-proxied) deployments.

### Uvicorn --proxy-headers

If you use Uvicorn's `--proxy-headers` flag, `request.client.host` will
reflect the XFF header directly. **Do not use `--proxy-headers` with
`--forwarded-allow-ips='*'`** — this makes Uvicorn trust XFF from any
source, which defeats the rate limiter's spoofing protection.

If you use `--proxy-headers`, restrict it to your proxy's IP:

```bash
uvicorn src.web.server:app --proxy-headers --forwarded-allow-ips='10.0.0.1'
```

However, the recommended approach is to **not** use `--proxy-headers`
and instead set `DOCMIND_RATE_LIMIT_TRUSTED_PROXY_IPS`. This keeps the
spoofing protection in the rate limiter's own code rather than relying
on Uvicorn's ASGI-level header parsing.

## Architecture

The rate limiter is implemented in `src/web/rate_limit.py`:

- **`RateLimiter` class** — In-memory sliding window. Maintains a
  `defaultdict[str, list[float]]` mapping IP → request timestamps.
  Prunes expired entries on every check. Thread-safe for the single-worker
  asyncio deployment (no locks needed — FastAPI's event loop is
  single-threaded by default).
- **`_client_key()`** — Extracts the client identifier from the request.
  Uses `request.client.host` (the direct TCP peer IP) by default. Only
  consults `X-Forwarded-For` when the direct peer is a trusted proxy,
  using the rightmost (proxy-appended) entry for anti-spoofing.
- **`rate_limit_middleware`** — FastAPI/Starlette ASGI middleware. When
  `config.rate_limit.enabled` is `False`, it's a pass-through with zero
  overhead (one bool check). When enabled, it checks the limiter and
  returns 429 on overflow.
- **`get_rate_limiter()`** — Module-level singleton factory. Created on
  first access using the current config. Call `_reinit_rate_limiter()` to
  discard and recreate (used when config changes at runtime or in tests).

The middleware is registered after the auth middleware in `server.py`, so
rate limiting runs on every request that survives authentication. Public
paths are exempt from both.

### Limitations

The in-memory limiter is designed for DocMind's single-instance deployment
model:

- **Not distributed.** In multi-worker setups (e.g. `DOCMIND_WORKERS > 1`
  with Uvicorn), each worker has its own limiter instance with independent
  state. A client hitting 3 workers gets 3× the limit. For multi-worker
  deployments that need accurate global limits, consider a Redis-backed
  limiter (not yet implemented — file an issue if you need this).
- **Per-IP only.** The limiter cannot distinguish between different users
  behind the same NAT or VPN. If you need per-user or per-API-key rate
  limiting, extend the `_client_key()` method.
- **No burst vs sustained distinction.** The sliding window is a single
  counter — there's no separate burst allowance. The practical effect is
  that clients can burst up to the full RPM limit and then must wait for
  slots to open organically.

## Testing

Rate limiting is tested in `tests/test_rate_limit.py`, covering:

- `RateLimiter` unit tests: sliding window logic, pruning, `retry_after`
  computation, per-IP isolation, window expiry, reset, unknown client
  fallback
- `RateLimitConfig` unit tests: dataclass fields, env var parsing,
  defaults, `trusted_proxy_ip_set` property
- `_client_key` X-Forwarded-For tests: trusted proxy validation, rightmost
  vs leftmost XFF entry, spoofing prevention, empty/malformed XFF fallback
- Bucket cleanup tests: periodic pruning of empty bucket entries
- Middleware integration tests (ASGI client): passthrough when disabled,
  429 response shape, `Retry-After` header presence, exempt path behaviour,
  per-test state isolation

Run the rate limit tests in isolation:

```bash
pytest tests/test_rate_limit.py -v
```

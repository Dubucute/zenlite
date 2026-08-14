"""Unit tests for the proxy rotation logic.

Plain-python (no pytest required): run with `python test_rotation.py`.
Exits non-zero if any check fails.

Covers the pure logic only — no network:
  - quota-exhaustion detection (`_is_quota_exhausted`)
  - upstream error message extraction (`_extract_error_message`)
  - verdict error hierarchy (`UpstreamStatusError` ⊂ `UpstreamVerdictError`)
  - known-good proxy ordering + blacklist handling (`_good_queue` / `_next_proxy`)
"""

import json

from app.proxy.manager import (
    ProxyManager,
    UpstreamStatusError,
    UpstreamVerdictError,
    _extract_error_message,
    _is_quota_exhausted,
)

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILURES.append(name)


def quota_detection() -> None:
    check(
        "quota: FreeUsageLimitError detected",
        _is_quota_exhausted(
            '{"error":{"name":"FreeUsageLimitError","message":"Rate limit exceeded"}}'
        ),
    )
    check("quota: 'rate limit exceeded' text detected",
          _is_quota_exhausted("Rate limit exceeded"))
    check("quota: transient 429 body is NOT quota",
          not _is_quota_exhausted('{"error":{"message":"Server busy, retry later"}}'))
    check("quota: empty body is NOT quota", not _is_quota_exhausted(""))


def error_message_extraction() -> None:
    body = json.dumps({
        "error": {"message": "The model is out of credits", "type": "invalid_request_error"},
    })
    check("extract: message pulled from JSON error body",
          _extract_error_message(body) == "The model is out of credits")
    check("extract: raw fallback on non-JSON", _extract_error_message("plain text") == "plain text")
    check("extract: raw fallback on truncated JSON",
          _extract_error_message('{"error":{"mess') == '{"error":{"mess')
    check("extract: raw fallback on empty", _extract_error_message("") == "")


def verdict_hierarchy() -> None:
    check("UpstreamStatusError is an UpstreamVerdictError",
          issubclass(UpstreamStatusError, UpstreamVerdictError))
    e = UpstreamStatusError(429, "nope")
    check("verdict carries status + message", e.status_code == 429 and "HTTP 429" in str(e))


def good_proxy_ordering() -> None:
    m = ProxyManager()
    m._ipvanish_pool = ["p1", "p2", "p3", "p4"]
    m._blacklist = {"p2": 9e9}  # effectively permanent

    m._mark_good("p3")
    m._mark_good("p1")  # most recent

    queue = m._good_queue()
    first = m._next_proxy(queue)
    second = m._next_proxy(queue)
    check("good queue drains most-recent-first", (first, second) == ("p1", "p3"))

    # Queue is empty now: fall back to round-robin pool (index 0 -> p1).
    third = m._next_proxy(queue)
    check("pool round-robin after good queue empties", third == "p1")
    # Next pool slot (index 1 -> p2) is blacklisted -> skip to p3.
    fourth = m._next_proxy(queue)
    check("round-robin skips blacklisted proxies", fourth == "p3")

    # A known-good proxy that gets blacklisted must leave the queue.
    m2 = ProxyManager()
    m2._ipvanish_pool = ["p1"]
    m2._mark_good("p1")
    m2._blacklist["p1"] = 9e9
    check("blacklisted good proxy excluded from queue", m2._good_queue() == [])
    try:
        m2._next_proxy(m2._good_queue())
        check("all-blacklisted pool raises", False)
    except Exception:
        check("all-blacklisted pool raises", True)


def main() -> None:
    quota_detection()
    error_message_extraction()
    verdict_hierarchy()
    good_proxy_ordering()
    print(f"\n{len(_FAILURES)} failure(s)" if _FAILURES else "\nAll checks passed.")
    raise SystemExit(1 if _FAILURES else 0)


if __name__ == "__main__":
    main()

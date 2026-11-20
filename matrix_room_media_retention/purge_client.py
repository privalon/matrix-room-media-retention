"""Thin client for matrix-media-repo's own room-scoped purge admin API.

Confirmed against matrix-media-repo's own docs (docs/admin.md, t2bot/
matrix-media-repo): `POST /_matrix/media/unstable/admin/purge/room/<room_id>`
with a `before_ts` query param (milliseconds) deletes all media known to
that room created/last-used before that timestamp -- local or remote,
regardless of which bridge (if any) produced it -- while leaving the
referencing Matrix events themselves untouched. Auth is a normal Matrix
access token belonging to an account matrix-media-repo's own config lists
as an admin, not a separate API key system.

No native dry-run parameter exists on this endpoint (confirmed against the
same docs) -- roadmap/041 §9's dry-run mode is implemented one layer up, in
the scheduler, by simply not calling purge_room() at all when dry_run is
set, rather than by any flag this client sends to matrix-media-repo itself.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

# matrix-media-repo rate-limits its own admin API (HTTP 429, M_LIMIT_EXCEEDED).
# Found live 2026-11-19: during the top-N report 140 of 165 per-room usage
# calls were rate limited and silently skipped, so the largest rooms (1.6 GB)
# were missing and the report showed ~17 MB against 2.6 GB overall. Retry with
# backoff instead of dropping the result.
_PURGE_TIMEOUT_SECONDS = 300.0
# Own pacing so a big walk (hundreds of rooms) stays under matrix-media-repo's
# rate limit in the first place, instead of relying on 429 retries alone.
_MIN_REQUEST_INTERVAL_SECONDS = 0.2
_USAGE_BATCH_SIZE = 100
_RATE_LIMIT_MAX_ATTEMPTS = 6
_RATE_LIMIT_BASE_DELAY_SECONDS = 1.0


class PurgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class PurgeResult:
    room_id: str
    num_removed: int


def _request_with_retry(method: str, url: str, **kwargs):
    """requests.get/post, retrying HTTP 429 with exponential backoff
    (honouring Retry-After when present). Returns the last response."""
    send = getattr(requests, method)
    delay = _RATE_LIMIT_BASE_DELAY_SECONDS
    response = send(url, **kwargs)
    for _ in range(_RATE_LIMIT_MAX_ATTEMPTS - 1):
        if response.status_code != 429:
            break
        retry_after = None
        headers = getattr(response, "headers", None)
        if isinstance(headers, dict) and headers.get("Retry-After"):
            try:
                retry_after = float(headers["Retry-After"])
            except ValueError:
                retry_after = None
        time.sleep(retry_after if retry_after is not None else delay)
        delay = min(delay * 2, 30.0)
        response = send(url, **kwargs)
    return response


class MediaRepoPurgeClient:
    def __init__(
        self,
        *,
        base_url: str,
        admin_access_token: str,
        timeout_seconds: float = 30.0,
        min_request_interval_seconds: float = _MIN_REQUEST_INTERVAL_SECONDS,
    ):
        self._min_interval = min_request_interval_seconds
        self._last_request_at = 0.0
        self._base_url = base_url.rstrip("/")
        self._access_token = admin_access_token
        self._timeout_seconds = timeout_seconds

    def _call(self, method: str, url: str, **kwargs):
        wait = self._min_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            return _request_with_retry(method, url, **kwargs)
        finally:
            self._last_request_at = time.monotonic()

    def purge_room(self, *, room_id: str, before_ts_ms: int) -> PurgeResult:
        url = f"{self._base_url}/_matrix/media/unstable/admin/purge/room/{room_id}"
        try:
            response = self._call(
                "post",
                url,
                params={"before_ts": before_ts_ms},
                headers={"Authorization": f"Bearer {self._access_token}"},
                # A room with hundreds of files can take far longer than the
                # default 30s to purge from S3; a timeout here must not be
                # mistaken for failure of a purge MMR is still performing.
                timeout=max(self._timeout_seconds, _PURGE_TIMEOUT_SECONDS),
            )
        except requests.RequestException as exc:
            # Network errors/timeouts must surface as PurgeError so the
            # scheduler logs and retries next tick instead of crashing.
            raise PurgeError(f"matrix-media-repo purge request failed for room {room_id!r}: {exc}") from exc
        if response.status_code != 200:
            raise PurgeError(
                f"matrix-media-repo purge failed for room {room_id!r}: "
                f"HTTP {response.status_code} {response.text!r}"
            )
        body = response.json()
        # matrix-media-repo's own response shape: {"num_removed": <int>}.
        # Tolerate a missing key rather than crashing the scheduler loop --
        # a malformed-but-200 response should be logged and treated as
        # "0 known", not fatal for every other configured room this tick.
        num_removed = int(body.get("num_removed", 0))
        return PurgeResult(room_id=room_id, num_removed=num_removed)

    def get_overall_usage_bytes(self, *, server_name: str) -> int:
        """`GET /_matrix/media/unstable/admin/usage/<server_name>` (docs/
        admin.md's "Per-server usage") -- total bytes across the whole
        server (media + thumbnails). Returns 0 on failure rather than
        raising -- used for the top-N-by-media-size report's own overall
        total line; a wrong-but-present report is still useful even if
        this one call fails."""
        url = f"{self._base_url}/_matrix/media/unstable/admin/usage/{server_name}"
        response = self._call(
            "get", url, headers={"Authorization": f"Bearer {self._access_token}"}, timeout=self._timeout_seconds
        )
        if response.status_code != 200:
            return 0
        return int(response.json().get("raw_bytes", {}).get("total", 0))

    def get_usage_for_mxcs(self, mxcs: list[str]) -> dict[str, dict]:
        """Best-effort variant: failures are logged and skipped."""
        return self.get_usage_for_mxcs_checked(mxcs)[0]

    def get_usage_for_mxcs_checked(self, mxcs: list[str]) -> tuple[dict[str, dict], bool]:
        """`GET /_matrix/media/unstable/admin/usage/<server_name>/uploads`
        with one or more repeated `?mxc=` query params (docs/admin.md's
        "Per-upload usage (batch of uploads / single upload)") -- per-MXC
        `size_bytes`/`created_ts`/etc., used to compute a room's total
        media size and oldest-file date from the MXC list
        `SynapseAdminClient.get_room_media_mxcs()` returns.

        Found live 2026-08-15: the `<server_name>` path segment is NOT a
        placeholder -- matrix-media-repo rejects the request outright
        ("MXC URIs must match the requested server") unless it matches
        every requested MXC's own domain. A room can reference media from
        more than one domain (local media plus anything federated/remote),
        so this groups `mxcs` by their own domain and issues one request
        per distinct domain, merging the results -- not a single call with
        a fixed/placeholder server name, which silently returned nothing
        for every room until this was found.

        Returns an empty dict for an empty `mxcs` list (no request made at
        all -- nothing to ask about); a malformed MXC or a failed request
        for one domain is skipped rather than failing the whole batch."""
        if not mxcs:
            return {}, True

        mxcs_by_domain: dict[str, list[str]] = {}
        for mxc in mxcs:
            # mxc://<domain>/<media_id> -- domain is the one part of this
            # URI shape that never itself contains a "/".
            without_scheme = mxc[len("mxc://"):] if mxc.startswith("mxc://") else mxc
            domain = without_scheme.split("/", 1)[0]
            if not domain:
                continue
            mxcs_by_domain.setdefault(domain, []).append(mxc)

        result: dict[str, dict] = {}
        complete = True
        for domain, domain_mxcs in mxcs_by_domain.items():
            url = f"{self._base_url}/_matrix/media/unstable/admin/usage/{domain}/uploads"
            for start in range(0, len(domain_mxcs), _USAGE_BATCH_SIZE):
                batch = domain_mxcs[start : start + _USAGE_BATCH_SIZE]
                try:
                    response = self._call(
                        "get",
                        url,
                        params=[("mxc", mxc) for mxc in batch],
                        headers={"Authorization": f"Bearer {self._access_token}"},
                        timeout=self._timeout_seconds,
                    )
                except requests.RequestException as exc:
                    logger.warning("usage lookup for %d mxc(s) on %s failed: %s", len(batch), domain, exc)
                    complete = False
                    continue
                if response.status_code != 200:
                    logger.warning(
                        "usage lookup for %d mxc(s) on %s failed: HTTP %s -- sizes missing from result",
                        len(batch),
                        domain,
                        response.status_code,
                    )
                    complete = False
                    continue
                body_json = response.json()
                if isinstance(body_json, dict):
                    result.update(body_json)
        return result, complete

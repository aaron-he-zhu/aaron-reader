import gzip
import hashlib
import io
import math
import random
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from typing import Dict, Optional
from urllib.parse import urlsplit

from .models import FetchResult


USER_AGENT = "AaronReader/1.1 (deterministic local feed reader)"


class FetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        retry_after_seconds: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_seconds = retry_after_seconds


class HttpClient:
    def __init__(
        self,
        timeout_seconds: int = 25,
        max_response_bytes: int = 8_000_000,
        min_host_interval_seconds: float = 0.5,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.min_host_interval_seconds = min_host_interval_seconds
        self._last_request_by_host: Dict[str, float] = {}

    def fetch(
        self,
        url: str,
        etag: str = "",
        last_modified: str = "",
        attempts: int = 2,
        accept: str = "",
    ) -> FetchResult:
        headers: Dict[str, str] = {
            "User-Agent": USER_AGENT,
            "Accept": accept or "application/rss+xml, application/atom+xml, application/xml, text/xml, text/markdown, text/html;q=0.9, */*;q=0.1",
            "Accept-Encoding": "gzip",
            "Cache-Control": "no-cache",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_error: Optional[BaseException] = None
        for attempt in range(max(1, attempts)):
            self._respect_host_interval(url)
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    status = int(response.getcode())
                    body = self._read_limited(response)
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        try:
                            with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                                body = compressed.read(self.max_response_bytes + 1)
                        except OSError as exc:
                            raise FetchError("invalid gzip response from %s: %s" % (url, exc), status)
                    if len(body) > self.max_response_bytes:
                        raise FetchError(
                            "response from %s exceeds %d bytes" % (url, self.max_response_bytes), status
                        )
                    return FetchResult(
                        url=response.geturl(),
                        status=status,
                        body=body,
                        content_type=response.headers.get("Content-Type", ""),
                        etag=response.headers.get("ETag", ""),
                        last_modified=response.headers.get("Last-Modified", ""),
                        body_hash=hashlib.sha256(body).hexdigest(),
                    )
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return FetchResult(
                        url=url,
                        status=304,
                        etag=exc.headers.get("ETag", etag),
                        last_modified=exc.headers.get("Last-Modified", last_modified),
                        not_modified=True,
                    )
                last_error = exc
                retry_after = self._retry_delay(attempt, exc.headers.get("Retry-After"))
                if (
                    exc.code not in (429, 500, 502, 503, 504)
                    or attempt + 1 >= attempts
                    or retry_after > 10.0
                ):
                    raise FetchError(
                        "HTTP %s fetching %s" % (exc.code, url),
                        exc.code,
                        retry_after_seconds=retry_after,
                    )
                self._wait_before_retry(attempt, exc.headers.get("Retry-After"))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise FetchError("network error fetching %s: %s" % (url, exc))
                self._wait_before_retry(attempt, None)
        raise FetchError("failed fetching %s: %s" % (url, last_error))

    def _respect_host_interval(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        if not host or self.min_host_interval_seconds <= 0:
            return
        previous = self._last_request_by_host.get(host)
        if previous is not None:
            remaining = self.min_host_interval_seconds - (time.monotonic() - previous)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_by_host[host] = time.monotonic()

    def _read_limited(self, response: object) -> bytes:
        body = response.read(self.max_response_bytes + 1)  # type: ignore[attr-defined]
        if len(body) > self.max_response_bytes:
            raise FetchError("response exceeds %d bytes" % self.max_response_bytes)
        return body

    @staticmethod
    def _retry_delay(attempt: int, retry_after: Optional[str]) -> float:
        delay: Optional[float] = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    delay = max(0.0, retry_at.timestamp() - time.time())
                except (TypeError, ValueError, OverflowError):
                    pass
        if delay is None or not math.isfinite(delay):
            delay = (2 ** attempt) + random.uniform(0.0, 0.25)
        return max(0.0, delay)

    @classmethod
    def _wait_before_retry(cls, attempt: int, retry_after: Optional[str]) -> None:
        time.sleep(min(cls._retry_delay(attempt, retry_after), 10.0))

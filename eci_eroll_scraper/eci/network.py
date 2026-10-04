"""Record portal traffic without storing CAPTCHA images, cookies, or tokens."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.async_api import Page, Response

from eci import selectors
from eci.models import PartRecord

logger = logging.getLogger("eci")


@dataclass
class CapturedFile:
    source_url: str
    suggested_name: str
    content: bytes
    content_type: str
    origin: str = "network"


@dataclass
class GenerateResult:
    http_status: int
    status_code: int | None
    message: str
    ref_id: str | None
    payload: list
    raw_ok: bool

    @property
    def accepted(self) -> bool:
        return self.http_status < 400 and self.raw_ok and len(self.payload) > 0


@dataclass
class ObservedCall:
    method: str
    path: str
    status: int
    note: str


class NetworkRecorder:
    """Listen to the page the operator is using. Do not call the APIs directly."""

    def __init__(self) -> None:
        self.events: list[ObservedCall] = []
        self.languages: list[tuple[str, str]] = []
        self.parts: list[PartRecord] = []
        self.capturing = False
        self._page: Page | None = None
        self._generate: asyncio.Queue[GenerateResult] = asyncio.Queue()
        self._files: list[CapturedFile] = []
        self._file_event = asyncio.Event()
        self._last_generate: GenerateResult | None = None
        self._pending_file_urls: set[str] = set()

    def attach(self, page: Page) -> None:
        self._page = page
        page.on("response", self._on_response)
        page.on("download", self._on_download)

    def start_capture(self) -> None:
        self.capturing = True
        self._files.clear()
        self._file_event = asyncio.Event()
        self._last_generate = None
        self._pending_file_urls.clear()
        while not self._generate.empty():
            self._generate.get_nowait()

    def stop_capture(self) -> None:
        self.capturing = False

    def discard_partial_capture(self) -> None:
        self._files.clear()
        self._file_event = asyncio.Event()
        self._pending_file_urls.clear()

    async def wait_for_generate(self, timeout: float | None = None) -> GenerateResult:
        """Wait for generate-published-pdfs. Raises asyncio.TimeoutError if nothing arrives."""
        if timeout is None:
            return await self._generate.get()
        return await asyncio.wait_for(self._generate.get(), timeout=timeout)

    async def wait_for_files(self, expected: int, timeout: float) -> list[CapturedFile]:
        """Wait until every generated file arrives, or traffic stays quiet."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        quiet_after = 8.0
        last_count = -1
        quiet_since: float | None = None
        while loop.time() < deadline:
            current = len(self._unique_files())
            if expected > 0 and current >= expected:
                return self._unique_files()
            if current > 0 and current == last_count:
                if quiet_since is None:
                    quiet_since = loop.time()
                elif loop.time() - quiet_since >= quiet_after:
                    break
            else:
                quiet_since = None
                last_count = current
            self._file_event.clear()
            remaining = deadline - loop.time()
            try:
                await asyncio.wait_for(self._file_event.wait(), timeout=min(1.0, max(remaining, 0.1)))
            except asyncio.TimeoutError:
                continue
        files = self._unique_files()
        if expected > 0 and len(files) < expected:
            await self._backfill_missing_files(expected)
            files = self._unique_files()
        return files

    def _unique_files(self) -> list[CapturedFile]:
        seen: set[str] = set()
        unique: list[CapturedFile] = []
        for item in self._files:
            if not item.content:
                continue
            digest = hashlib.sha256(item.content).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            unique.append(item)
        return unique

    async def _on_download(self, download) -> None:
        if not self.capturing:
            return
        try:
            temporary = await download.path()
            if temporary is None:
                failure = await download.failure()
                logger.warning("Browser download failed: %s", failure)
                return
            content = temporary.read_bytes()
            self._remember_file(
                CapturedFile(
                    source_url=download.url,
                    suggested_name=download.suggested_filename,
                    content=content,
                    content_type="application/octet-stream",
                    origin="browser",
                )
            )
        except Exception:
            logger.exception("Could not read a browser download")

    async def _on_response(self, response: Response) -> None:
        try:
            await self._handle_response(response)
        except Exception as exc:
            if _is_body_gone_error(exc):
                logger.warning(
                    "Network body unavailable for %s; will refetch if needed",
                    response.url.split("?")[0],
                )
            else:
                logger.exception("Network listener failed for %s", response.url.split("?")[0])

    async def _handle_response(self, response: Response) -> None:
        url = response.url
        path = urlparse(url).path
        interesting = _interesting(path)
        if not interesting:
            return

        note = ""
        if selectors.API_CAPTCHA in path or selectors.API_VOICE_CAPTCHA in path:
            note = "captcha payload omitted"
            self._add_event(response.request.method, _public_path(path), response.status, note)
            return

        if selectors.API_ROLL_TYPES in path:
            note = await self._summarize_roll_types(response)
        elif selectors.API_LANGUAGES in path and response.request.method == "POST":
            note = await self._summarize_languages(response)
        elif selectors.API_PARTS in path and response.request.method == "POST":
            note = await self._summarize_parts(response)
        elif selectors.API_GENERATE in path and response.request.method == "POST":
            note = await self._summarize_generate(response)
        elif selectors.API_PUBLISHED_FILE in path or path.startswith(selectors.EROLL_FILE_PREFIX):
            note = await self._capture_file_response(response)

        self._add_event(response.request.method, _public_path(path), response.status, note)

    def _add_event(self, method: str, path: str, status: int, note: str) -> None:
        self.events.append(ObservedCall(method, path, status, note))
        logger.info("Observed %s %s -> %s %s", method, path, status, note)

    async def _summarize_roll_types(self, response: Response) -> str:
        data = await _json_or_none(response)
        if not isinstance(data, dict):
            return "non-json response"
        payload = data.get("payload") or []
        names = [item.get("displayName", "") for item in payload if isinstance(item, dict)]
        return f"{len(names)} roll types: {', '.join(names)}"

    async def _summarize_languages(self, response: Response) -> str:
        data = await _json_or_none(response)
        if not isinstance(data, dict):
            return "non-json response"
        payload = data.get("payload") or {}
        if isinstance(payload, dict):
            self.languages = [(str(code), str(name)) for code, name in payload.items()]
            return "languages: " + ", ".join(f"{code}={name}" for code, name in self.languages)
        return "unexpected language payload"

    async def _summarize_parts(self, response: Response) -> str:
        data = await _json_or_none(response)
        if not isinstance(data, dict):
            return "non-json response"
        payload = data.get("payload") or []
        parts: list[PartRecord] = []
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict) or item.get("partNumber") is None:
                    continue
                parts.append(
                    PartRecord(
                        part_number=int(item["partNumber"]),
                        part_name=str(item.get("partName") or ""),
                        district_code=str(item.get("districtCd") or ""),
                    )
                )
        self.parts = parts
        return f"{len(parts)} parts"

    async def _summarize_generate(self, response: Response) -> str:
        data = await _json_or_none(response)
        result = _generate_result(response.status, data)
        if self.capturing:
            self._last_generate = result
            for url in _urls_from_payload(result.ref_id, result.payload):
                self._pending_file_urls.add(url)
            await self._generate.put(result)
        message = result.message or "no message"
        return f"generate statusCode={result.status_code} items={len(result.payload)} message={message}"

    async def _capture_file_response(self, response: Response) -> str:
        if not self.capturing or response.status >= 400:
            return f"file response status {response.status}"
        content_type = response.headers.get("content-type", "")
        body = await self._read_response_body(response)
        if body is None:
            return "file body unavailable (queued for refetch)"
        return self._store_file_bytes(response.url, body, content_type)

    async def _read_response_body(self, response: Response) -> bytes | None:
        """Read Playwright body; if Chromium already discarded it, refetch with cookies."""
        try:
            body = await response.body()
            if body:
                return body
        except Exception as exc:
            if _is_body_gone_error(exc):
                logger.warning(
                    "Playwright dropped body for %s; refetching",
                    response.url.split("?")[0],
                )
            else:
                logger.warning(
                    "Body read failed for %s: %s; refetching",
                    response.url.split("?")[0],
                    exc,
                )
        return await self._refetch_url(response.url)

    async def _refetch_url(self, url: str) -> bytes | None:
        page = self._page
        if page is None:
            return None
        try:
            resp = await page.request.get(url, timeout=180_000)
            if resp.status >= 400:
                logger.warning("Refetch HTTP %s for %s", resp.status, url.split("?")[0])
                return None
            data = await resp.body()
            return data or None
        except Exception as exc:
            logger.warning("Refetch failed for %s: %s", url.split("?")[0], exc)
            return None

    async def _backfill_missing_files(self, expected: int) -> None:
        """Fetch any CDN/API file URLs from the generate payload that were not captured."""
        have = {item.source_url for item in self._unique_files()}
        missing = [url for url in sorted(self._pending_file_urls) if url.split("?")[0] not in have]
        if not missing and self._last_generate is not None:
            missing = [
                url
                for url in _urls_from_payload(self._last_generate.ref_id, self._last_generate.payload)
                if url.split("?")[0] not in have
            ]
        if not missing:
            return
        need = max(expected - len(self._unique_files()), 0)
        if need <= 0:
            return
        logger.info("Backfilling %s missing PDF(s) via direct fetch", min(need, len(missing)))
        for url in missing:
            if len(self._unique_files()) >= expected:
                break
            body = await self._refetch_url(url)
            if not body:
                continue
            self._store_file_bytes(url, body, "application/pdf")

    def _store_file_bytes(self, url: str, body: bytes, content_type: str) -> str:
        path = urlparse(url).path
        if "json" in (content_type or "") or (not body.startswith(b"%PDF") and body[:1] == b"{"):
            captured = _file_from_json(url, body)
            if captured is None:
                return "json file response had no document"
            captured.origin = "base64"
            self._remember_file(captured)
            return f"captured base64 document {captured.suggested_name}"
        if body.startswith(b"%PDF") or "pdf" in (content_type or "") or path.startswith(selectors.EROLL_FILE_PREFIX):
            name = path.rstrip("/").split("/")[-1] or "download.bin"
            origin = "cdn" if path.startswith(selectors.EROLL_FILE_PREFIX) else "network"
            self._remember_file(
                CapturedFile(
                    source_url=url.split("?")[0],
                    suggested_name=name,
                    content=body,
                    content_type=content_type or "application/pdf",
                    origin=origin,
                )
            )
            return f"captured {name} ({len(body)} bytes)"
        return "response was not a PDF or ZIP"

    def _remember_file(self, captured: CapturedFile) -> None:
        self._files.append(captured)
        self._pending_file_urls.discard(captured.source_url)
        self._file_event.set()


def _interesting(path: str) -> bool:
    markers = (
        selectors.API_CAPTCHA,
        selectors.API_VOICE_CAPTCHA,
        selectors.API_ROLL_TYPES,
        selectors.API_LANGUAGES,
        selectors.API_PARTS,
        selectors.API_GENERATE,
        selectors.API_PUBLISHED_FILE,
        selectors.EROLL_FILE_PREFIX,
    )
    return any(marker in path for marker in markers)


def _public_path(path: str) -> str:
    if selectors.API_VOICE_CAPTCHA in path:
        return selectors.API_VOICE_CAPTCHA + "{captchaId}"
    return path


def _is_body_gone_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "no data found for resource" in text
        or "navigated away" in text
        or "response.body" in text and "protocol error" in text
    )


def _urls_from_payload(ref_id: str | None, payload: list) -> list[str]:
    """Turn generate-published-pdfs payload items into absolute download URLs."""
    urls: list[str] = []
    cdn_base = "https://voters.eci.gov.in/eroll/"
    for item in payload:
        candidates: list[str] = []
        if isinstance(item, str):
            candidates.append(item)
        elif isinstance(item, dict):
            for key in ("url", "fileUrl", "filePath", "path", "cdnPath", "fileName", "fileId"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
                    break
        for raw in candidates:
            if raw.startswith("http://") or raw.startswith("https://"):
                urls.append(raw)
            elif raw.startswith(selectors.EROLL_FILE_PREFIX):
                urls.append("https://voters.eci.gov.in" + raw)
            elif raw.startswith("/"):
                urls.append("https://voters.eci.gov.in" + raw)
            elif ref_id == "CDN" or "/" in raw or raw.lower().endswith(".pdf"):
                urls.append(cdn_base + raw.lstrip("/"))
    # Preserve order, drop duplicates
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        key = url.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        unique.append(url)
    return unique


async def _json_or_none(response: Response):
    try:
        return await response.json()
    except Exception:
        return None


def _generate_result(http_status: int, data) -> GenerateResult:
    if not isinstance(data, dict):
        return GenerateResult(http_status, None, "Response was not JSON", None, [], False)
    payload = data.get("payload")
    items = payload if isinstance(payload, list) else []
    status_code = data.get("statusCode")
    try:
        status_code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code = None
    message = str(data.get("message") or "")
    accepted = http_status < 400 and status_code == 200 and len(items) > 0
    return GenerateResult(
        http_status=http_status,
        status_code=status_code,
        message=message,
        ref_id=data.get("refId") if isinstance(data.get("refId"), str) else None,
        payload=items,
        raw_ok=accepted,
    )


def _file_from_json(url: str, body: bytes) -> CapturedFile | None:
    import base64
    import json

    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    payload = data.get("payload")
    if not isinstance(payload, str) or len(payload) < 80:
        return None
    try:
        content = base64.b64decode(payload, validate=False)
    except Exception:
        return None
    if not content:
        return None
    name = data.get("refId") if isinstance(data.get("refId"), str) else ""
    if not name:
        name = urlparse(url).path.rstrip("/").split("/")[-1] or "download.bin"
    return CapturedFile(
        source_url=url.split("?")[0],
        suggested_name=name,
        content=content,
        content_type="application/pdf" if content.startswith(b"%PDF") else "application/octet-stream",
    )


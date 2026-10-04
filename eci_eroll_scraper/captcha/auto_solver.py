"""Automatic CAPTCHA solver for the ECI electoral-roll portal.

Modes via CAPTCHA_SOLVER env (default: hybrid):
  - ocr      — ddddocr on captcha image
  - whisper  — faster-whisper on voice captcha
  - hybrid   — whisper first; if answer looks weak, fall back to OCR on same image
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re

import httpx
from playwright.async_api import Page, Response

from eci import selectors
from eci.network import CapturedFile, NetworkRecorder

import config

logger = logging.getLogger("eci")

MAX_ATTEMPTS = 15

_AES_KEY_B64 = "e855n97lc4tcPkj7WWsi38yNWpalLBLZzQdkqHWYbZ0="
_API_BASE = "https://gateway-voters.eci.gov.in"
_API_HEADERS = {
    "appName": "VSP",
    "applicationName": "VSP",
    "PLATFORM-TYPE": "ECIWEB",
    "channelidobo": "VSP",
}

_ocr = None


def _solver_mode() -> str:
    return os.environ.get("CAPTCHA_SOLVER", "hybrid").strip().lower()


def _get_ocr():
    global _ocr
    if _ocr is None:
        import ddddocr

        logger.info("Loading ddddocr captcha solver...")
        _ocr = ddddocr.DdddOcr(show_ad=False)
        logger.info("ddddocr loaded")
    return _ocr


def _decrypt(encrypted_b64: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = base64.b64decode(_AES_KEY_B64)
    data = base64.b64decode(encrypted_b64)
    return json.loads(AESGCM(key).decrypt(data[:12], data[12:], None))


def _ocr_image(jpeg_b64: str) -> str:
    raw = base64.b64decode(jpeg_b64)
    text = _get_ocr().classification(raw)
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", str(text or ""))
    logger.debug("OCR: '%s' → '%s'", text, cleaned)
    return cleaned


async def _fetch_voice_wav(captcha_id: str) -> bytes | None:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{_API_BASE}/api/v1/captcha-service/generateVoiceCaptcha/{captcha_id}",
                headers=_API_HEADERS,
                timeout=15,
            )
            if response.status_code == 200 and len(response.content) > 1000:
                return response.content
    except Exception as exc:
        logger.debug("Voice fetch error: %s", exc)
    return None


def _valid_answer(answer: str | None) -> bool:
    return bool(answer) and 4 <= len(answer) <= 6


async def _dismiss_blocking_alerts(page: Page) -> None:
    """Portal toast overlays (.alert_global) steal clicks for minutes; remove them."""
    try:
        removed = await page.evaluate(
            """() => {
                let n = 0;
                for (const sel of [
                    '.alert_global',
                    '.alert.customError',
                    '.library-alert',
                    '[role="alert"]',
                    '.Toastify',
                ]) {
                    document.querySelectorAll(sel).forEach((el) => {
                        el.remove();
                        n += 1;
                    });
                }
                return n;
            }"""
        )
        if removed:
            logger.info("Dismissed %s blocking alert overlay(s)", removed)
    except Exception as exc:
        logger.debug("Alert dismiss skipped: %s", exc)


async def _click_through_overlays(page: Page, locator, *, timeout_ms: int = 8_000) -> None:
    """Click even when toast overlays intercept pointer events."""
    await _dismiss_blocking_alerts(page)
    try:
        await locator.click(force=True, timeout=timeout_ms)
        return
    except Exception:
        pass
    await _dismiss_blocking_alerts(page)
    handle = await locator.element_handle(timeout=timeout_ms)
    if handle is None:
        raise RuntimeError("element not found for click")
    await handle.evaluate("el => el.click()")


async def _refresh_captcha_payload(page: Page) -> dict | None:
    refresh_btn = page.locator(selectors.CAPTCHA_REFRESH)
    if await refresh_btn.count() == 0:
        logger.warning("No captcha refresh button found")
        return None
    try:
        async with page.expect_response(
            lambda r: "/getCaptcha/" in r.url and r.status == 200,
            timeout=15_000,
        ) as resp_info:
            await _click_through_overlays(page, refresh_btn, timeout_ms=8_000)
        response: Response = await resp_info.value
        body = await response.json()
        if "data" not in body:
            return None
        return _decrypt(body["data"])
    except Exception as exc:
        logger.warning("Failed to refresh captcha: %s", exc)
        return None


def _answer_variants(whisper_ans: str, ocr_ans: str) -> list[tuple[str, str]]:
    """Ordered unique candidates to try on the SAME captcha before refreshing."""
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(answer: str, method: str) -> None:
        cleaned = re.sub(r"[^a-zA-Z0-9]", "", answer or "").lower()
        if len(cleaned) > 6:
            cleaned = cleaned[:6]
        if not _valid_answer(cleaned) or cleaned in seen:
            return
        seen.add(cleaned)
        ordered.append((cleaned, method))

    add(whisper_ans, "whisper")
    add(ocr_ans, "ocr")

    # Common voice confusions — single-position swaps from whisper only.
    swap_pairs = (("b", "v"), ("v", "b"), ("g", "q"), ("q", "g"), ("q", "u"), ("u", "q"), ("p", "b"))
    base = re.sub(r"[^a-zA-Z0-9]", "", whisper_ans or "").lower()
    if _valid_answer(base):
        for index, char in enumerate(base):
            for src, dst in swap_pairs:
                if char != src:
                    continue
                candidate = base[:index] + dst + base[index + 1 :]
                add(candidate, f"whisper_swap_{src}{dst}")

    return ordered


async def _solve_candidates(page: Page) -> list[tuple[str, str]]:
    mode = _solver_mode()
    payload = await _refresh_captcha_payload(page)
    if not payload:
        return []
    jpeg_b64 = payload.get("captcha")
    captcha_id = payload.get("id")
    if not jpeg_b64:
        return []

    ocr_ans = ""
    whisper_ans = ""
    whisper_raw = ""

    if mode in ("ocr", "hybrid"):
        ocr_ans = _ocr_image(jpeg_b64)
    if mode in ("whisper", "hybrid") and captcha_id:
        wav = await _fetch_voice_wav(str(captcha_id))
        if wav:
            from captcha.whisper_solver import transcribe_captcha_wav

            whisper_ans, whisper_raw = transcribe_captcha_wav(wav)
            logger.info("whisper raw=%r → %r (ocr=%r)", whisper_raw, whisper_ans, ocr_ans)
        else:
            logger.warning("Voice captcha empty for id=%s", captcha_id)

    if mode == "ocr":
        return _answer_variants("", ocr_ans)
    if mode == "whisper":
        return _answer_variants(whisper_ans, "")
    return _answer_variants(whisper_ans, ocr_ans)


async def _fill_and_submit(page: Page, answer: str) -> None:
    await _dismiss_blocking_alerts(page)
    inp = page.locator(selectors.CAPTCHA_INPUT)
    try:
        await _click_through_overlays(page, inp, timeout_ms=5_000)
    except Exception:
        pass
    await inp.fill("")
    await page.wait_for_timeout(100)
    await inp.fill(answer)
    await page.wait_for_timeout(150)
    btn = page.locator(selectors.DOWNLOAD_BUTTON)
    try:
        await btn.scroll_into_view_if_needed(timeout=5_000)
    except Exception:
        pass
    await page.wait_for_timeout(100)
    await _click_through_overlays(page, btn, timeout_ms=8_000)


async def wait_for_auto_captcha(
    page: Page,
    network: NetworkRecorder,
    *,
    state: str,
    year: str,
    roll_type: str,
    constituency: str,
    language: str,
    part_numbers: list[int],
) -> list[CapturedFile]:
    """Solve captcha via CAPTCHA_SOLVER mode (ocr / whisper / hybrid)."""
    parts_str = ", ".join(str(n) for n in part_numbers)
    mode = _solver_mode()
    logger.info(
        "Auto-solving captcha (%s) for %s / %s / %s / parts [%s]",
        mode,
        state,
        constituency,
        language,
        parts_str,
    )
    if mode in ("ocr", "hybrid"):
        _get_ocr()
    if mode in ("whisper", "hybrid"):
        from captcha.whisper_solver import _get_whisper_model

        _get_whisper_model()

    await _dismiss_blocking_alerts(page)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            await asyncio.sleep(1)
            await _dismiss_blocking_alerts(page)

        try:
            candidates = await _solve_candidates(page)
        except Exception as exc:
            logger.warning("Attempt %s: solve error: %s", attempt, exc)
            continue

        if not candidates:
            logger.warning("Attempt %s: no captcha candidates", attempt)
            continue

        accepted_any = False
        for answer, method in candidates:
            logger.info(
                "Attempt %s: trying %s='%s' (%s candidates)",
                attempt,
                method,
                answer,
                len(candidates),
            )
            network.start_capture()
            try:
                try:
                    await _fill_and_submit(page, answer)
                except Exception as exc:
                    logger.warning(
                        "Attempt %s: submit click failed (%s): %s",
                        attempt,
                        method,
                        exc,
                    )
                    continue
                try:
                    result = await network.wait_for_generate(config.GENERATE_TIMEOUT_S)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Attempt %s: no generate response within %ss (%s='%s')",
                        attempt,
                        int(config.GENERATE_TIMEOUT_S),
                        method,
                        answer,
                    )
                    continue

                if result.accepted:
                    files = await network.wait_for_files(
                        len(result.payload), config.DOWNLOAD_TIMEOUT_S
                    )
                    pdfs = [f for f in files if f.content.startswith(b"%PDF")]
                    if pdfs:
                        logger.info(
                            "Attempt %s: SUCCESS (%s) — %s PDFs",
                            attempt,
                            method,
                            len(pdfs),
                        )
                        return pdfs
                    logger.warning("Attempt %s: accepted but no PDFs (%s)", attempt, method)
                    accepted_any = True
                    break
                msg = result.message or ""
                if "not published" in msg.lower():
                    raise RuntimeError(f"Data not available: {msg}")
                logger.info("Attempt %s: %s rejected (%s)", attempt, method, msg)
                # Same captcha id is still valid — try next candidate without refresh.
            finally:
                network.stop_capture()
                network.discard_partial_capture()

        if accepted_any:
            continue

    raise RuntimeError(f"Captcha solver failed after {MAX_ATTEMPTS} attempts")

"""Human CAPTCHA checkpoint.

This module never OCRs the CAPTCHA, never sends it to a model or solver,
and never types or refreshes the CAPTCHA itself. It may print the image
src attribute and a local preview path so the operator can see what is
on the page.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

from playwright.async_api import Page

from eci import selectors
from eci.network import CapturedFile, NetworkRecorder

import config

logger = logging.getLogger("eci")

CAPTCHA_PREVIEW = config.LOG_DIR / "last_captcha.png"


async def wait_for_manual_captcha(
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
    await _focus(page)
    image_info = await _captcha_image_info(page)
    _print_required(
        state=state,
        year=year,
        roll_type=roll_type,
        constituency=constituency,
        language=language,
        part_numbers=part_numbers,
        image_info=image_info,
    )
    logger.info("CAPTCHA checkpoint reached")
    logger.info("Waiting for user")
    network.start_capture()
    try:
        while True:
            result = await network.wait_for_generate()
            logger.info("Download request detected")
            if not result.accepted:
                _print_failed(result.message)
                network.discard_partial_capture()
                image_info = await _captcha_image_info(page)
                if image_info.get("src_kind"):
                    print(f"CAPTCHA image src: {image_info['src_display']}", flush=True)
                    if image_info.get("preview_path"):
                        print(f"CAPTCHA preview file: {image_info['preview_path']}", flush=True)
                continue
            files = await network.wait_for_files(len(result.payload), config.DOWNLOAD_TIMEOUT_S)
            pdfs = [item for item in files if item.content.startswith(b"%PDF")]
            if not pdfs:
                _print_failed(result.message or "The portal accepted the request but no PDF arrived.")
                network.discard_partial_capture()
                continue
            return pdfs
    finally:
        network.stop_capture()


async def _captcha_image_info(page: Page) -> dict[str, str]:
    """Read only structural src info from the CAPTCHA <img>. Does not solve it."""
    data = await page.evaluate(
        """(selector) => {
            const img = document.querySelector(selector);
            if (!img || !img.src) {
                return { found: false, src: '', width: 0, height: 0 };
            }
            return {
                found: true,
                src: img.src,
                width: img.naturalWidth || 0,
                height: img.naturalHeight || 0,
                alt: img.alt || '',
            };
        }""",
        selectors.CAPTCHA_IMAGE,
    )
    if not data.get("found"):
        return {
            "src_kind": "missing",
            "src_display": "(CAPTCHA image not found on the page)",
            "preview_path": "",
            "api": "GET https://gateway-voters.eci.gov.in/api/v1/captcha-service/getCaptcha/EROLL",
        }

    src = str(data.get("src") or "")
    kind, display, preview = _describe_src(src)
    return {
        "src_kind": kind,
        "src_display": display,
        "preview_path": preview,
        "width": str(data.get("width") or ""),
        "height": str(data.get("height") or ""),
        "selector": selectors.CAPTCHA_IMAGE,
        "api": "GET https://gateway-voters.eci.gov.in/api/v1/captcha-service/getCaptcha/EROLL",
    }


def _describe_src(src: str) -> tuple[str, str, str]:
    if src.startswith("http://") or src.startswith("https://"):
        return "http", src, ""
    if src.startswith("blob:"):
        return "blob", src, ""
    if src.startswith("data:"):
        preview = _write_data_url_preview(src)
        # Full data URLs are huge. Print a short, still-valid HTML-style reference.
        match = re.match(r"(data:image/[^;]+;base64,)(.{0,48})", src)
        if match:
            display = f"{match.group(1)}{match.group(2)}...  (inline data URL, not a public http link)"
        else:
            display = "data:... (inline data URL, not a public http link)"
        return "data-url", display, preview
    return "other", src[:200], ""


def _write_data_url_preview(src: str) -> str:
    match = re.match(r"data:image/([^;]+);base64,(.+)", src, re.DOTALL)
    if not match:
        return ""
    try:
        content = base64.b64decode(match.group(2), validate=False)
    except Exception:
        return ""
    if not content:
        return ""
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    CAPTCHA_PREVIEW.write_bytes(content)
    return str(CAPTCHA_PREVIEW)


def _print_required(
    *,
    state: str,
    year: str,
    roll_type: str,
    constituency: str,
    language: str,
    part_numbers: list[int],
    image_info: dict[str, str],
) -> None:
    parts = ", ".join(str(number) for number in part_numbers)
    lines = [
        "",
        "==================================================",
        "CAPTCHA REQUIRED",
        "==================================================",
        "",
        f"State: {state}",
        f"Year: {year}",
        f"Roll Type: {roll_type}",
        f"Constituency: {constituency}",
        f"Language: {language}",
        "",
        "Parts:",
        parts,
        "",
        f"Count: {len(part_numbers)}",
        "",
        f"CAPTCHA selector: {image_info.get('selector', selectors.CAPTCHA_IMAGE)}",
        f"CAPTCHA image src kind: {image_info.get('src_kind', '')}",
        f"CAPTCHA image src: {image_info.get('src_display', '')}",
        f"CAPTCHA generated by: {image_info.get('api', '')}",
    ]
    if image_info.get("preview_path"):
        lines.append(f"CAPTCHA preview file: {image_info['preview_path']}")
    if image_info.get("width"):
        lines.append(f"CAPTCHA size: {image_info['width']}x{image_info['height']}")
    lines.extend(
        [
            "",
            "There is usually no separate https:// image URL.",
            "The page embeds the CAPTCHA as an inline data URL on img[alt=\"Captcha\"].",
            "",
            "Enter the CAPTCHA in the browser and click:",
            "Download Selected PDFs",
            "",
            "Waiting for download...",
            "",
        ]
    )
    print("\n".join(lines), flush=True)


def _print_failed(server_message: str) -> None:
    logger.warning("Download submission was not accepted: %s", server_message or "no server message")
    message = (server_message or "").lower()
    unpublished = "not published" in message or "supplement not published" in message
    invalid_captcha = "captcha" in message or "catpcha" in message

    if unpublished and not invalid_captcha:
        title = "DOWNLOAD REJECTED"
        body = [
            "The portal rejected this download for a reason other than CAPTCHA.",
            "This assembly / roll type may not be published.",
            "Try another roll type or constituency. The script is still waiting if you retry.",
        ]
    else:
        title = "CAPTCHA FAILED"
        body = [
            "The CAPTCHA appears to have been rejected.",
            "",
            "Please enter the CAPTCHA again and click Download Selected PDFs.",
            "",
            "Waiting...",
        ]

    lines = ["", "==================================================", title, "==================================================", ""]
    lines.extend(body)
    if server_message:
        lines.extend(["", f"Portal message: {server_message}", ""])
    else:
        lines.append("")
    print("\n".join(lines), flush=True)


async def _focus(page: Page) -> None:
    try:
        await page.bring_to_front()
    except Exception:
        logger.warning("Could not bring the browser window to the foreground")
    try:
        await page.evaluate("() => { window.focus(); }")
    except Exception:
        logger.warning("Could not focus the page")

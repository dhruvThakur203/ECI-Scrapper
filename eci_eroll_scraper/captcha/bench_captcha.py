"""Live captcha accuracy bench: Whisper vs OCR against the real ECI portal.

Runs N solve→submit cycles on a single AC batch and prints hit rates.
Does not change the user's long-running OCR scrape — separate process.

    .\\.venv\\Scripts\\python.exe captcha\\bench_captcha.py --trials 10 --solver whisper
    .\\.venv\\Scripts\\python.exe captcha\\bench_captcha.py --trials 10 --solver ocr
    .\\.venv\\Scripts\\python.exe captcha\\bench_captcha.py --trials 10 --solver both
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("CAPTCHA_WHISPER_DEVICE", "cpu")
os.environ.setdefault("CAPTCHA_WHISPER_COMPUTE", "int8")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from browser.browser_manager import BrowserManager
from captcha.auto_solver import _decrypt, _get_ocr, _ocr_image
from captcha.whisper_solver import transcribe_captcha_wav
from eci import selectors
from eci.models import Option
from eci.portal import Portal
from eci.checkpoint import Checkpoint
from eci.downloader import DownloadStore

import config

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("eci.bench")

OUT = config.LOG_DIR / "captcha_bench"
API = "https://gateway-voters.eci.gov.in"
HEADERS = {
    "appName": "VSP",
    "applicationName": "VSP",
    "PLATFORM-TYPE": "ECIWEB",
    "channelidobo": "VSP",
}


async def _fetch_voice(captcha_id: str) -> bytes | None:
    import httpx

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{API}/api/v1/captcha-service/generateVoiceCaptcha/{captcha_id}",
            headers=HEADERS,
            timeout=20,
        )
        if r.status_code == 200 and len(r.content) > 1000:
            return r.content
    return None


async def _refresh_payload(page) -> dict:
    refresh = page.locator(selectors.CAPTCHA_REFRESH)
    async with page.expect_response(
        lambda r: "/getCaptcha/" in r.url and r.status == 200,
        timeout=20_000,
    ) as info:
        await refresh.click()
    body = await (await info.value).json()
    return _decrypt(body["data"])


async def _prepare_form(portal: Portal) -> list[int]:
    await portal._open()
    states = await portal._states()
    target = next(s for s in states if "delhi" in s.state_name.lower())
    await portal._select_state(target)
    await portal._ensure_year()
    rolls = await portal._roll_types()
    await portal._select_roll_type(rolls[0])
    controls = await portal._dependent_controls()
    ac = Option(**controls["constituencies"][0])
    languages, parts = await portal._select_constituency(ac)
    if not parts:
        raise RuntimeError("No parts for bench AC")
    code, name = languages[0]
    await portal._select_language(code, name)
    wanted = [p.part_number for p in parts[:10]]
    await portal._goto_page(0)
    selected = await portal._select_current_page(wanted)
    if set(selected) != set(wanted):
        logger.warning("Could not select full first page; selected=%s", selected)
    logger.info("Bench ready on %s / %s parts %s", target.state_name, ac.text, selected or wanted)
    return wanted


async def _submit(page, answer: str, network) -> tuple[bool, str]:
    from captcha.auto_solver import _fill_and_submit

    network.start_capture()
    try:
        await _fill_and_submit(page, answer)
        result = await network.wait_for_generate()
        ok = bool(result.accepted)
        msg = result.message or ""
        if ok:
            # drain files quickly so next captcha is fresh; ignore content
            try:
                await network.wait_for_files(len(result.payload), 60)
            except Exception:
                pass
        return ok, msg
    finally:
        network.stop_capture()
        network.discard_partial_capture()


async def run_trials(solver: str, trials: int) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    manager = BrowserManager()
    session = await manager.start(visible=True)
    portal = Portal(session, Checkpoint(), DownloadStore())
    results = []
    try:
        wanted = await _prepare_form(portal)
        page = portal.page
        network = portal.network

        if solver in ("ocr", "both", "hybrid"):
            _get_ocr()
        if solver in ("whisper", "both", "hybrid"):
            from captcha.whisper_solver import _get_whisper_model

            _get_whisper_model()

        for i in range(1, trials + 1):
            # Ensure parts stay selected between trials
            checked = await page.locator(
                f"{selectors.PART_ROW} {selectors.ROW_CHECKBOX}:checked"
            ).count()
            if checked == 0:
                await portal._goto_page(0)
                await portal._select_current_page(wanted)

            dec = await _refresh_payload(page)
            cid = dec.get("id")
            jpeg = dec.get("captcha")
            wav = await _fetch_voice(cid) if cid else None
            if jpeg:
                (OUT / f"{i:02d}.png").write_bytes(base64.b64decode(jpeg))
            if wav:
                (OUT / f"{i:02d}.wav").write_bytes(wav)

            ocr_ans = _ocr_image(jpeg) if jpeg and solver in ("ocr", "both", "hybrid") else ""
            whisper_ans, whisper_raw = ("", "")
            if wav and solver in ("whisper", "both", "hybrid"):
                whisper_ans, whisper_raw = transcribe_captcha_wav(wav)

            if solver == "ocr":
                candidates = [(ocr_ans, "ocr")] if ocr_ans else []
            elif solver == "whisper":
                candidates = [(whisper_ans, "whisper")] if whisper_ans else []
            else:
                from captcha.auto_solver import _answer_variants

                candidates = _answer_variants(whisper_ans, ocr_ans)

            if not candidates:
                row = {
                    "trial": i,
                    "method": "none",
                    "answer": "",
                    "ocr": ocr_ans,
                    "whisper": whisper_ans,
                    "whisper_raw": whisper_raw,
                    "ok": False,
                    "message": "no_candidates",
                }
                results.append(row)
                logger.info("Trial %s: SKIP no candidates ocr=%r whisper=%r", i, ocr_ans, whisper_ans)
                continue

            ok = False
            msg = ""
            used_answer = ""
            used_method = ""
            for answer, method in candidates:
                if not answer or not (4 <= len(answer) <= 6):
                    continue
                # keep parts selected
                checked = await page.locator(
                    f"{selectors.PART_ROW} {selectors.ROW_CHECKBOX}:checked"
                ).count()
                if checked == 0:
                    await portal._goto_page(0)
                    await portal._select_current_page(wanted)
                ok, msg = await _submit(page, answer, network)
                used_answer, used_method = answer, method
                logger.info(
                    "Trial %s candidate %s=%r → %s (%s)",
                    i,
                    method,
                    answer,
                    "OK" if ok else "FAIL",
                    msg,
                )
                if ok:
                    break

            row = {
                "trial": i,
                "method": used_method,
                "answer": used_answer,
                "ocr": ocr_ans,
                "whisper": whisper_ans,
                "whisper_raw": whisper_raw,
                "ok": ok,
                "message": msg or "no_valid_candidate",
            }
            results.append(row)
            logger.info(
                "Trial %s: %s answer=%r ocr=%r whisper=%r raw=%r → %s (%s)",
                i,
                "OK" if ok else "FAIL",
                used_answer,
                ocr_ans,
                whisper_ans,
                whisper_raw,
                ok,
                msg,
            )
            await asyncio.sleep(1.5)
    finally:
        await manager.stop()

    submitted = [r for r in results if r.get("message") != "bad_length"]
    hits = sum(1 for r in submitted if r["ok"])
    total = len(submitted) or 1
    summary = {
        "solver": solver,
        "trials": trials,
        "submitted": len(submitted),
        "hits": hits,
        "rate": hits / total,
        "results": results,
    }
    (OUT / f"summary_{solver}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        f"\n=== {solver.upper()} hit rate: {hits}/{len(submitted)} = {100*hits/total:.0f}% "
        f"(target >= 80%) ===\n"
        f"Details: {OUT / f'summary_{solver}.json'}\n"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--solver", choices=["whisper", "ocr", "both", "hybrid"], default="hybrid")
    args = parser.parse_args()
    asyncio.run(run_trials(args.solver, args.trials))


if __name__ == "__main__":
    main()

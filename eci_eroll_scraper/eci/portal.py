"""Drive the electoral-roll form. CAPTCHAs are solved automatically via OCR."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from browser.browser_manager import BrowserSession
from captcha.auto_solver import wait_for_auto_captcha
from eci.batches import plan_batches
from eci.checkpoint import Checkpoint
from eci.downloader import DownloadStore
from eci.models import Combination, Option, PartRecord, StateOption
from eci import selectors

import config

logger = logging.getLogger("eci")


class PortalError(RuntimeError):
    pass


class _TestFinished(Exception):
    """The single test batch reached the end of its download attempt."""


class Portal:
    def __init__(self, session: BrowserSession, checkpoint: Checkpoint, store: DownloadStore) -> None:
        self.session = session
        self.page = session.page
        self.network = session.network
        self.checkpoint = checkpoint
        self.store = store
        self.district_names: dict[str, str] = {}

    async def discover(self, state_name: str | None, state_code: str | None) -> dict[str, Any]:
        await self._open()
        states = await self._states()
        target = _pick_state(states, state_name, state_code, default_first=True)
        report: dict[str, Any] = {
            "url": config.PORTAL_URL,
            "states": [state.__dict__ for state in states],
            "selected_state": target.__dict__,
        }
        print(f"\nStates discovered: {len(states)}")
        for state in states:
            print(f"  {state.state_code}  {state.state_name}")

        await self._select_state(target)
        await self._ensure_year()
        roll_types = await self._roll_types()
        report["year"] = await self.page.locator(selectors.YEAR_SELECT).input_value()
        report["roll_types"] = [option.__dict__ for option in roll_types]
        print(f"\nYear of revision: {report['year']}")
        print(f"Roll types for {target.state_name}: {len(roll_types)}")
        for option in roll_types:
            print(f"  {option.value}  {option.text}")

        roll = _selected_or_first(roll_types, await self.page.locator(selectors.ROLL_TYPE_SELECT).input_value())
        await self._select_roll_type(roll)
        controls = await self._dependent_controls()
        report["dependent_controls"] = controls
        print("\nDependent controls after roll type:")
        print(f"  District visible: {controls['district_visible']}")
        if controls["districts"]:
            print(f"  Districts: {len(controls['districts'])}")
            for option in controls["districts"]:
                print(f"    {option['value']}  {option['text']}")
        print(f"  Assembly constituencies: {len(controls['constituencies'])}")

        constituency = Option(**controls["constituencies"][0]) if controls["constituencies"] else None
        if constituency is None:
            raise PortalError("No assembly constituency was available for the selected roll type")
        languages, parts = await self._select_constituency(constituency)
        report["selected_roll_type"] = roll.__dict__
        report["selected_constituency"] = constituency.__dict__
        report["languages"] = [{"code": code, "name": name} for code, name in languages]
        report["part_count"] = len(parts)
        report["part_sample"] = [part.__dict__ for part in parts[:10]]
        print(f"\nSelected AC: {constituency.text}")
        print(f"Languages: {', '.join(name for _code, name in languages) or '(none)'}")
        print(f"PDF parts found: {len(parts)}")
        for part in parts[:10]:
            print(f"  {part.part_number}  {part.part_name}")
        if len(parts) > 10:
            print(f"  ... {len(parts) - 10} more")

        structure = await self._page_structure()
        if structure["select_all_present"] and structure["visible_rows"]:
            selected = await self._select_current_page([part.part_number for part in parts[: config.PARTS_PER_PAGE]])
            structure["select_all_checked_count"] = len(selected)
            await self._clear_selection()
            print(f"Select All checked {len(selected)} visible row(s), then cleared the selection.")
        report["structure"] = structure
        report["network"] = [event.__dict__ for event in self.network.events]
        self._print_structure(structure)
        self._print_network()
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        config.DISCOVERY_REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nDiscovery report written to {config.DISCOVERY_REPORT}")
        print("No PDFs were downloaded.")
        return report

    async def run(
        self,
        *,
        state_name: str | None,
        state_code: str | None,
        all_states: bool,
        dry_run: bool,
        test_one: bool = False,
    ) -> None:
        await self._open()
        states = await self._states()
        chosen = states if all_states else [_pick_state(states, state_name, state_code, default_first=False)]
        for state in chosen:
            try:
                await self._process_state(state, dry_run=dry_run, test_one=test_one)
            except _TestFinished:
                logger.info("Test batch finished. Remaining combinations were not started.")
                return
            except Exception:
                logger.exception("State failed: %s", state.state_name)
                self.checkpoint.save()
                if not all_states:
                    raise

    async def _process_state(self, state: StateOption, *, dry_run: bool, test_one: bool = False) -> None:
        await self._select_state(state)
        await self._ensure_year()
        roll_types = await self._roll_types()
        if test_one:
            roll_types = roll_types[:1]
        logger.info("State: %s", state.state_name)
        logger.info("%s roll types found for %s", len(roll_types), state.state_name)
        for roll in roll_types:
            try:
                await self._select_roll_type(roll)
                controls = await self._dependent_controls()
                constituencies = [Option(**item) for item in controls["constituencies"]]
                if test_one:
                    constituencies = constituencies[:1]
                logger.info("Roll Type: %s", roll.text)
                logger.info(
                    "%s assembly constituencies for %s (district selector %s)",
                    len(constituencies),
                    roll.text,
                    "visible" if controls["district_visible"] else "absent",
                )
                for constituency in constituencies:
                    try:
                        await self._process_constituency(
                            state,
                            roll,
                            constituency,
                            dry_run=dry_run,
                            test_one=test_one,
                        )
                    except _TestFinished:
                        raise
                    except Exception as exc:
                        logger.exception("Assembly constituency failed: %s / %s", roll.text, constituency.text)
                        self._report_ac_failure(state, roll, constituency, exc)
                        self.checkpoint.save()
                        # Do not swallow permanently — resume will retry incomplete ACs from disk gaps.
                        # Continue so other ACs still get processed in this run.
            except _TestFinished:
                raise
            except Exception:
                logger.exception("Roll type failed: %s", roll.text)
                self.checkpoint.save()

    async def _process_constituency(
        self,
        state: StateOption,
        roll: Option,
        constituency: Option,
        *,
        dry_run: bool,
        test_one: bool = False,
    ) -> None:
        languages, parts = await self._select_constituency(constituency)
        district_name, district_code = self._district_for(parts)
        logger.info("AC: %s", constituency.text)
        logger.info("Total parts: %s", len(parts))
        if not languages:
            logger.warning("No language options for %s / %s", roll.text, constituency.text)
            return
        if config.ENGLISH_ONLY:
            filtered = [
                (code, name)
                for code, name in languages
                if code.upper() == "ENG" or name.upper() == "ENGLISH"
            ]
            if filtered:
                logger.info("English-only mode: keeping %s of %s languages", len(filtered), len(languages))
                languages = filtered
            else:
                logger.warning(
                    "English-only mode: no ENGLISH option for %s / %s (had %s)",
                    roll.text,
                    constituency.text,
                    languages,
                )
                return
        if test_one:
            languages = languages[:1]
        for code, name in languages:
            combo = Combination(
                state=state.state_name,
                state_code=state.state_code,
                year=config.REVISION_YEAR,
                roll_type=roll.text,
                roll_type_id=roll.value,
                district=district_name,
                district_code=district_code,
                ac=constituency.text,
                ac_number=constituency.value,
                language=name,
                language_code=code,
                parts_expected=[part.part_number for part in parts],
            )
            if dry_run:
                self._log_dry_run(combo, parts)
                continue
            try:
                await self._download_combination(combo, parts, test_one=test_one)
            except _TestFinished:
                raise
            except Exception as exc:
                # Keep going to the next language for this AC; gaps are already reported.
                logger.exception(
                    "Language incomplete for %s / %s: %s",
                    constituency.text,
                    name,
                    exc,
                )
                self.checkpoint.save()

    def _log_dry_run(self, combo: Combination, parts: list[PartRecord]) -> None:
        already = self._already_downloaded(combo)
        batches = plan_batches(parts, already)
        remaining = sum(len(batch) for batch in batches)
        print(
            "\n".join(
                [
                    f"State: {combo.state}",
                    f"Roll Type: {combo.roll_type}",
                    f"AC: {combo.ac}",
                    f"Language: {combo.language}",
                    f"Total Parts: {len(parts)}",
                    f"Already Downloaded: {len(already)}",
                    f"Remaining: {remaining}",
                    f"Required CAPTCHA submissions: {len(batches)}",
                    "",
                ]
            ),
            flush=True,
        )

    def _already_downloaded(self, combo: Combination) -> set[int]:
        on_disk = self.store.valid_part_numbers(combo)
        remembered = self.checkpoint.completed_parts(combo)
        confirmed = on_disk.intersection(remembered) if remembered else on_disk
        # A valid file is enough on its own. A checkpoint entry without a valid file is not.
        return on_disk if on_disk else confirmed

    async def _download_combination(self, combo: Combination, parts: list[PartRecord], *, test_one: bool) -> None:
        expected = [part.part_number for part in parts]
        already = self._already_downloaded(combo)
        if already:
            self.checkpoint.mark_completed(combo, sorted(already), expected)
        logger.info("State: %s", combo.state)
        logger.info("Roll Type: %s", combo.roll_type)
        logger.info("AC: %s", combo.ac)
        logger.info("Language: %s", combo.language)
        logger.info("Total parts: %s", len(parts))
        logger.info("Already downloaded: %s", len(already))
        remaining_count = sum(len(batch) for batch in plan_batches(parts, already))
        logger.info("Remaining: %s", remaining_count)
        if not parts:
            logger.warning("No parts to download for %s / %s", combo.ac, combo.language)
            self.checkpoint.mark_completed(combo, [], [])
            return
        if remaining_count == 0:
            logger.info("Batch complete")
            return

        await self._select_language(combo.language_code, combo.language)

        soft_skipped: set[int] = set()
        while True:
            already = self._already_downloaded(combo)
            batches = plan_batches(parts, already | soft_skipped)
            if not batches:
                break
            if test_one:
                batches = batches[:1]

            batch = batches[0]
            wanted = [part.part_number for part in batch]
            span = f"{wanted[0]}-{wanted[-1]}" if len(wanted) > 1 else str(wanted[0])
            batch_ok = False
            last_error: Exception | None = None

            for attempt in range(1, config.MAX_BATCH_RETRIES + 1):
                try:
                    await self._ensure_part_table_ready(combo.ac, len(parts))
                    page_index = _page_index(parts, batch[0].part_number)
                    await self._goto_page(page_index)
                    selected = await self._select_current_page(wanted)
                    if set(selected) != set(wanted):
                        raise PortalError(
                            f"Selected {selected} for {combo.ac}, expected {wanted}. The batch was not submitted."
                        )
                    logger.info("Batch: %s", span)
                    logger.info("Selected parts: %s", len(selected))
                    self.checkpoint.note_attempt(combo, expected)
                    files = await wait_for_auto_captcha(
                        self.page,
                        self.network,
                        state=combo.state,
                        year=combo.year,
                        roll_type=combo.roll_type,
                        constituency=combo.ac,
                        language=combo.language,
                        part_numbers=wanted,
                    )
                    saved = self.store.save_batch(combo, batch, files)
                    missed = [number for number in wanted if number not in saved]
                    if saved:
                        self.checkpoint.mark_completed(combo, saved, expected)
                    if missed:
                        self.checkpoint.mark_failed(combo, missed, expected)
                        logger.warning(
                            "Batch %s saved %s of %s parts (attempt %s/%s). Missing: %s",
                            span,
                            len(saved),
                            len(wanted),
                            attempt,
                            config.MAX_BATCH_RETRIES,
                            missed,
                        )
                        # Partial save: replan remaining; do not soft-skip saved parts.
                        batch_ok = True
                        break
                    logger.info("Batch complete")
                    batch_ok = True
                    break
                except _TestFinished:
                    raise
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "Batch %s failed (attempt %s/%s) for %s / %s: %s",
                        span,
                        attempt,
                        config.MAX_BATCH_RETRIES,
                        combo.ac,
                        combo.language,
                        exc,
                    )
                    self.checkpoint.mark_failed(combo, wanted, expected)
                    await self._recover_after_batch_error(combo)
                    await _pause()

            if test_one:
                raise _TestFinished

            if batch_ok:
                await self._wait_for_selection_reset()
                continue

            # Exhausted retries for this window — soft-skip it in THIS run and keep going
            # so later parts of the same AC still download. Resume will retry soft-skipped parts.
            still_missing = [n for n in wanted if n not in self._already_downloaded(combo)]
            soft_skipped.update(still_missing)
            logger.error(
                "Soft-skipping batch %s for %s / %s after %s attempts (will retry on resume). Last error: %s",
                span,
                combo.ac,
                combo.language,
                config.MAX_BATCH_RETRIES,
                last_error,
            )
            self._report_gaps(
                combo,
                expected,
                self._already_downloaded(combo),
                still_missing,
                reason=f"batch_retries_exhausted: {last_error}",
            )

        final_have = self._already_downloaded(combo)
        missing = [number for number in expected if number not in final_have]
        if missing:
            self._report_gaps(
                combo,
                expected,
                final_have,
                missing,
                reason="combination_incomplete",
            )
            raise PortalError(
                f"{combo.ac} / {combo.language}: downloaded {len(final_have)}/{len(expected)} parts; "
                f"missing {len(missing)} parts {missing[:20]}{'...' if len(missing) > 20 else ''}. "
                f"See {config.GAPS_REPORT}"
            )
        logger.info("All %s parts complete for %s / %s", len(expected), combo.ac, combo.language)

    async def _recover_after_batch_error(self, combo: Combination) -> None:
        """Re-settle the part table after a DOM/captcha glitch mid-AC."""
        try:
            await self._select_language(combo.language_code, combo.language)
            await self._ensure_part_table_ready(combo.ac, len(combo.parts_expected) or 1)
        except Exception as exc:
            logger.warning("Table recovery after batch error failed: %s", exc)

    def _report_gaps(
        self,
        combo: Combination,
        expected: list[int],
        have: set[int],
        missing: list[int],
        *,
        reason: str,
    ) -> None:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "state": combo.state,
            "state_code": combo.state_code,
            "roll_type": combo.roll_type,
            "ac": combo.ac,
            "ac_code": combo.ac_number,
            "language": combo.language,
            "language_code": combo.language_code,
            "expected_count": len(expected),
            "downloaded_count": len(have),
            "missing_count": len(missing),
            "missing_parts": missing,
            "downloaded_parts": sorted(have),
        }
        with config.GAPS_REPORT.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(
            "\n".join(
                [
                    "",
                    "==================================================",
                    "DOWNLOAD GAP / SKIP REPORT",
                    "==================================================",
                    f"AC: {combo.ac}",
                    f"Language: {combo.language}",
                    f"Reason: {reason}",
                    f"Downloaded: {len(have)} / {len(expected)}",
                    f"Missing ({len(missing)}): {missing[:30]}{'...' if len(missing) > 30 else ''}",
                    f"Written to: {config.GAPS_REPORT}",
                    "Resume later with: python scraper.py --resume --state \"NCT OF Delhi\"",
                    "==================================================",
                    "",
                ]
            ),
            flush=True,
        )
        logger.error(
            "GAP %s / %s: %s/%s downloaded, missing %s — %s",
            combo.ac,
            combo.language,
            len(have),
            len(expected),
            missing,
            reason,
        )

    def _report_ac_failure(
        self,
        state: StateOption,
        roll: Option,
        constituency: Option,
        exc: Exception,
    ) -> None:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "reason": "ac_exception",
            "state": state.state_name,
            "state_code": state.state_code,
            "roll_type": roll.text,
            "ac": constituency.text,
            "ac_code": constituency.value,
            "error": str(exc),
        }
        with config.GAPS_REPORT.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(
            "\n".join(
                [
                    "",
                    "==================================================",
                    "AC SKIPPED AFTER FAILURE",
                    "==================================================",
                    f"AC: {constituency.text}",
                    f"Roll: {roll.text}",
                    f"Error: {exc}",
                    f"Logged in: {config.GAPS_REPORT}",
                    "Incomplete parts will be retried on --resume",
                    "==================================================",
                    "",
                ]
            ),
            flush=True,
        )

    async def _open(self) -> None:
        async def attempt() -> None:
            await self.page.goto(
                config.PORTAL_URL,
                wait_until="domcontentloaded",
                timeout=config.NAVIGATION_TIMEOUT_MS,
            )
            await self.page.wait_for_selector(selectors.STATE_SELECT, timeout=config.NAVIGATION_TIMEOUT_MS)

        await _retry("Open portal", attempt)

    async def _states(self) -> list[StateOption]:
        options = await _read_options(self.page, selectors.STATE_SELECT)
        states = [StateOption(state_name=option.text, state_code=option.value) for option in options if option.value]
        if not states:
            raise PortalError("The state dropdown did not contain any states")
        return states

    async def _select_state(self, state: StateOption) -> None:
        async def attempt() -> None:
            current = await self.page.locator(selectors.STATE_SELECT).input_value()
            if current != state.state_code:
                async with self.page.expect_response(
                    lambda response: selectors.API_ROLL_TYPES in response.url and response.status == 200,
                    timeout=config.NAVIGATION_TIMEOUT_MS,
                ):
                    await self.page.select_option(selectors.STATE_SELECT, state.state_code)
                await _pause()
            await self.page.wait_for_function(
                """(expected) => {
                    const el = document.querySelector('#stateCode');
                    return !!el && el.value === expected;
                }""",
                arg=state.state_code,
                timeout=config.ACTION_TIMEOUT_MS,
            )
            await self.page.wait_for_selector(selectors.ROLL_TYPE_SELECT, timeout=config.NAVIGATION_TIMEOUT_MS)

        await _retry(f"Select state {state.state_name}", attempt)
        logger.info("State selected: %s", state.state_name)

    async def _ensure_year(self) -> None:
        value = await self.page.locator(selectors.YEAR_SELECT).input_value()
        if value == config.REVISION_YEAR:
            logger.info("Year of revision verified: %s", value)
            return

        async def attempt() -> None:
            async with self.page.expect_response(
                lambda response: selectors.API_ROLL_TYPES in response.url and response.status == 200,
                timeout=config.NAVIGATION_TIMEOUT_MS,
            ):
                await self.page.select_option(selectors.YEAR_SELECT, config.REVISION_YEAR)
            await _pause()
            selected = await self.page.locator(selectors.YEAR_SELECT).input_value()
            if selected != config.REVISION_YEAR:
                raise PortalError(f"Year is {selected}, expected {config.REVISION_YEAR}")

        await _retry("Select revision year 2026", attempt)
        logger.info("Year of revision verified: %s", config.REVISION_YEAR)

    async def _roll_types(self) -> list[Option]:
        await self.page.wait_for_function(
            """() => {
                const el = document.querySelector('#roleType');
                return !!el && [...el.options].some(option => option.value);
            }""",
            timeout=config.NAVIGATION_TIMEOUT_MS,
        )
        options = [option for option in await _read_options(self.page, selectors.ROLL_TYPE_SELECT) if option.value]
        if not options:
            raise PortalError("No roll types were returned for this state and year")
        return options

    async def _select_roll_type(self, roll: Option) -> None:
        async def attempt() -> None:
            current = await self.page.locator(selectors.ROLL_TYPE_SELECT).input_value()
            if current != roll.value:
                await self.page.select_option(selectors.ROLL_TYPE_SELECT, roll.value)
                await _pause()
            selected = await self.page.locator(selectors.ROLL_TYPE_SELECT).input_value()
            if selected != roll.value:
                raise PortalError(f"Roll type is {selected}, expected {roll.value}")
            await self.page.wait_for_function(
                """() => {
                    const el = document.querySelector('#constituency');
                    return !!el && [...el.options].some(option => option.value);
                }""",
                timeout=config.ACTION_TIMEOUT_MS,
            )

        await _retry(f"Select roll type {roll.text}", attempt)
        logger.info("Roll Type selected: %s", roll.text)

    async def _dependent_controls(self) -> dict[str, Any]:
        district_locator = self.page.locator(selectors.DISTRICT_SELECT)
        district_visible = await district_locator.count() > 0 and await district_locator.first.is_visible()
        districts: list[Option] = []
        if district_visible:
            districts = [option for option in await _read_options(self.page, selectors.DISTRICT_SELECT) if option.value]
            for option in districts:
                self.district_names[option.value] = option.text
            if await district_locator.input_value():
                await self.page.select_option(selectors.DISTRICT_SELECT, "")
                await _pause()
        constituencies = [option for option in await _read_options(self.page, selectors.AC_SELECT) if option.value]
        return {
            "district_visible": district_visible,
            "districts": [option.__dict__ for option in districts],
            "constituencies": [option.__dict__ for option in constituencies],
        }

    async def _select_constituency(self, constituency: Option) -> tuple[list[tuple[str, str]], list[PartRecord]]:
        async def attempt() -> tuple[list[tuple[str, str]], list[PartRecord]]:
            # Reset AC first so retries always trigger fresh language/part API calls.
            # Selecting the same value again does not refetch and expect_response times out.
            # Also drain any clear-triggered responses so they cannot steal the next waiters.
            current = await self.page.locator(selectors.AC_SELECT).input_value()
            if current:
                try:
                    async with self.page.expect_response(_is_parts, timeout=8_000):
                        async with self.page.expect_response(_is_languages, timeout=8_000):
                            await self.page.select_option(selectors.AC_SELECT, "")
                except PlaywrightTimeoutError:
                    try:
                        await self.page.select_option(selectors.AC_SELECT, "")
                    except Exception:
                        pass
                await _pause()

            self.network.languages = []
            self.network.parts = []

            languages: list[tuple[str, str]] = []
            parts: list[PartRecord] = []
            try:
                async with self.page.expect_response(
                    _is_parts, timeout=config.NAVIGATION_TIMEOUT_MS
                ) as parts_info:
                    async with self.page.expect_response(
                        _is_languages, timeout=config.NAVIGATION_TIMEOUT_MS
                    ) as language_info:
                        await self.page.select_option(selectors.AC_SELECT, constituency.value)
                await _pause()
                languages = _languages_from_json(await (await language_info.value).json())
                parts = _parts_from_json(await (await parts_info.value).json())
            except PlaywrightTimeoutError:
                # APIs often already completed (NetworkRecorder saw them) while expect_response raced.
                await asyncio.sleep(1.0)
                languages = list(self.network.languages)
                parts = list(self.network.parts)
                if not languages and not parts:
                    raise
                logger.warning(
                    "AC %s: using network-captured data after expect_response timeout "
                    "(%s languages, %s parts)",
                    constituency.text,
                    len(languages),
                    len(parts),
                )

            # Prefer recorder data if response JSON was empty but the listener already filled it.
            if not languages and self.network.languages:
                languages = list(self.network.languages)
            if not parts and self.network.parts:
                parts = list(self.network.parts)

            selected = await self.page.locator(selectors.AC_SELECT).input_value()
            if selected != constituency.value:
                raise PortalError(
                    f"Assembly constituency is {selected}, expected {constituency.value}"
                )
            if not parts:
                raise PortalError(f"No parts returned for assembly constituency {constituency.text}")

            await self.page.wait_for_selector(
                selectors.LANGUAGE_SELECT, state="attached", timeout=config.ACTION_TIMEOUT_MS
            )
            await self._ensure_part_table_ready(constituency.text, len(parts))
            return languages, parts

        languages, parts = await _retry(f"Select AC {constituency.text}", attempt)
        logger.info("AC selected: %s", constituency.text)
        return languages, parts

    async def _ensure_part_table_ready(self, ac_label: str, part_count: int) -> None:
        """Wait for part rows in the DOM. Do not require Playwright 'visible'
        (overlays / delayed paint often fail that check even when the API succeeded).
        """
        try:
            await self.page.wait_for_function(
                """() => {
                    const lang = document.querySelector('#langCd');
                    const hasLang = !!lang && [...lang.options].some(option => option.value);
                    const rows = document.querySelectorAll('table.contenttable-eroll tbody tr').length;
                    return hasLang && rows > 0;
                }""",
                timeout=config.ACTION_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            # Language may be present but not auto-selected; pick the first real option.
            lang = self.page.locator(selectors.LANGUAGE_SELECT)
            if await lang.count():
                options = [option for option in await _read_options(self.page, selectors.LANGUAGE_SELECT) if option.value]
                if options and not await lang.input_value():
                    await self.page.select_option(selectors.LANGUAGE_SELECT, options[0].value)
                    await _pause()
            try:
                await self.page.wait_for_function(
                    """() => document.querySelectorAll('table.contenttable-eroll tbody tr').length > 0""",
                    timeout=config.ACTION_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError:
                logger.warning(
                    "Part table rows still missing for %s after API returned %s parts; "
                    "will rely on API list and retry row selection later",
                    ac_label,
                    part_count,
                )
                return

        table = self.page.locator(selectors.PART_TABLE)
        if await table.count():
            try:
                await table.first.scroll_into_view_if_needed()
            except Exception:
                pass

    async def _select_language(self, code: str, name: str) -> None:
        async def attempt() -> None:
            current = await self.page.locator(selectors.LANGUAGE_SELECT).input_value()
            if current != code:
                await self.page.select_option(selectors.LANGUAGE_SELECT, code)
                await _pause()
            selected = await self.page.locator(selectors.LANGUAGE_SELECT).input_value()
            if selected != code:
                raise PortalError(f"Language is {selected}, expected {code}")

        await _retry(f"Select language {name}", attempt)
        logger.info("Language selected: %s", name)

    async def _select_current_page(self, wanted_numbers: list[int]) -> list[int]:
        wanted = set(wanted_numbers)
        await self._clear_selection()
        search = self.page.locator(selectors.SEARCH_INPUT)
        if await search.count() and await search.input_value():
            await search.fill("")
            await _pause()

        # Wait for a stable row set — the table often re-renders after downloads/captcha.
        rows = self.page.locator(selectors.PART_ROW)
        for _ in range(10):
            try:
                await self.page.wait_for_function(
                    """() => document.querySelectorAll('table.contenttable-eroll tbody tr').length > 0""",
                    timeout=3_000,
                )
                await self.page.wait_for_timeout(400)
                if await rows.count() > 0:
                    break
            except PlaywrightTimeoutError:
                await self.page.wait_for_timeout(500)
        if await rows.count() == 0:
            return []

        row_count = await rows.count()
        visible: list[tuple[int, int]] = []
        for index in range(row_count):
            try:
                text = " ".join((await rows.nth(index).inner_text()).split())
            except Exception:
                continue
            number_text = text.split(" - ", 1)[0].strip()
            if not number_text.isdigit():
                continue
            visible.append((index, int(number_text)))
        targets = [(index, number) for index, number in visible if number in wanted]
        if not targets:
            return []
        if len(targets) > config.MAX_PARTS_PER_DOWNLOAD:
            raise PortalError(
                f"Refusing to select {len(targets)} parts. The portal limit is {config.MAX_PARTS_PER_DOWNLOAD}."
            )

        select_all = self.page.locator(selectors.SELECT_ALL)
        use_select_all = (
            len(targets) == len(visible)
            and len(visible) <= config.MAX_PARTS_PER_DOWNLOAD
            and await select_all.count() > 0
        )
        if use_select_all:
            await self._click_checkbox(select_all)
        else:
            for index, _number in targets:
                box = rows.nth(index).locator(selectors.ROW_CHECKBOX)
                try:
                    if not await box.is_checked():
                        await self._click_checkbox(box)
                except Exception:
                    await self._click_checkbox(box)

        await self.page.wait_for_timeout(300)
        if await self.page.get_by_text("Maximum 10 Parts are allow once at a time").count():
            raise PortalError("The portal rejected the selection because it was over 10 parts")
        selected = await self._checked_part_numbers()
        if set(selected) != {number for _index, number in targets}:
            for index, _number in targets:
                box = rows.nth(index).locator(selectors.ROW_CHECKBOX)
                try:
                    if not await box.is_checked():
                        await self._click_checkbox(box)
                except Exception:
                    await self._click_checkbox(box)
            selected = await self._checked_part_numbers()
        return selected

    async def _click_checkbox(self, locator) -> None:
        """Click without scroll_into_view — that throws when React re-renders the table."""
        last_error: Exception | None = None
        for _ in range(4):
            try:
                if await locator.count() == 0:
                    await self.page.wait_for_timeout(300)
                    continue
                await locator.click(force=True, timeout=5_000)
                return
            except Exception as exc:
                last_error = exc
                await self.page.wait_for_timeout(400)
        # Last resort: JS click on the first matching node.
        try:
            handle = await locator.element_handle(timeout=2_000)
            if handle is not None:
                await handle.evaluate("el => el.click()")
                return
        except Exception as exc:
            last_error = exc
        raise PortalError(f"Could not click checkbox: {last_error}")

    async def _checked_part_numbers(self) -> list[int]:
        rows = self.page.locator(selectors.PART_ROW)
        selected: list[int] = []
        for index in range(await rows.count()):
            box = rows.nth(index).locator(selectors.ROW_CHECKBOX)
            if await box.count() == 0 or not await box.is_checked():
                continue
            text = " ".join((await rows.nth(index).inner_text()).split())
            number_text = text.split(" - ", 1)[0].strip()
            if number_text.isdigit():
                selected.append(int(number_text))
        return selected

    async def _clear_selection(self) -> None:
        for _ in range(15):
            checked = self.page.locator(f"{selectors.PART_ROW} {selectors.ROW_CHECKBOX}:checked")
            count = await checked.count()
            if count == 0:
                return
            box = checked.first
            try:
                await self._click_checkbox(box)
            except Exception:
                break
            await self.page.wait_for_timeout(100)

    async def _goto_page(self, index: int) -> None:
        if await self.page.locator(selectors.PART_TABLE).count() == 0:
            return
        await _click_pager(self.page, "<<")
        for _ in range(index):
            moved = await _click_pager(self.page, ">")
            if not moved:
                break
            await _pause()
        if await self.page.locator(selectors.PAGE_INDICATOR).count():
            await self.page.wait_for_function(
                """(expected) => {
                    const el = document.querySelector('.pagination .control-btn2 strong');
                    return !!el && el.textContent.trim() === String(expected);
                }""",
                arg=index + 1,
                timeout=config.ACTION_TIMEOUT_MS,
            )

    async def _wait_for_selection_reset(self) -> None:
        try:
            await self.page.wait_for_function(
                """() => document.querySelectorAll("table.contenttable-eroll tbody input[type='checkbox']:checked").length === 0""",
                timeout=10_000,
            )
        except PlaywrightTimeoutError:
            logger.warning("Part checkboxes were still selected after the download")
        await _pause()

    async def _page_structure(self) -> dict[str, Any]:
        return await self.page.evaluate(
            """() => {
                const captcha = document.querySelector("img[alt='Captcha']");
                const refresh = document.querySelector("img[alt='refrsh captcha']");
                const audio = document.querySelector("img[alt='Read aloud captcha']");
                const download = [...document.querySelectorAll('button')].find(button => (button.innerText || '').includes('Download Selected PDFs'));
                const srcKind = (img) => {
                    if (!img || !img.src) return '';
                    if (img.src.startsWith('data:')) return 'data-url';
                    if (img.src.startsWith('blob:')) return 'blob';
                    return img.src.split('?')[0];
                };
                return {
                    selects: [...document.querySelectorAll('select')].map(el => el.id),
                    captcha_input: '#captcha',
                    captcha_image: "img[alt='Captcha']",
                    captcha_width: captcha ? captcha.naturalWidth : 0,
                    captcha_height: captcha ? captcha.naturalHeight : 0,
                    captcha_src_kind: srcKind(captcha),
                    captcha_refresh: "img[alt='refrsh captcha']",
                    captcha_refresh_src: srcKind(refresh),
                    captcha_audio: "img[alt='Read aloud captcha']",
                    captcha_audio_present: !!audio,
                    download_button: download ? download.innerText.trim() : '',
                    select_all_present: !!document.querySelector('#selectAll'),
                    select_all: '#selectAll',
                    table: 'table.contenttable-eroll',
                    headers: [...document.querySelectorAll('table.contenttable-eroll thead th')].map(th => th.innerText.trim()),
                    visible_rows: document.querySelectorAll('table.contenttable-eroll tbody tr').length,
                    search_placeholder: (document.querySelector("input[placeholder='Search']") || {}).placeholder || '',
                    pager: [...document.querySelectorAll('.pagination button')].map(button => button.innerText.trim()),
                    page_size: 10,
                    portal_part_limit: 10,
                };
            }"""
        )

    def _district_for(self, parts: list[PartRecord]) -> tuple[str, str]:
        codes = []
        for part in parts:
            if part.district_code and part.district_code not in codes:
                codes.append(part.district_code)
        if not codes:
            return "", ""
        names = [self.district_names.get(code, code) for code in codes]
        return ", ".join(names), ", ".join(codes)

    def _print_structure(self, structure: dict[str, Any]) -> None:
        print("\nDiscovered selectors")
        print("  State: select#stateCode")
        print("  Year: select#revyear")
        print("  Roll type: select#roleType")
        print("  District: select#district (removed from the page for bye-election roll types)")
        print("  Assembly constituency: select#constituency")
        print("  Language: select#langCd")
        print(f"  CAPTCHA image: {structure['captcha_image']} ({structure['captcha_width']}x{structure['captcha_height']}, {structure['captcha_src_kind']})")
        print(f"  CAPTCHA refresh: {structure['captcha_refresh']}")
        print(f"  CAPTCHA audio: {structure['captcha_audio']} present={structure['captcha_audio_present']}")
        print("  CAPTCHA input: input#captcha")
        print(f"  Download button: {structure['download_button']!r}")
        print(f"  Select all: {structure['select_all']} present={structure['select_all_present']}")
        print(f"  Table: {structure['table']} headers={structure['headers']}")
        print(f"  Visible rows: {structure['visible_rows']}  pager={structure['pager']}")
        print(f"  Search: placeholder {structure['search_placeholder']!r}")
        print("  The part table pages 10 rows at a time, and the portal rejects more than 10 parts per download.")

    def _print_network(self) -> None:
        print("\nRelevant network calls")
        for event in self.network.events:
            print(f"  {event.status} {event.method} {event.path}  {event.note}")


def _pick_state(
    states: list[StateOption],
    state_name: str | None,
    state_code: str | None,
    *,
    default_first: bool,
) -> StateOption:
    if state_name is None and state_code is None:
        if default_first:
            return states[0]
        raise PortalError("Pass --state or --state-code. Add --all-states only when you intend to walk every state.")
    matches = []
    for state in states:
        name_ok = state_name is None or state.state_name.lower() == state_name.lower()
        code_ok = state_code is None or state.state_code.lower() == state_code.lower()
        if state_name is not None and state_code is not None:
            if name_ok and code_ok:
                matches.append(state)
        elif name_ok and code_ok:
            matches.append(state)
    if not matches:
        raise PortalError(f"No state matched name={state_name!r} code={state_code!r}")
    if len(matches) > 1:
        raise PortalError("More than one state matched. Pass --state-code as well.")
    return matches[0]


def _selected_or_first(options: list[Option], current: str) -> Option:
    for option in options:
        if option.value == current:
            return option
    return options[0]


def _page_index(parts: list[PartRecord], part_number: int) -> int:
    for index, part in enumerate(parts):
        if part.part_number == part_number:
            return index // config.PARTS_PER_PAGE
    return 0


def _is_parts(response) -> bool:
    return selectors.API_PARTS in response.url and response.request.method == "POST"


def _is_languages(response) -> bool:
    return selectors.API_LANGUAGES in response.url and response.request.method == "POST"


def _parts_from_json(data: dict) -> list[PartRecord]:
    payload = data.get("payload") if isinstance(data, dict) else None
    parts: list[PartRecord] = []
    if not isinstance(payload, list):
        return parts
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
    return parts


def _languages_from_json(data: dict) -> list[tuple[str, str]]:
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return []
    return [(str(code), str(name)) for code, name in payload.items()]


async def _read_options(page: Page, selector: str) -> list[Option]:
    await page.wait_for_selector(selector, state="attached", timeout=config.ACTION_TIMEOUT_MS)
    raw = await page.locator(selector).evaluate(
        """element => [...element.options].map(option => ({
            value: option.value,
            text: (option.textContent || '').trim()
        }))"""
    )
    return [Option(value=item["value"], text=item["text"]) for item in raw]


async def _click_pager(page: Page, label: str) -> bool:
    buttons = page.locator(selectors.PAGINATION_BUTTONS)
    count = await buttons.count()
    for index in range(count):
        button = buttons.nth(index)
        if (await button.inner_text()).strip() != label:
            continue
        if await button.is_disabled():
            return False
        await button.click()
        return True
    return False


async def _pause() -> None:
    await asyncio.sleep(random.uniform(config.MIN_DELAY, config.MAX_DELAY))


async def _retry(label: str, action):
    last_error: Exception | None = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            return await action()
        except Exception as exc:
            last_error = exc
            logger.warning("%s failed (%s/%s): %s", label, attempt, config.MAX_RETRIES, exc)
            if attempt == config.MAX_RETRIES:
                break
            await _pause()
    assert last_error is not None
    raise last_error

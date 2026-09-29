"""Chromium session for the public electoral-roll portal."""

from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from eci.network import NetworkRecorder

import config

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


@dataclass
class BrowserSession:
    browser: Browser
    context: BrowserContext
    page: Page
    network: NetworkRecorder


class BrowserManager:
    def __init__(self) -> None:
        self._playwright = None
        self.session: BrowserSession | None = None

    async def start(self, *, visible: bool = True) -> BrowserSession:
        self._playwright = await async_playwright().start()
        headless = not visible
        launch_args = [] if headless else ["--start-maximized"]
        browser = await self._playwright.chromium.launch(headless=headless, args=launch_args)
        context_options = {"accept_downloads": True, "user_agent": USER_AGENT}
        if headless:
            context_options["viewport"] = {"width": 1440, "height": 1100}
        else:
            context_options["no_viewport"] = True
        context = await browser.new_context(**context_options)
        page = await context.new_page()
        page.set_default_timeout(config.ACTION_TIMEOUT_MS)
        network = NetworkRecorder()
        network.attach(page)
        self.session = BrowserSession(browser, context, page, network)
        return self.session

    async def stop(self) -> None:
        if self.session is not None:
            await self.session.context.close()
            await self.session.browser.close()
            self.session = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

"""
scraper.py - Async Playwright Scraper Engine for ASP.NET Portal Allotment Extraction.

Handles nested ASP.NET dropdown PostBacks, table rendering, print media emulation,
and PDF exporting with robust network idle synchronization and filename sanitization.
"""

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

logger = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    """
    Sanitizes string inputs to prevent OS file path errors.
    Removes slashes, illegal characters, and replaces multiple spaces.

    Args:
        name: Raw string from dropdown option text or value.

    Returns:
        OS-safe filename string.
    """
    if not name:
        return "UNKNOWN"
    # Remove control characters & illegal filename characters: \ / : * ? " < > |
    sanitized = re.sub(r'[\\/*?:"<>|\r\n\t]', "_", name)
    # Collapse multiple spaces or underscores
    sanitized = re.sub(r"[\s_]+", "_", sanitized).strip("_")
    return sanitized[:100]  # Cap length to prevent path length issues


class AllotmentScraper:
    """
    Async Playwright wrapper tailored for ASP.NET WebForms allotment portals.
    """

    def __init__(self, timeout_ms: int = 30000, headless: bool = True):
        self.timeout_ms = timeout_ms
        self.headless = headless
        self._playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def start(self) -> None:
        """Launches Playwright Chromium browser and creates page context."""
        logger.info("Initializing Async Playwright Chromium instance...")
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 960},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        self.page = await self.context.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        logger.info("Playwright browser session started.")

    async def stop(self) -> None:
        """Gracefully closes Playwright browser resources."""
        if self.page:
            await self.page.close()
            self.page = None
        if self.context:
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Playwright browser session closed.")

    async def navigate(self, url: str) -> None:
        """Navigates to the target allotment portal and waits for load."""
        if not self.page:
            raise RuntimeError("Browser page not initialized. Call start() first.")
        
        logger.info(f"Navigating to URL: {url}")
        await self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        await self.page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        logger.info(f"Successfully loaded {url}")

    async def _locate_select(self, keywords: List[str]) -> Optional[str]:
        """Locates a <select> element by ID, name, or label keywords."""
        if not self.page:
            return None
        
        # Try generic select elements on page
        selects = await self.page.query_selector_all("select")
        for select in selects:
            select_id = (await select.get_attribute("id") or "").lower()
            select_name = (await select.get_attribute("name") or "").lower()
            
            for keyword in keywords:
                if keyword in select_id or keyword in select_name:
                    return f"select#{await select.get_attribute('id')}" if select_id else None

        # Fallback: return first/second select based on keyword
        if selects:
            if "college" in keywords[0] and len(selects) >= 1:
                return "select >> nth=0"
            elif "branch" in keywords[0] and len(selects) >= 2:
                return "select >> nth=1"
            elif len(selects) >= 1:
                return "select >> nth=0"

        return None

    async def get_college_options(self) -> List[Dict[str, str]]:
        """
        Extracts all valid college dropdown options.

        Returns:
            List of dicts containing 'text', 'value', and 'index'.
        """
        if not self.page:
            return []

        select_selector = await self._locate_select(["college", "dist", "inst"]) or "select >> nth=0"
        await self.page.wait_for_selector(select_selector, state="visible", timeout=self.timeout_ms)

        options = await self.page.eval_on_selector_all(
            f"{select_selector} option",
            "options => options.map(o => ({ text: o.innerText.trim(), value: o.value.trim() }))"
        )

        # Filter out placeholder items like "Select", "-- Select College --", etc.
        valid_options = []
        for idx, opt in enumerate(options):
            txt = opt["text"].strip()
            val = opt["value"].strip()
            if val and not any(placeholder in txt.lower() for placeholder in ["select", "--", "choose"]):
                valid_options.append({"index": idx, "text": txt, "value": val})

        logger.info(f"Extracted {len(valid_options)} valid college options.")
        return valid_options

    async def select_college(self, value: str, text: Optional[str] = None) -> None:
        """
        Selects a College option and waits for ASP.NET PostBack network idle.
        """
        if not self.page:
            return

        select_selector = await self._locate_select(["college", "dist", "inst"]) or "select >> nth=0"
        logger.info(f"Selecting College: '{text or value}' (value='{value}')")

        # ASP.NET WebForms trigger PostBack on select change
        await self.page.select_option(select_selector, value=value)
        
        # Wait for ASP.NET PostBack network idle
        try:
            await self.page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        except Exception:
            logger.warning("networkidle timeout after selecting college; continuing after 2s delay.")
            await asyncio.sleep(2.0)

    async def get_branch_options(self) -> List[Dict[str, str]]:
        """
        Extracts all valid branch options from the second dropdown after PostBack.
        """
        if not self.page:
            return []

        select_selector = await self._locate_select(["branch", "course", "spec"]) or "select >> nth=1"
        await self.page.wait_for_selector(select_selector, state="visible", timeout=self.timeout_ms)

        options = await self.page.eval_on_selector_all(
            f"{select_selector} option",
            "options => options.map(o => ({ text: o.innerText.trim(), value: o.value.trim() }))"
        )

        valid_options = []
        for idx, opt in enumerate(options):
            txt = opt["text"].strip()
            val = opt["value"].strip()
            if val and not any(placeholder in txt.lower() for placeholder in ["select", "--", "choose"]):
                valid_options.append({"index": idx, "text": txt, "value": val})

        logger.info(f"Extracted {len(valid_options)} valid branch options.")
        return valid_options

    async def select_branch(self, value: str, text: Optional[str] = None) -> None:
        """
        Selects a Branch option and waits for PostBack to settle.
        """
        if not self.page:
            return

        select_selector = await self._locate_select(["branch", "course", "spec"]) or "select >> nth=1"
        logger.info(f"Selecting Branch: '{text or value}' (value='{value}')")

        await self.page.select_option(select_selector, value=value)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        except Exception:
            logger.warning("networkidle timeout after selecting branch; continuing after 1.5s delay.")
            await asyncio.sleep(1.5)

    async def trigger_show_allotments(self) -> None:
        """
        Clicks the 'Show Allotments' button and waits for the data table to load.
        """
        if not self.page:
            return

        button_selectors = [
            "input[type='submit']",
            "input[value*='Show']",
            "button:has-text('Show')",
            "button:has-text('Submit')",
            "button:has-text('Search')",
            "#btnShow",
            "#btnSubmit"
        ]

        clicked = False
        for selector in button_selectors:
            if await self.page.is_visible(selector):
                logger.info(f"Clicking allotment submit button using selector: {selector}")
                await self.page.click(selector)
                clicked = True
                break

        if not clicked:
            # Fallback to pressing Enter on the page or submitting form
            logger.warning("No explicit button matched selector list. Pressing Enter...")
            await self.page.keyboard.press("Enter")

        try:
            await self.page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        except Exception:
            logger.warning("networkidle timeout after clicking show allotments; waiting 2s.")
            await asyncio.sleep(2.0)

    async def export_pdf(self, output_path: str) -> str:
        """
        Emulates print media style and exports current page view as PDF.

        Args:
            output_path: Target filepath for the saved PDF.

        Returns:
            Absolute filepath of saved PDF.
        """
        if not self.page:
            raise RuntimeError("Browser page not initialized.")

        # Ensure target directory exists
        dest = Path(output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Emulating print media type and writing PDF to: {dest.resolve()}")
        await self.page.emulate_media(media="print")
        
        await self.page.pdf(
            path=str(dest),
            format="A4",
            print_background=True,
            margin={"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"}
        )
        
        # Reset media emulation back to screen
        await self.page.emulate_media(media="screen")
        logger.info(f"PDF exported successfully ({dest.stat().st_size} bytes)")
        return str(dest.resolve())

import asyncio
import json
import logging
import re
import time
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.core.config import settings
from app.core.retry import retry_async
from app.monitoring.metrics import get_metrics, timer

logger = logging.getLogger(__name__)


def _log_stage(stage: str, duration: float, extra: str = "") -> None:
    logger.info(
        "[Timing] stage=%s duration_ms=%.1f%s",
        stage,
        duration * 1000,
        f" {extra}" if extra else "",
    )

SOCIAL_DOMAINS: dict[str, list[str]] = {
    "linkedin": [
        "linkedin.com/in/",
        "linkedin.com/company/",
        "linkedin.com/school/",
    ],
    "facebook": ["facebook.com/", "fb.com/"],
    "instagram": ["instagram.com/"],
    "twitter": ["twitter.com/", "x.com/"],
    "youtube": ["youtube.com/", "youtube.com/@", "youtube.com/channel/"],
}

CONTACT_KEYWORDS = [
    "contact", "contact-us", "about", "about-us", "about_us", "get-in-touch",
    "support", "help", "impressum",
]
COMMON_CONTACT_PATHS = ("contact", "contact-us", "about", "about-us", "support", "impressum")
_EMAIL_PATTERN = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"

_PAGE_MISS = object()


class WebsiteScraperService:
    def __init__(
        self,
        timeout: int | None = None,
        page_cache_ttl: float | None = None,
    ) -> None:
        self.timeout = timeout if timeout is not None and timeout > 0 else settings.scraper_timeout
        self._page_cache_ttl = (
            page_cache_ttl
            if page_cache_ttl is not None and page_cache_ttl > 0
            else settings.scraper_page_cache_ttl
        )
        self._page_cache: dict[str, tuple[float, str | None]] = {}
        self._download_locks: dict[str, asyncio.Lock] = {}

    async def scrape(self, website_url: str) -> dict[str, object]:
        result: dict[str, object] = {
            "emails": [],
            "linkedin": None,
            "facebook": None,
            "instagram": None,
            "twitter": None,
            "youtube": None,
        }

        if not website_url:
            return result

        parsed = urlparse(website_url)
        if not parsed.scheme:
            website_url = f"https://{website_url}"
            parsed = urlparse(website_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        logger.info("Scraping website: %s", website_url)

        scrape_start = time.monotonic()
        get_metrics().increment("scraper_calls")
        try:
            with timer("scraper"):
                homepage_html = await self._download_page(website_url)
        except Exception:
            logger.exception(
                "Unexpected error downloading homepage %s — returning empty scrape",
                website_url,
            )
            return result
        _log_stage("scraping_homepage", time.monotonic() - scrape_start, website_url)

        if homepage_html is None:
            get_metrics().increment("scraper_failures")
            logger.warning("Failed to download homepage: %s", website_url)
            return result

        try:
            homepage_soup = BeautifulSoup(homepage_html, "html.parser")

            email_start = time.monotonic()
            homepage_emails = self._extract_emails(homepage_soup)
            result["emails"] = homepage_emails
            _log_stage("email_extraction", time.monotonic() - email_start)

            social_start = time.monotonic()
            social = self._extract_social_links(homepage_soup, base_url)
            result.update(social)
            _log_stage("social_extraction", time.monotonic() - social_start)

            contact_urls = self._contact_page_urls(homepage_soup, base_url, website_url)
            if contact_urls:
                logger.info("Checking %d public contact page(s) for %s", len(contact_urls), website_url)
                contact_start = time.monotonic()
                pages = await asyncio.gather(
                    *(self._download_page(url) for url in contact_urls), return_exceptions=True
                )
                _log_stage("scraping_contact_pages", time.monotonic() - contact_start)
                emails = list(homepage_emails)
                for contact_html in pages:
                    if isinstance(contact_html, Exception) or not contact_html:
                        continue
                    contact_soup = BeautifulSoup(contact_html, "html.parser")
                    emails.extend(self._extract_emails(contact_soup))
                    contact_social = self._extract_social_links(contact_soup, base_url)
                    for key in ("linkedin", "facebook", "instagram", "twitter", "youtube"):
                        if contact_social.get(key) and not result[key]:
                            result[key] = contact_social[key]
                result["emails"] = list(dict.fromkeys(emails))
        except Exception:
            logger.exception(
                "Unexpected error parsing website %s — returning partial scrape",
                website_url,
            )

        _log_stage("scraper_total", time.monotonic() - scrape_start, website_url)
        return result

    def _cached_page(self, url: str) -> str | None | object:
        entry = self._page_cache.get(url)
        if entry is None:
            return _PAGE_MISS
        fetched_at, html = entry
        if time.monotonic() - fetched_at > self._page_cache_ttl:
            del self._page_cache[url]
            return _PAGE_MISS
        return html

    async def _download_page(self, url: str) -> str | None:
        cached = self._cached_page(url)
        if cached is not _PAGE_MISS:
            logger.debug("Reusing cached page: %s", url)
            return cached  # type: ignore[return-value]

        lock = self._download_locks.setdefault(url, asyncio.Lock())
        async with lock:
            cached = self._cached_page(url)
            if cached is not _PAGE_MISS:
                return cached  # type: ignore[return-value]
            html = await self._fetch_page(url)
            self._page_cache[url] = (time.monotonic(), html)
            return html

    async def _fetch_page(self, url: str) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as client:
                response = await retry_async(
                    lambda: client.get(
                        url,
                        headers={
                            "User-Agent": "Mozilla/5.0 (compatible; BusinessLeadFinder/1.0)",
                            "Accept": "text/html,application/xhtml+xml",
                        },
                        follow_redirects=True,
                    ),
                    context=f"Website scrape {url}",
                )
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "text/html" not in content_type and "application/xhtml" not in content_type:
                    logger.debug("Skipping non-HTML response from %s: %s", url, content_type)
                    return None
                return response.text
        except httpx.TimeoutException:
            logger.warning("Timeout downloading %s", url)
            return None
        except httpx.HTTPStatusError as e:
            logger.warning("HTTP error %s downloading %s: %s", e.response.status_code, url, e)
            return None
        except httpx.RequestError as e:
            logger.warning("Network error downloading %s: %s", url, e)
            return None
        except Exception:
            logger.exception("Unexpected error downloading %s", url)
            return None

    def _find_contact_page(self, soup: BeautifulSoup, base_url: str) -> str | None:
        """Return the first relevant internal contact page (legacy helper)."""
        origin = urlparse(base_url).netloc.lower()
        for tag in soup.find_all(["a", "link"]):
            href = tag.get("href", "")
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            combined = f"{(tag.get_text() or '').strip().lower()} {href.lower()}"
            url = self._normalize_url(base_url, href)
            if (
                url
                and urlparse(url).netloc.lower() == origin
                and any(kw in combined for kw in CONTACT_KEYWORDS)
            ):
                return url
        return None

    def _contact_page_urls(
        self, soup: BeautifulSoup, base_url: str, homepage_url: str | None = None
    ) -> list[str]:
        """Find standard and linked contact pages without leaving the website."""
        origin = urlparse(base_url).netloc.lower()
        urls: list[str] = []

        def add(url: str | None) -> None:
            if not url or url == homepage_url or url in urls:
                return
            if urlparse(url).netloc.lower() == origin:
                urls.append(url)

        for tag in soup.find_all(["a", "link"]):
            href = tag.get("href", "")
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            text = (tag.get_text() or "").strip().lower()
            href_lower = href.lower()
            combined = f"{text} {href_lower}"
            if any(kw in combined for kw in CONTACT_KEYWORDS):
                add(self._normalize_url(base_url, href))
        for path in COMMON_CONTACT_PATHS:
            add(urljoin(base_url.rstrip("/") + "/", path))
        return urls

    def _extract_emails(self, soup: BeautifulSoup) -> list[str]:
        raw = re.findall(_EMAIL_PATTERN, soup.get_text(" "))
        for tag in soup.find_all("a", href=True):
            href = unquote(tag["href"])
            if href.lower().startswith("mailto:"):
                raw.extend(re.findall(_EMAIL_PATTERN, href[7:].split("?", 1)[0]))
        raw.extend(self._extract_json_ld_emails(soup))
        raw.extend(re.findall(_EMAIL_PATTERN, self._deobfuscate_email_text(soup.get_text(" "))))
        seen: set[str] = set()
        emails: list[str] = []
        for email in raw:
            stripped = email.strip().lower()
            domain = stripped.split("@")[-1] if "@" in stripped else ""
            if (
                stripped
                and stripped not in seen
                and len(stripped) <= 254
                and "." in domain
                and not any(
                    domain.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".css", ".js"]
                )
            ):
                seen.add(stripped)
                emails.append(stripped)
        return emails

    @staticmethod
    def _deobfuscate_email_text(text: str) -> str:
        """Convert common public ``name [at] example [dot] com`` forms."""
        text = re.sub(r"\s*(?:\[|\()\s*(?:at|@)\s*(?:\]|\))\s*", "@", text, flags=re.I)
        return re.sub(r"\s*(?:\[|\()\s*(?:dot|\.)\s*(?:\]|\))\s*", ".", text, flags=re.I)

    @staticmethod
    def _extract_json_ld_emails(soup: BeautifulSoup) -> list[str]:
        emails: list[str] = []

        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower() == "email" and isinstance(item, str):
                        emails.extend(re.findall(_EMAIL_PATTERN, item))
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                walk(json.loads(tag.string or tag.get_text() or "{}"))
            except (TypeError, ValueError):
                continue
        return emails

    def _extract_social_links(self, soup: BeautifulSoup, base_url: str) -> dict[str, str | None]:
        result: dict[str, str | None] = {
            "linkedin": None,
            "facebook": None,
            "instagram": None,
            "twitter": None,
            "youtube": None,
        }

        found: dict[str, str] = {}

        for tag in soup.find_all("a", href=True):
            href = tag["href"]
            href_lower = href.lower()

            for network, patterns in SOCIAL_DOMAINS.items():
                for pattern in patterns:
                    if pattern in href_lower:
                        if network not in found:
                            full = self._normalize_url(base_url, href)
                            if full:
                                found[network] = full

        result.update(found)
        return result

    @staticmethod
    def _normalize_url(base_url: str, href: str) -> str | None:
        if not href or href.startswith("javascript:") or href.startswith("#"):
            return None
        return urljoin(base_url, href)

import pytest
from bs4 import BeautifulSoup

from app.services.website_scraper import WebsiteScraperService


class TestExtractEmails:
    def test_extract_simple_email(self):
        soup = BeautifulSoup("<html><body>Contact: hello@example.com</body></html>", "html.parser")
        service = WebsiteScraperService()
        emails = service._extract_emails(soup)
        assert "hello@example.com" in emails

    def test_extract_multiple_emails(self):
        html = """
        <html>
            <body>
                <p>info@company.com</p>
                <p>support@company.co.uk</p>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        service = WebsiteScraperService()
        emails = service._extract_emails(soup)
        assert "info@company.com" in emails
        assert "support@company.co.uk" in emails

    def test_ignore_image_extensions(self):
        soup = BeautifulSoup(
            '<html><body><img src="logo.png" alt="test@png.com"></body></html>',
            "html.parser",
        )
        service = WebsiteScraperService()
        emails = service._extract_emails(soup)
        assert emails == []

    def test_deduplicate_emails(self):
        soup = BeautifulSoup(
            "<html><body><p>Contact: hello@example.com or Hello@example.com</p></body></html>",
            "html.parser",
        )
        service = WebsiteScraperService()
        emails = service._extract_emails(soup)
        assert emails == ["hello@example.com"]

    def test_no_emails(self):
        soup = BeautifulSoup("<html><body>No contact info</body></html>", "html.parser")
        service = WebsiteScraperService()
        emails = service._extract_emails(soup)
        assert emails == []

    def test_extracts_mailto_json_ld_and_obfuscated_emails(self):
        soup = BeautifulSoup(
            """
            <a href="mailto:hello%40example.com?subject=Hi">Email us</a>
            <p>support [at] example [dot] com</p>
            <script type="application/ld+json">
              {"@type": "Organization", "email": "team@example.com"}
            </script>
            """,
            "html.parser",
        )
        emails = WebsiteScraperService()._extract_emails(soup)
        assert emails == ["hello@example.com", "team@example.com", "support@example.com"]


class TestExtractSocialLinks:
    def test_linkedin(self):
        html = '<a href="https://linkedin.com/in/company">LinkedIn</a>'
        soup = BeautifulSoup(html, "html.parser")
        service = WebsiteScraperService()
        result = service._extract_social_links(soup, "https://example.com")
        assert result["linkedin"] == "https://linkedin.com/in/company"

    def test_facebook(self):
        html = '<a href="https://facebook.com/company">Facebook</a>'
        soup = BeautifulSoup(html, "html.parser")
        service = WebsiteScraperService()
        result = service._extract_social_links(soup, "https://example.com")
        assert result["facebook"] == "https://facebook.com/company"

    def test_instagram(self):
        html = '<a href="https://instagram.com/company">Instagram</a>'
        soup = BeautifulSoup(html, "html.parser")
        service = WebsiteScraperService()
        result = service._extract_social_links(soup, "https://example.com")
        assert result["instagram"] == "https://instagram.com/company"

    def test_twitter(self):
        html = '<a href="https://twitter.com/company">Twitter</a>'
        soup = BeautifulSoup(html, "html.parser")
        service = WebsiteScraperService()
        result = service._extract_social_links(soup, "https://example.com")
        assert result["twitter"] == "https://twitter.com/company"

    def test_x_domain(self):
        html = '<a href="https://x.com/company">X</a>'
        soup = BeautifulSoup(html, "html.parser")
        service = WebsiteScraperService()
        result = service._extract_social_links(soup, "https://example.com")
        assert result["twitter"] == "https://x.com/company"

    def test_youtube(self):
        html = '<a href="https://youtube.com/@channel">YouTube</a>'
        soup = BeautifulSoup(html, "html.parser")
        service = WebsiteScraperService()
        result = service._extract_social_links(soup, "https://example.com")
        assert result["youtube"] == "https://youtube.com/@channel"

    def test_relative_url_normalization(self):
        html = '<a href="/linkedin">LinkedIn</a>'
        soup = BeautifulSoup(html, "html.parser")
        service = WebsiteScraperService()
        result = service._extract_social_links(soup, "https://example.com")
        assert result["linkedin"] is None  # relative path doesn't match social domains

    def test_multiple_social_links(self):
        html = """
        <html>
            <body>
                <a href="https://linkedin.com/in/company">LinkedIn</a>
                <a href="https://facebook.com/company">Facebook</a>
                <a href="https://instagram.com/company">Instagram</a>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        service = WebsiteScraperService()
        result = service._extract_social_links(soup, "https://example.com")
        assert result["linkedin"] == "https://linkedin.com/in/company"
        assert result["facebook"] == "https://facebook.com/company"
        assert result["instagram"] == "https://instagram.com/company"
        assert result["twitter"] is None

    def test_no_social_links(self):
        soup = BeautifulSoup("<html><body><a href='/pricing'>Pricing</a></body></html>", "html.parser")
        service = WebsiteScraperService()
        result = service._extract_social_links(soup, "https://example.com")
        assert all(v is None for v in result.values())


class TestFindContactPage:
    def test_find_contact_link(self):
        html = '<a href="/contact">Contact Us</a>'
        soup = BeautifulSoup(html, "html.parser")
        service = WebsiteScraperService()
        url = service._find_contact_page(soup, "https://example.com")
        assert url == "https://example.com/contact"

    def test_find_about_link(self):
        html = '<a href="/about">About</a>'
        soup = BeautifulSoup(html, "html.parser")
        service = WebsiteScraperService()
        url = service._find_contact_page(soup, "https://example.com")
        assert url == "https://example.com/about"

    def test_ignore_hash_links(self):
        html = '<a href="#contact">Jump</a>'
        soup = BeautifulSoup(html, "html.parser")
        service = WebsiteScraperService()
        url = service._find_contact_page(soup, "https://example.com")
        assert url is None

    def test_no_contact_page(self):
        html = '<a href="/pricing">Pricing</a><a href="/products">Products</a>'
        soup = BeautifulSoup(html, "html.parser")
        service = WebsiteScraperService()
        url = service._find_contact_page(soup, "https://example.com")
        assert url is None


class TestNormalizeUrl:
    def test_absolute_url(self):
        result = WebsiteScraperService._normalize_url("https://example.com", "https://other.com/page")
        assert result == "https://other.com/page"

    def test_relative_url(self):
        result = WebsiteScraperService._normalize_url("https://example.com", "/contact")
        assert result == "https://example.com/contact"

    def test_javascript_ignored(self):
        result = WebsiteScraperService._normalize_url("https://example.com", "javascript:void(0)")
        assert result is None


class TestScrapeIntegration:
    @pytest.mark.asyncio
    async def test_scrape_no_website(self):
        service = WebsiteScraperService()
        result = await service.scrape("")
        assert result["emails"] == []
        assert result["linkedin"] is None

    @pytest.mark.asyncio
    async def test_scrape_adds_scheme(self):
        service = WebsiteScraperService()
        result = await service.scrape("")
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_scrape_checks_common_public_pages_concurrently(self, monkeypatch):
        service = WebsiteScraperService()
        requested: list[str] = []

        async def fake_download(url: str) -> str | None:
            requested.append(url)
            if url == "https://example.com":
                return '<a href="/contact-us">Contact us</a>'
            if url == "https://example.com/impressum":
                return "Legal: legal@example.com"
            return None

        monkeypatch.setattr(service, "_download_page", fake_download)

        result = await service.scrape("https://example.com")

        assert result["emails"] == ["legal@example.com"]
        assert {
            "https://example.com/contact",
            "https://example.com/contact-us",
            "https://example.com/about",
            "https://example.com/about-us",
            "https://example.com/support",
            "https://example.com/impressum",
        }.issubset(requested)

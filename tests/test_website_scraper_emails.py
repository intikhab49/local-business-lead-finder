import pytest

from app.services.website_scraper import WebsiteScraperService


@pytest.mark.asyncio
async def test_deobfuscated_emails_collected(monkeypatch):
    html = """
    <html><head><title>Test</title></head>
    <body>
      <p>Contact: name [at] example [dot] com</p>
    </body></html>
    """

    service = WebsiteScraperService(timeout=2, page_cache_ttl=1)

    async def fake_download(url):
        return html

    monkeypatch.setattr(service, "_download_page", fake_download)

    result = await service.scrape("https://example.com")
    assert "name@example.com" in result.get("emails", [])


@pytest.mark.asyncio
async def test_json_ld_email_extracted(monkeypatch):
    html = """
    <html><head></head>
    <body>
      <script type="application/ld+json">{"email": "info@acme.com"}</script>
    </body></html>
    """

    service = WebsiteScraperService(timeout=2, page_cache_ttl=1)

    async def fake_download(url):
        return html

    monkeypatch.setattr(service, "_download_page", fake_download)

    result = await service.scrape("https://acme.example.com")
    assert "info@acme.com" in result.get("emails", [])


@pytest.mark.asyncio
async def test_mailto_in_header_footer(monkeypatch):
    html = """
    <html>
      <header><a href="mailto:header@example.com">Email</a></header>
      <footer><a href="mailto:footer@example.org">Footer</a></footer>
    </html>
    """

    service = WebsiteScraperService(timeout=2, page_cache_ttl=1)

    async def fake_download(url):
        return html

    monkeypatch.setattr(service, "_download_page", fake_download)

    result = await service.scrape("https://headers.example.com")
    emails = result.get("emails", [])
    assert "header@example.com" in emails
    assert "footer@example.org" in emails

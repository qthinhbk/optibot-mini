"""
Scraper for OptiSigns Help Center (Zendesk-based).

Fetches articles via the public Zendesk Help Center API,
converts HTML bodies to clean Markdown, and saves each
article as <slug>.md under the ./articles/ directory.

Features:
  - Paginates through all articles automatically.
  - Strips nav, ads, and Zendesk chrome from HTML before conversion.
  - Preserves headings, code blocks, relative links, images, and lists.
  - Generates a manifest.json with article metadata + content hashes
    for delta-detection on subsequent runs.
"""

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://support.optisigns.com"
API_BASE = f"{BASE_URL}/api/v2/help_center"
ARTICLES_DIR = Path("articles")
MANIFEST_FILE = Path("manifest.json")
PER_PAGE = 100  # Zendesk max

# Shared session — avoids brotli decoding issues and reuses connections
_session = requests.Session()
_session.headers.update({
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",
    "User-Agent": "OptiBot-Scraper/1.0",
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTML → Markdown conversion
# ---------------------------------------------------------------------------

def html_to_markdown(html_body: str) -> str:
    """Convert an HTML article body to clean Markdown."""
    if not html_body:
        return ""

    soup = BeautifulSoup(html_body, "html.parser")

    # Remove unwanted elements: nav, header, footer, ads, scripts, styles
    for selector in [
        "nav", "header", "footer", "script", "style", "noscript",
        ".header", ".footer", ".navbar", ".nav", ".advertisement",
        ".ad", ".sidebar", ".breadcrumbs", ".article-votes",
        ".article-subscribe", ".article-share", ".article-attachments",
        ".article-relatives", ".article-footer", ".article-more-questions",
        "[role='navigation']", "[role='banner']", "[role='contentinfo']",
    ]:
        for tag in soup.select(selector):
            tag.decompose()

    # Pre-process: resolve relative URLs and clean Zendesk artifacts
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        # Clean Zendesk escaped quotes
        href = href.replace('\\"', '').replace("%5C%22", "")
        # Resolve relative URLs
        if href.startswith("/"):
            href = urljoin(BASE_URL, href)
        a_tag["href"] = href

    for img_tag in soup.find_all("img", src=True):
        src = img_tag["src"]
        # Skip tiny icons, avatars, and tracking pixels
        skip_patterns = [
            "avatar", "icon", "emoji", "badge",
            "1x1", "pixel", "tracker", "spacer",
        ]
        src_lower = src.lower()
        if any(p in src_lower for p in skip_patterns):
            img_tag.decompose()
            continue
        # Resolve relative URLs
        if src.startswith("/"):
            img_tag["src"] = urljoin(BASE_URL, src)

    # Convert using markdownify
    markdown = md(
        str(soup),
        heading_style="ATX",
        bullets="-",
        strong_em_symbol="**",
        escape_underscores=False,
        escape_asterisks=False,
        wrap=False,
        wrap_width=0,
    )

    # Post-processing cleanup
    markdown = _clean_markdown(markdown)

    return markdown


def _clean_markdown(text: str) -> str:
    """Clean up common Markdown artifacts."""
    # Remove lines that are just escaped newlines
    text = text.replace("\\n", "\n")

    # Collapse 3+ consecutive blank lines into 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Remove trailing whitespace from each line
    text = "\n".join(line.rstrip() for line in text.split("\n"))

    # Remove leading/trailing blank lines
    text = text.strip()

    return text


# ---------------------------------------------------------------------------
# Zendesk API helpers
# ---------------------------------------------------------------------------

def fetch_all_articles() -> list[dict]:
    """Fetch all articles from the Zendesk Help Center API with pagination."""
    articles = []
    url = f"{API_BASE}/en-us/articles.json?per_page={PER_PAGE}"

    while url:
        log.info(f"Fetching: {url}")
        resp = _session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        page_articles = data.get("articles", [])
        articles.extend(page_articles)
        log.info(f"  Got {len(page_articles)} articles (total so far: {len(articles)})")

        url = data.get("next_page")
        if url:
            time.sleep(0.5)  # Be polite to Zendesk

    return articles


# ---------------------------------------------------------------------------
# Slug / filename helpers
# ---------------------------------------------------------------------------

def make_slug(article: dict) -> str:
    """
    Generate a filename-safe slug from the article's html_url or title.
    Prefers the URL slug (last path segment) if available.
    """
    html_url = article.get("html_url", "")
    if html_url:
        path = urlparse(html_url).path
        # Zendesk URLs: /hc/en-us/articles/12345-Article-Title
        segments = [s for s in path.split("/") if s]
        if segments:
            raw_slug = segments[-1]
            return _sanitize_slug(raw_slug)

    # Fallback: use title
    title = article.get("title", f"article-{article['id']}")
    return _sanitize_slug(title)


def _sanitize_slug(text: str) -> str:
    """Make a string safe for use as a filename."""
    # Replace non-alphanumeric chars (except hyphens) with hyphens
    slug = re.sub(r"[^a-zA-Z0-9\-]", "-", text)
    # Collapse multiple hyphens
    slug = re.sub(r"-{2,}", "-", slug)
    # Strip leading/trailing hyphens
    slug = slug.strip("-")
    return slug.lower()


# ---------------------------------------------------------------------------
# Content hash for delta detection
# ---------------------------------------------------------------------------

def content_hash(text: str) -> str:
    """SHA-256 hash of content for change detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Main scraper logic
# ---------------------------------------------------------------------------

def build_frontmatter(article: dict) -> str:
    """Build YAML-like frontmatter for the markdown file."""
    title = article.get("title", "Untitled")
    url = article.get("html_url", "")
    article_id = article.get("id", "")
    updated_at = article.get("updated_at", "")
    created_at = article.get("created_at", "")

    lines = [
        "---",
        f"title: \"{title}\"",
        f"article_id: {article_id}",
        f"url: \"{url}\"",
        f"created_at: \"{created_at}\"",
        f"updated_at: \"{updated_at}\"",
        "---",
        "",
    ]
    return "\n".join(lines)


def scrape_articles(output_dir: Path = ARTICLES_DIR) -> dict:
    """
    Main entry point: fetch all articles, convert to Markdown, save.

    Returns a summary dict with counts:
      { "total": N, "added": N, "updated": N, "skipped": N, "errors": N }
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load existing manifest for delta detection
    old_manifest = {}
    if MANIFEST_FILE.exists():
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            old_manifest = json.load(f)

    # Fetch all articles
    log.info("=" * 60)
    log.info("Starting article scrape from support.optisigns.com")
    log.info("=" * 60)

    articles = fetch_all_articles()
    log.info(f"Total articles fetched from API: {len(articles)}")

    new_manifest = {}
    stats = {"total": len(articles), "added": 0, "updated": 0, "skipped": 0, "errors": 0}

    for i, article in enumerate(articles, 1):
        article_id = str(article["id"])
        title = article.get("title", "Untitled")
        slug = make_slug(article)
        filename = f"{slug}.md"
        filepath = output_dir / filename

        log.info(f"[{i}/{len(articles)}] Processing: {title}")

        try:
            # Get HTML body
            html_body = article.get("body", "")
            if not html_body:
                log.warning(f"  Skipping (no body): {title}")
                stats["skipped"] += 1
                continue

            # Convert to Markdown
            markdown_body = html_to_markdown(html_body)
            if not markdown_body.strip():
                log.warning(f"  Skipping (empty after conversion): {title}")
                stats["skipped"] += 1
                continue

            # Build full file content with frontmatter
            frontmatter = build_frontmatter(article)
            full_content = f"# {title}\n\n{markdown_body}\n"
            file_content = frontmatter + full_content

            # Delta detection: compare hash
            new_hash = content_hash(full_content)
            old_entry = old_manifest.get(article_id, {})
            old_hash = old_entry.get("content_hash", "")

            if old_hash == new_hash and filepath.exists():
                log.info(f"  Skipped (unchanged): {filename}")
                stats["skipped"] += 1
            elif old_hash and old_hash != new_hash:
                filepath.write_text(file_content, encoding="utf-8")
                log.info(f"  Updated: {filename}")
                stats["updated"] += 1
            else:
                filepath.write_text(file_content, encoding="utf-8")
                log.info(f"  Added: {filename}")
                stats["added"] += 1

            # Record in new manifest
            new_manifest[article_id] = {
                "title": title,
                "slug": slug,
                "filename": filename,
                "html_url": article.get("html_url", ""),
                "updated_at": article.get("updated_at", ""),
                "content_hash": new_hash,
            }

        except Exception as e:
            log.error(f"  Error processing '{title}': {e}")
            stats["errors"] += 1

    # Save manifest
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(new_manifest, f, indent=2, ensure_ascii=False)
    log.info(f"Manifest saved: {MANIFEST_FILE} ({len(new_manifest)} entries)")

    # Summary
    log.info("=" * 60)
    log.info("Scrape complete!")
    log.info(f"  Total articles: {stats['total']}")
    log.info(f"  Added:          {stats['added']}")
    log.info(f"  Updated:        {stats['updated']}")
    log.info(f"  Skipped:        {stats['skipped']}")
    log.info(f"  Errors:         {stats['errors']}")
    log.info("=" * 60)

    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    stats = scrape_articles()
    print(json.dumps(stats, indent=2))

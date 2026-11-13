#!/usr/bin/env python3
"""Scrape https://temporal.io/blog and generate an RSS 2.0 feed.

- Reads the blog listing page for post URLs + dates.
- Fetches each *new* post once to get its title, description and author.
- Keeps a small JSON cache so posts stay in the feed after they scroll off
  the first page of the blog, and so we don't re-fetch old posts.
- Output is deterministic: if nothing changed, the files are byte-identical,
  so the workflow won't create empty commits.
"""
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://temporal.io"
BLOG_URL = f"{BASE}/blog"
OUT_DIR = Path("docs")
FEED_PATH = OUT_DIR / "temporal-blog.xml"
CACHE_PATH = OUT_DIR / "cache.json"
MAX_ITEMS = 50
HEADERS = {
    "User-Agent": "temporal-blog-rss-bot (+https://github.com/%s)"
    % os.environ.get("GITHUB_REPOSITORY", "your/repo")
}
DATE_RE = re.compile(r"\b([A-Z][a-z]{2}) (\d{2}), (\d{4})\b")
SKIP_SLUGS = {"categories", "feed.xml", "tag", "tags", "author", "authors"}

ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
ATOM = "{http://www.w3.org/2005/Atom}"


def get(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_listing_date(text: str):
    m = DATE_RE.search(text)
    if not m:
        return None
    try:
        dt = datetime.strptime(" ".join(m.groups()), "%b %d %Y")
    except ValueError:
        return None
    # Listing has no time of day; use noon UTC so timezones can't shift the date.
    return dt.replace(hour=12, tzinfo=timezone.utc)


def scrape_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    posts: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(BASE, a["href"]).split("#")[0].split("?")[0].rstrip("/")
        if not url.startswith(BLOG_URL + "/"):
            continue
        slug = url[len(BLOG_URL) + 1:]
        if "/" in slug or slug in SKIP_SLUGS or slug.endswith(".xml"):
            continue
        text = a.get_text(" ", strip=True)
        date = parse_listing_date(text)
        if not date:
            continue  # nav links, "Read post" buttons, etc. have no date
        heading = a.find(re.compile(r"^h[1-6]$"))
        if heading:
            title = heading.get_text(" ", strip=True)
        else:
            chunks = list(a.stripped_strings)
            title = chunks[0] if chunks else slug.replace("-", " ").title()
        posts.setdefault(url, {"url": url, "title": title, "date": date.isoformat()})
    return list(posts.values())


def meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def enrich(post: dict) -> dict:
    """Fetch the article page for a cleaner title, description, author, date."""
    try:
        soup = BeautifulSoup(get(post["url"]), "html.parser")
    except requests.RequestException as exc:
        print(f"  ! could not fetch {post['url']}: {exc}", file=sys.stderr)
        return post
    title = meta(soup, "og:title", "twitter:title")
    if title:
        post["title"] = re.sub(r"\s*\|\s*Temporal\s*$", "", title)
    post["description"] = meta(soup, "og:description", "description") or ""
    author = meta(soup, "author", "article:author")
    if author:
        post["author"] = author
    published = meta(soup, "article:published_time")
    if published:
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            post["date"] = dt.isoformat()
        except ValueError:
            pass
    return post


def load_cache() -> dict[str, dict]:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def feed_self_url() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "owner/repo")
    owner, _, name = repo.partition("/")
    return os.environ.get(
        "FEED_URL", f"https://{owner}.github.io/{name}/{FEED_PATH.name}"
    )


def build_feed(posts: list[dict]) -> bytes:
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Temporal Blog (unofficial feed)"
    ET.SubElement(ch, "link").text = BLOG_URL
    ET.SubElement(ch, "description").text = (
        "Articles from temporal.io/blog, scraped by a GitHub Actions workflow."
    )
    ET.SubElement(ch, "language").text = "en"
    ET.SubElement(
        ch,
        f"{ATOM}link",
        href=feed_self_url(),
        rel="self",
        type="application/rss+xml",
    )
    if posts:
        newest = datetime.fromisoformat(posts[0]["date"])
        ET.SubElement(ch, "lastBuildDate").text = format_datetime(newest)

    for p in posts:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = p["title"]
        ET.SubElement(item, "link").text = p["url"]
        ET.SubElement(item, "guid", isPermaLink="true").text = p["url"]
        ET.SubElement(item, "pubDate").text = format_datetime(
            datetime.fromisoformat(p["date"])
        )
        if p.get("description"):
            ET.SubElement(item, "description").text = p["description"]
        if p.get("author"):
            ET.SubElement(item, "author").text = p["author"]

    ET.indent(rss)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n"


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / ".nojekyll").touch()  # serve files as-is; skip the Jekyll build
    cache = load_cache()

    listing = scrape_listing(get(BLOG_URL))
    if not listing:
        print("No posts found on listing page; markup may have changed.", file=sys.stderr)
        return 1
    print(f"Found {len(listing)} posts on the listing page.")

    for post in listing:
        if post["url"] in cache:
            continue
        print(f"New post: {post['url']}")
        cache[post["url"]] = enrich(post)
        time.sleep(1)  # be polite

    posts = sorted(cache.values(), key=lambda p: p["date"], reverse=True)[:MAX_ITEMS]
    cache = {p["url"]: p for p in posts}

    CACHE_PATH.write_text(
        json.dumps(cache, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    FEED_PATH.write_bytes(build_feed(posts))
    print(f"Wrote {FEED_PATH} with {len(posts)} items.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

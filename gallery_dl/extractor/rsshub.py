# -*- coding: utf-8 -*-

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Extractor for RSS/Atom feeds produced by RSSHub (https://rsshub.app/).

Works with any RSSHub instance — the official one at rsshub.app, a
self-hosted deployment, or a local mirror. Parses RSS 2.0 and Atom 1.0
feeds and yields image URLs found in each item.

Example:
    gallery-dl "https://rsshub.app/jandan/pic"
    gallery-dl "https://rsshub.app/jandan/pic?limit=200"
"""

import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

from .common import Extractor, Message
from .. import text


NS_MEDIA = "http://search.yahoo.com/mrss"
NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"
NS_ATOM = "http://www.w3.org/2005/Atom"
NS_ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"

_FS_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]+')

_IMAGE_EXT = re.compile(
    r"\.(?:jpe?g|jpe|png|gif|webp|bmp|avif|heic|svg|ico|tiff?)"
    r"(?:[?#]|$)",
    re.IGNORECASE,
)

_IMG_SRC = re.compile(
    r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"'>\s]+)["']""",
    re.IGNORECASE,
)


def _parse_rfc2822(s):
    """Parse an RFC 2822 date string (RSS 2.0 pubDate) to naive UTC datetime."""
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


class RsshubFeedExtractor(Extractor):
    """Extractor for RSS/Atom feeds from any RSSHub instance."""
    category = "rsshub"
    subcategory = "feed"
    directory_fmt = ("{feed_path1}", "{feed_path2a}")
    filename_fmt = "{filename}.{extension}"
    archive_fmt = "{feed_path1}/{feed_path2a}{feed_path2b:?/}/{item_id}-{num}"

    pattern = (r"(?i)(?P<rsshub>rsshub:)?"
               r"https?://[^/?#]+/\S+")
    example = "https://rsshub.app/jandan/pic"

    def __init__(self, match):
        Extractor.__init__(self, match)
        if match["rsshub"]:
            self.url = self.url[len("rsshub:"):]
        parsed = urlparse(self.url)
        self.root = f"{parsed.scheme}://{parsed.netloc}"
        self.feed_domain = parsed.netloc.replace(":", "-")
        self.feed_path = (parsed.path.strip("/").replace("/", "-")
                          or "feed")
        parts = [p for p in parsed.path.split("/") if p]
        self.feed_path1 = parts[0] if parts else "feed"
        self.feed_path2a = parts[1] if len(parts) > 1 else ""
        self.feed_path2b = parts[2] if len(parts) > 2 else ""

    def items(self):
        response = self.request(self.url)
        ctype = response.headers.get("content-type", "").lower()
        if not any(t in ctype for t in ("xml", "rss", "atom")):
            self.log.debug(
                "Skipping non-XML response (%s): %s", ctype, self.url)
            return

        try:
            root_elem = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            self.log.warning(
                "Invalid XML feed from %s: %s", self.url, exc)
            return

        tag = root_elem.tag
        if tag == "rss" or tag.endswith("}rss"):
            yield from self._parse_rss(root_elem)
        elif tag == "feed" or tag.endswith("}feed"):
            yield from self._parse_atom(root_elem)
        else:
            self.log.debug("Unsupported feed root: %s", tag)

    def _parse_rss(self, root):
        channel = root.find("channel")
        if channel is None:
            return
        feed_title = text.unescape(channel.findtext("title") or "")
        items = channel.findall("item")
        total = len(items)
        for idx, item in enumerate(items, 1):
            yield from self._item_rss(item, idx, total, feed_title)

    def _parse_atom(self, root):
        feed_title = text.unescape(
            root.findtext(f"{{{NS_ATOM}}}title") or "")
        items = root.findall(f"{{{NS_ATOM}}}entry")
        total = len(items)
        for idx, item in enumerate(items, 1):
            yield from self._item_atom(item, idx, total, feed_title)

    def _item_rss(self, item, idx, total, feed_title):
        title = text.unescape(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        pubdate = (item.findtext("pubDate") or "").strip()
        author = (item.findtext("author") or "").strip()
        description = item.findtext("description") or ""
        content_encoded = item.findtext(
            f"{{{NS_CONTENT}}}encoded") or ""

        images = self._extract_images(
            item, description, content_encoded)
        if not images:
            return

        data = self._base_data(
            feed_title, title, idx, total,
            guid or link or f"item-{idx}", link, author,
            _parse_rfc2822(pubdate))
        yield Message.Directory, "", data
        yield from self._emit_images(data, images)

    def _item_atom(self, item, idx, total, feed_title):
        title = text.unescape(
            item.findtext(f"{{{NS_ATOM}}}title") or "")
        link_elem = item.find(f"{{{NS_ATOM}}}link")
        link = link_elem.get("href", "") if link_elem is not None else ""
        entry_id = (item.findtext(f"{{{NS_ATOM}}}id") or "").strip()
        author = ""
        author_elem = item.find(f"{{{NS_ATOM}}}author")
        if author_elem is not None:
            author = text.unescape(
                author_elem.findtext(f"{{{NS_ATOM}}}name") or "")
        published = (item.findtext(f"{{{NS_ATOM}}}published")
                     or item.findtext(f"{{{NS_ATOM}}}updated") or "")
        summary = (item.findtext(f"{{{NS_ATOM}}}summary")
                   or item.findtext(f"{{{NS_ATOM}}}content") or "")

        images = self._extract_images(item, summary, "")
        if not images:
            return

        date = None
        if published:
            d = self.parse_datetime_iso(published)
            if d:
                date = d

        data = self._base_data(
            feed_title, title, idx, total,
            entry_id or link or f"item-{idx}", link, author, date)
        yield Message.Directory, "", data
        yield from self._emit_images(data, images)

    def _base_data(self, feed_title, title, idx, total,
                   item_id, link, author, date):
        data = {
            "category":   self.category,
            "subcategory": self.subcategory,
            "feed_domain": self.feed_domain,
            "feed_path":  self.feed_path,
            "feed_path1": self.feed_path1,
            "feed_path2a": self.feed_path2a,
            "feed_path2b": self.feed_path2b,
            "feed_title": feed_title,
            "item_title": self._sanitize_title(title),
            "item_index": idx,
            "item_total": total,
            "item_id":    item_id,
            "item_link":  link,
            "item_author": author,
        }
        if date is not None:
            data["date"] = date
        return data

    def _emit_images(self, base, images):
        for num, url in enumerate(images, 1):
            data = dict(base)
            data["num"] = num
            data["image_count"] = len(images)
            data["imageurl"] = url
            text.nameext_from_url(url, data)
            yield Message.Url, url, data

    def _extract_images(self, item, description, content_encoded):
        """Extract image URLs from an item, deduplicated, in priority order."""
        seen = set()
        images = []

        def add(url):
            if not url or url in seen:
                return False
            seen.add(url)
            images.append(url)
            return True

        # 1. <enclosure type="image/...">
        for enc in item.iter("enclosure"):
            url = enc.get("url")
            ctype = enc.get("type", "")
            if url and ctype.lower().startswith("image/"):
                add(url)

        # 2. <media:content> medium=image
        for mc in item.iter(f"{{{NS_MEDIA}}}content"):
            url = mc.get("url")
            if not url:
                continue
            medium = (mc.get("medium") or "").lower()
            expression = (mc.get("expression") or "").lower()
            if medium and medium != "image":
                continue
            if expression == "sample":
                continue
            add(url)

        # 3. <media:thumbnail>
        for mt in item.iter(f"{{{NS_MEDIA}}}thumbnail"):
            url = mt.get("url")
            if url:
                add(url)

        # 4. <itunes:image>
        for it in item.iter(f"{{{NS_ITUNES}}}image"):
            url = it.get("href")
            if url:
                add(url)

        # 5. <img src="..."> in description / content:encoded
        for html in (description, content_encoded):
            if not html:
                continue
            for url in _IMG_SRC.findall(html):
                if not _IMAGE_EXT.search(url):
                    continue
                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("/"):
                    url = self.root + url
                elif not url.startswith(("http:", "https:")):
                    continue
                add(url)

        return images

    @staticmethod
    def _sanitize_title(title):
        title = _FS_BAD.sub("-", title).strip(" -.")
        return title[:200] or "untitled"

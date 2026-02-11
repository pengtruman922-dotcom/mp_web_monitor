"""Cross-domain content filtering: skip items linking to external websites."""

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Two-level TLD suffixes common in Chinese domains
_TWO_LEVEL_SUFFIXES = (
    ".gov.cn", ".com.cn", ".org.cn", ".edu.cn", ".net.cn",
    ".ac.cn", ".mil.cn",
)


def extract_root_domain(url: str) -> str:
    """Extract the root domain from a URL.

    Examples:
        www.nea.gov.cn          -> nea.gov.cn
        zfxxgk.nea.gov.cn       -> nea.gov.cn
        www.xinhuanet.com       -> xinhuanet.com
        news.people.com.cn      -> people.com.cn
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
    except Exception:
        return ""

    if not hostname:
        return ""

    # Remove www. prefix
    if hostname.startswith("www."):
        hostname = hostname[4:]

    # Check for two-level suffixes (e.g., .gov.cn, .com.cn)
    for suffix in _TWO_LEVEL_SUFFIXES:
        if hostname.endswith(suffix):
            # hostname = "zfxxgk.nea.gov.cn", suffix = ".gov.cn"
            # prefix = "zfxxgk.nea"
            prefix = hostname[: -len(suffix)]
            parts = prefix.rsplit(".", 1)
            # Take last part before suffix: "nea" + ".gov.cn" -> "nea.gov.cn"
            return parts[-1] + suffix

    # Normal TLD: take last two segments
    parts = hostname.rsplit(".", 2)
    if len(parts) >= 2:
        return ".".join(parts[-2:])

    return hostname


def is_same_domain(item_url: str, source_url: str) -> bool:
    """Return True if item_url and source_url share the same root domain."""
    if not item_url or not source_url:
        return True  # safe fallback: keep the item
    return extract_root_domain(item_url) == extract_root_domain(source_url)


def filter_by_domain(
    items: list[dict],
    source_url: str,
    url_key: str = "url",
) -> list[dict]:
    """Return only items whose URL shares the same root domain as source_url.

    Items without a URL are kept (safe fallback).
    If source_url is empty, all items are returned unchanged.
    """
    if not source_url:
        return items

    result = []
    skipped = 0
    for item in items:
        url = item.get(url_key, "")
        if not url or is_same_domain(url, source_url):
            result.append(item)
        else:
            skipped += 1

    if skipped:
        logger.info(
            "Domain filter: kept %d, skipped %d cross-domain items (source=%s)",
            len(result), skipped, extract_root_domain(source_url),
        )
    return result

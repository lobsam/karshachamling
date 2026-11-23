import logging
import re
import unicodedata

from app import http
from app.config import Settings

log = logging.getLogger(__name__)

_ALLOWED_TAGS = {
    "p", "br", "hr", "h2", "h3", "h4", "strong", "b", "em", "i", "u", "s", "blockquote",
    "ul", "ol", "li", "a", "img", "figure", "figcaption", "code", "pre",
    "table", "thead", "tbody", "tr", "th", "td",
}  # fmt: skip
_ALLOWED_ATTRS = {
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "title", "width", "height"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
}


def clean_html(html: str) -> str:
    """Strip anything that is not plain article markup (scripts, styles, classes...)."""
    if not html:
        return ""
    import nh3  # lazy: nh3 needs randomness, which Workers forbid at startup

    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto", "tel"},
        link_rel="noopener noreferrer",
    )


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:180] or "item"


async def trigger_site_rebuild(settings: Settings) -> None:
    """Ask Cloudflare Pages to rebuild the public site (no-op when not configured)."""
    if not settings.cf_pages_deploy_hook:
        return
    try:
        r = await http.post(settings.cf_pages_deploy_hook)
        if not r.ok:
            log.error("Cloudflare Pages deploy hook returned %s", r.status)
    except http.HttpError:
        log.exception("Cloudflare Pages deploy hook failed")

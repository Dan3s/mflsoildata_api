"""AGROVOC keyword lookup (FAO's agricultural multilingual thesaurus).

The data management team asked that dataset keywords be aligned to a thesaurus,
preferably AGROVOC. FAO publishes a free Skosmos REST API with no key required.

Proxied through our API rather than called from the browser on purpose:
  - the viewer's keystrokes never reach a third party directly;
  - CORS and FAO availability stay our problem, not the page's;
  - results are cached, so repeated prefixes cost nothing.

Failure is always soft: if FAO is slow or down the field simply offers no
suggestions and the user types a keyword by hand, exactly as before.
"""

import logging
from functools import lru_cache
from typing import Dict, List

import httpx

logger = logging.getLogger(__name__)

SEARCH_URL = "https://agrovoc.fao.org/browse/rest/v1/search"
TIMEOUT_SECONDS = 6.0


@lru_cache(maxsize=512)
def _search_cached(query: str, lang: str, limit: int) -> tuple:
    response = httpx.get(
        SEARCH_URL,
        params={
            # Trailing "*" makes it a prefix search, which is what a
            # type-ahead needs; without it only exact labels match.
            "query": f"{query}*",
            "lang": lang,
            "vocab": "agrovoc",
            "maxhits": limit,
        },
        headers={"Accept": "application/json"},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    seen: Dict[str, str] = {}
    for item in response.json().get("results", []):
        label = (item.get("prefLabel") or "").strip()
        # AGROVOC can return the same label for several concepts (e.g. a plant
        # under two schemes); keep the first URI for each distinct label.
        if label and label not in seen:
            seen[label] = item.get("uri") or ""
    return tuple(seen.items())


def search(query: str, lang: str = "en", limit: int = 10) -> List[dict]:
    """Concept labels matching ``query``. Returns [] on any failure."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    lang = lang if lang in {"en", "es"} else "en"
    try:
        pairs = _search_cached(query, lang, limit)
    except Exception as exc:  # network, timeout, malformed payload
        logger.warning("AGROVOC lookup failed for %r (%s): %s", query, lang, exc)
        return []
    return [{"label": label, "uri": uri} for label, uri in pairs]

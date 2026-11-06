"""The only FOMO URL builder.

Coin and profile paths are the routes published by fomo.family
(tokens/:chain/:tokenAddress and profile/:handle). No query string is added.
Unknown chains resolve to the home page. Shortened and redirect hosts are not used.
"""

from urllib.parse import quote

FOMO_HOME = "https://fomo.family"

# Slugs from the fomo.family chain map. Anything else is unknown.
_CHAINS = {
    "solana": "solana",
    "base": "base",
    "monad": "monad",
    "bnb": "bnb",
    "bsc": "bnb",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "hyperliquid": "hyperliquid",
    "robinhood": "robinhood",
    "arc": "arc",
    "elysium": "elysium",
}


def _clean_segment(value: str) -> str:
    text = (value or "").strip()
    return quote(text, safe="")


def coin_url(mint: str, chain: str) -> str:
    """FOMO coin page. Unknown chain -> FOMO_HOME. Never adds parameters."""
    slug = _CHAINS.get((chain or "").strip().lower())
    if not slug or not (mint or "").strip():
        return FOMO_HOME
    return f"{FOMO_HOME}/tokens/{slug}/{_clean_segment(mint.strip())}"


def profile_url(handle: str) -> str:
    """Account tab profile. Empty handle -> FOMO_HOME."""
    text = (handle or "").strip().lstrip("@")
    if not text:
        return FOMO_HOME
    return f"{FOMO_HOME}/profile/{_clean_segment(text)}"

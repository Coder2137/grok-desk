import pathlib

from links import FOMO_HOME, coin_url, profile_url

ROOT = pathlib.Path(__file__).resolve().parents[1]
BANNED = ("utm_", "ref=", "referral", "affiliate", "onelink", "bit.ly", "t.co", "tinyurl")


def test_coin_and_profile_have_no_parameters():
    mint = "So11111111111111111111111111111111111111112"
    urls = [
        coin_url(mint, "solana"),
        coin_url(mint, "base"),
        coin_url(mint, "bnb"),
        profile_url("trader"),
        FOMO_HOME,
    ]
    assert urls[0] == f"{FOMO_HOME}/tokens/solana/{mint}"
    assert urls[3] == f"{FOMO_HOME}/profile/trader"
    for url in urls:
        assert url.startswith("https://fomo.family/") or url == FOMO_HOME
        assert "?" not in url
        assert "#" not in url
        lowered = url.lower()
        for banned in BANNED:
            assert banned not in lowered


def test_unknown_chain_is_home():
    assert coin_url("Mint", "bitcoin") == FOMO_HOME
    assert coin_url("Mint", "") == FOMO_HOME
    assert coin_url("", "solana") == FOMO_HOME
    assert profile_url("") == FOMO_HOME


def test_buy_path_is_solana_coin_page():
    url = coin_url("Mint", "solana")
    assert "/tokens/solana/" in url
    assert "fomo.family" in url
    assert "onelink" not in url


def test_web_does_not_build_fomo_urls():
    for path in (ROOT / "web").rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        assert "fomo.family" not in text
        assert "onelink" not in text
        assert "utm_" not in text

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_phone_lists_stay_paged_and_the_default_theme_stays_linked():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert script.index("Desk 10k") < script.index("Desk 20k") < script.index("Desk 40k")
    assert "Show more" in script
    assert "data-more" in script
    assert 'class="desks' in script or "desksHtml" in script
    assert "themes/" not in page
    assert 'href="app.css"' in page
    assert ".more" in css
    for name in ("a.css", "b.css", "c.css"):
        theme = (ROOT / "web" / "themes" / name).read_text(encoding="utf-8")
        lowered = theme.lower()
        assert "http://" not in lowered
        assert "https://" not in lowered
        assert "fonts.google" not in lowered
        assert "@import" not in lowered


def test_activity_and_coins_are_capped_in_the_renderer():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "state.more.activity" in script
    assert "state.more.coins" in script
    assert "rows.slice(0, state.more.activity)" in script
    assert ".slice(0, 8)" not in script

from wayback_tool.css_processor import (
    extract_css_import_urls,
    extract_css_urls,
)


def test_imports_and_assets_are_classified_separately() -> None:
    css = (
        '@import "theme.css";'
        '@font-face { src: url("./font.woff2"); }'
        'body { background: url("/hero.webp"); }'
    )

    assert extract_css_import_urls(css) == {"theme.css"}
    assert extract_css_urls(css) == [
        "theme.css",
        "./font.woff2",
        "/hero.webp",
    ]

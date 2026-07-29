from wayback_tool.sitemap import content_template_key


def test_repeatable_content_routes_share_a_template() -> None:
    assert (
        content_template_key("https://example.com/blog/first")
        == content_template_key("https://example.com/blog/second")
        == "example.com/blog/:detail"
    )


def test_regular_pages_are_not_grouped() -> None:
    assert content_template_key("https://example.com/about") is None

from wayback_tool.path_mapper import (
    extract_wayback_timestamp,
    strip_wayback_prefix,
    url_to_local_rel,
)


def test_wayback_url_parts_are_preserved() -> None:
    archived = (
        "https://web.archive.org/web/20240228203415id_/"
        "https://example.com/about"
    )

    assert extract_wayback_timestamp(archived) == "20240228203415"
    assert strip_wayback_prefix(archived) == "https://example.com/about"


def test_url_maps_to_host_scoped_html_path() -> None:
    assert (
        url_to_local_rel("https://example.com/about")
        == "example.com/about/index.html"
    )

from wayback_tool.cli import parse_options


def test_cli_returns_typed_options() -> None:
    options = parse_options(
        [
            "--url",
            "https://example.com",
            "--workers",
            "4",
            "--max-pages",
            "25",
        ]
    )

    assert options.url == "https://example.com"
    assert options.workers == 4
    assert options.max_pages == 25
    assert options.max_per_template == 1

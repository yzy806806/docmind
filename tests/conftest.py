"""Pytest configuration for docmind tests."""

import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "skipif_not_redis: skip test if fakeredis is not available",
    )


def pytest_collection_modifyitems(config, items):
    """Skip tests marked with skipif_not_redis if fakeredis is not available."""
    try:
        import fakeredis  # noqa: F401
        skip_redis = False
    except ImportError:
        skip_redis = True

    if skip_redis:
        skip_marker = pytest.mark.skip(reason="fakeredis not installed")
        for item in items:
            if item.get_closest_marker("skipif_not_redis"):
                item.add_marker(skip_marker)

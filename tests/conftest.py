"""Shared test fixtures.

The EasyEDA client throttles itself to one request per 12 seconds, which
is correct in production and intolerable in a test suite — a handful of
tests that reach the fetcher turn a sub-second run into half a minute, and
a slow suite is a suite people stop running.

Every test gets the delay and retry backoff zeroed and the limiter's clock
reset. Tests that exercise the throttling or backoff itself set those
constants back to real values via monkeypatch, which still takes precedence.
"""

import pytest

from kicad_jlcpcb_mcp import part_library


@pytest.fixture(autouse=True)
def _no_easyeda_throttle(monkeypatch):
    monkeypatch.setattr(part_library, "EASYEDA_MIN_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(part_library, "EASYEDA_RETRY_BACKOFF_SECONDS", 0.0)
    part_library._limiter.last_request_time = 0.0
    yield
    part_library._limiter.last_request_time = 0.0

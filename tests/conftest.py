import pytest
import requests

from routing import nominatim


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never reach OSRM or Nominatim; each one mocks the call it expects."""

    def blocked(url, *args, **kwargs):
        raise AssertionError(f"unexpected network call to {url}")

    monkeypatch.setattr(requests, "get", blocked)
    monkeypatch.setattr(nominatim, "MIN_INTERVAL_SECONDS", 0)

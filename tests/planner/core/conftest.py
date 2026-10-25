"""The planner core's unit tests (copied from the design package): the shared fixture.
Builders live in helpers.py."""

from __future__ import annotations

import pytest

from custom_components.solar_ev_planner.core import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(shadow=False)

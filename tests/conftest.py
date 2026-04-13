"""Shared pytest fixtures for task-ninja tests."""

import asyncio
import sys

import pytest


@pytest.fixture(autouse=True)
def _event_loop():
    """Ensure an event loop exists in the main thread.

    Python 3.12+ removed the implicit event loop auto-creation in
    asyncio.get_event_loop(). Tests that call
    asyncio.get_event_loop().run_until_complete() need an explicit loop.
    """
    if sys.version_info >= (3, 12):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            yield loop
            loop.close()
            return
    yield asyncio.get_event_loop()

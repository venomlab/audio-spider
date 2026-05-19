import pytest

from audio_spider.errors import PABackendError
from audio_spider.pa_backend import PABackend


@pytest.fixture
def live_pa():
    """A real PulseAudio backend, fresh per test.

    Function-scoped because PABackend.subscribe() is a one-shot — different
    tests want their own event listener. Skips if PA isn't reachable.
    """
    pa = PABackend(client_name="audio-spider-test")
    try:
        pa.connect()
    except PABackendError as e:
        pytest.skip(f"PulseAudio not available: {e}")
    yield pa
    pa.close()

class AudioSpiderError(Exception):
    """Base class for all audio_spider errors."""


class PABackendError(AudioSpiderError):
    """Failure interacting with PulseAudio."""


class ConfigError(AudioSpiderError):
    """Bad or unreadable config file."""


class ValidationError(AudioSpiderError):
    """User intent rejected (e.g. incompatible ports)."""

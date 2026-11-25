"""session-audit: Analyze Claude session transcripts and outcomes: retros and usage analytics."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("session-audit")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "unknown"

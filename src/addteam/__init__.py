"""addteam: one-command access + onboarding bootstrap."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("addteam")
except PackageNotFoundError:  # running from an uninstalled source tree
    __version__ = "0.0.0+local"

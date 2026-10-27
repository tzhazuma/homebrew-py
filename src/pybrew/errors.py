"""Exception hierarchy used across pybrew."""

from __future__ import annotations


class PybrewError(Exception):
    """Base class for all pybrew errors."""


class FormulaParseError(PybrewError):
    """A Ruby formula could not be parsed."""


class FormulaNotFound(PybrewError):
    """No formula with the requested name exists in the configured taps."""


class ChecksumMismatch(PybrewError):
    """A downloaded artefact did not match its expected SHA-256."""


class DownloadError(PybrewError):
    """A download failed."""


class BottleError(PybrewError):
    """A bottle could not be selected, fetched or unpacked."""


class LinkConflict(PybrewError):
    """A link operation would overwrite a file that pybrew does not own."""


class UnsupportedInstall(PybrewError):
    """A formula requires a build step that the Python installer cannot run yet."""


class DependencyError(PybrewError):
    """Dependencies could not be resolved."""


class UsageError(PybrewError):
    """The command line was used incorrectly."""


class BuildError(PybrewError):
    """A source build command or install step failed."""

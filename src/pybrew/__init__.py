"""pybrew - a clean-room Python reimplementation of Homebrew's core.

The package intentionally keeps the same concepts as Homebrew (taps, formulae,
Cellar, kegs, bottles, receipts, opt links) while replacing the Ruby runtime and
implementing the core install pipeline in Python.
"""

__version__ = "0.1.0"

PYBREW_VERSION = f"pybrew {__version__}"

__all__ = ["__version__", "PYBREW_VERSION"]

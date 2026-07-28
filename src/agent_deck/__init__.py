"""Agent Deck package metadata.

This package contains the local daemon, core state models, rendering plans, and
hardware adapters for Agent Deck. Importing the package must not open devices,
start network listeners, read or write files, or mutate user configuration;
those side effects belong in CLI entry points and daemon startup code.
"""

#: Public package version string. It has no inputs, no return value, raises no
#: errors by itself, and exists so CLI tools and tests can identify the package
#: build without triggering network, filesystem, or hardware side effects.
__version__ = "0.2.0"

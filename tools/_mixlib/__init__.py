"""Shared implementation for the MixOS host and device-side tools.

Each module here replaces logic that had been copy-pasted across several
command line tools. The rule for putting something in this package is that at
least two tools need it and the copies had already started to disagree.

Nothing in this package imports a platform-specific module at import time, so
every tool that uses it stays importable on Windows, Linux and the Raspberry Pi
alike. Linux-only facilities are acquired inside the function that needs them
and degrade with a clear error rather than an ImportError at startup.
"""

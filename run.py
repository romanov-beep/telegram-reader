#!/usr/bin/env python3
"""Portable plugin entrypoint, independent of the caller's working directory."""
from telegram_reader.__main__ import main

raise SystemExit(main())

#!/usr/bin/env python3
"""Kept at the top level because skills and docs run design.py by this path.
The code is browser_llm/design.py; the command is `browser-llm design`."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_llm.design import *  # noqa: E402,F401,F403
from browser_llm.design import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

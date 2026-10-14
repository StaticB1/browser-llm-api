#!/usr/bin/env python3
"""Kept at the top level because scripts, skills and other repos run gen_asset.py
by this path. The code is browser_llm/gen_asset.py; the command is
`browser-llm image`, which takes the same options."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_llm.gen_asset import *  # noqa: E402,F401,F403
from browser_llm.gen_asset import main  # noqa: E402

if __name__ == "__main__":
    main()

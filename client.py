#!/usr/bin/env python3
"""Kept at the top level because skills and scripts run client.py by this path
or `from client import ask`. The code is browser_llm/client.py; the command is
`browser-llm ask`, and code can `from browser_llm.client import ask`."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_llm.client import *  # noqa: E402,F401,F403
from browser_llm.client import ask, main  # noqa: E402,F401

if __name__ == "__main__":
    main()

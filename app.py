#!/usr/bin/env python3
"""Compatibility launcher.

KiwiEater was reworked from a single file into the ``kiwieater`` package, but
``python app.py`` is kept working so existing instructions/muscle-memory still
launch the tool.  All it does is hand off to :func:`run.main`.
"""

import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from run import main
    main()

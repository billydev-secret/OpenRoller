"""Compatibility shim: ``python main.py`` still works.

The real entrypoint is ``riskyroller/__main__.py`` (``python -m riskyroller``
or the ``riskyroller`` command); this file only forwards to it so existing
service units and habits keep working.
"""

from riskyroller.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())

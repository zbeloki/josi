"""Allow running actl as ``python -m actl``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

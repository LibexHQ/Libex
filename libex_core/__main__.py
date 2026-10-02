"""
`python -m libex_core`, the same program as the `libex-core` script.
"""

import sys

from libex_core.cli.main import main

if __name__ == "__main__":
    sys.exit(main())

"""Enable ``python -m jobradar`` and serve as the PyInstaller entry point."""

import sys

from jobradar.cli import main

if __name__ == "__main__":
    sys.exit(main())

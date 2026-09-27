"""Allow `python -m cli`."""

import sys

from cli.main import main

sys.exit(main())

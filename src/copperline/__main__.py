"""Allow `python -m copperline` to invoke the CLI."""

from .cli import main

raise SystemExit(main())

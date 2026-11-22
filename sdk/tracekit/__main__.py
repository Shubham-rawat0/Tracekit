"""TraceKit command-line interface entry point.

Allows running tracekit operations from the command line,
including project management, API key operations, and dashboard serving.
"""

from .cli import main

# Running "python -m tracekit" takes the same argument parser path as the console script.
if __name__ == "__main__":
    raise SystemExit(main())

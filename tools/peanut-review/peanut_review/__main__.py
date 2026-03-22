"""Entry point for python3 -m peanut_review."""
import sys

if sys.version_info < (3, 10):
    sys.exit("Error: peanut-review requires Python 3.10+")

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

"""The safety benchmark: try everything worth trying, record what the middleware refused.

Not a test suite. The test suite proves the code does what its author intended; this
measures what a device actually permits and refuses, in a form somebody who did not write
the code can read, compare, and publish.

    open-mhs bench --out benchmark.md
"""

from open_mhs.bench.corpus import Attempt, for_cell, for_device
from open_mhs.bench.report import summary, to_console, to_json, to_markdown
from open_mhs.bench.runner import Bench, Result, Run

__all__ = [
    "Attempt", "for_device", "for_cell",
    "Bench", "Result", "Run",
    "summary", "to_markdown", "to_json", "to_console",
]

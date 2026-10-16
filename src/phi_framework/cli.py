"""Console entry points used by the python_wheel_task definitions in resources/*.job.yml."""

from __future__ import annotations

import sys

from phi_framework.config import parse_args
from phi_framework.runtime import configure_logging


def _run(module_main) -> None:
    configure_logging()
    module_main(parse_args(sys.argv[1:]))


def setup() -> None:
    from phi_framework.setup import main

    _run(main)


def scan() -> None:
    from phi_framework.scan import main

    _run(main)


def export() -> None:
    from phi_framework.export import main

    _run(main)


def ingest() -> None:
    from phi_framework.ingest import main

    _run(main)


def apply() -> None:
    from phi_framework.apply import main

    _run(main)

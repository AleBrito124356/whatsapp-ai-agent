"""Every transcript quoted in the README must be what the code really prints.

README blocks preceded by ``<!-- transcript: NAME -->`` must appear verbatim in
tests/golden/NAME.out, which tests/test_cli.py regenerates from the simulator.
"""

from __future__ import annotations

import re

from .conftest import REPO_ROOT

BLOCK = re.compile(r"<!-- transcript: (\w+) -->\s*```text\n(.*?)```", re.S)


def test_readme_transcripts_come_from_the_simulator():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    blocks = BLOCK.findall(readme)
    assert len(blocks) >= 5
    for name, block in blocks:
        golden = (REPO_ROOT / "tests" / "golden" / f"{name}.out").read_text(encoding="utf-8")
        assert block in golden, f"README block for {name} is not in the golden transcript:\n{block}"

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "synthetic"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def run_script(name: str, *args: str) -> subprocess.CompletedProcess:
    """CLI スクリプトを現在の python で実行(sudachipy 不在なら fallback)。"""
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *[str(a) for a in args]],
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session")
def scripts_dir() -> Path:
    return SCRIPTS_DIR


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR

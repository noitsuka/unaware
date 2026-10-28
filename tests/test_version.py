import json
import tomllib
from pathlib import Path

from unaware import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_the_same_everywhere():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    frontend = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))["version"]
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))["version"]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert __version__ == pyproject == frontend == lock
    assert f"## {__version__}" in changelog

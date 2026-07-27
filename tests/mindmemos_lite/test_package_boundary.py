from __future__ import annotations

import tomllib
from pathlib import Path

from mindmemos_lite.runtime import MindMemOS

import mindmemos_lite

REPO_ROOT = Path(__file__).resolve().parents[2]
LITE_PROJECT_ROOT = REPO_ROOT / "src" / "mindmemos_lite"


def test_lite_uses_its_own_import_namespace() -> None:
    package_dir = Path(mindmemos_lite.__file__).resolve().parent

    assert package_dir.name == "mindmemos_lite"
    assert MindMemOS.__module__ == "mindmemos_lite.runtime"
    assert not (LITE_PROJECT_ROOT / "mindmemos").exists()


def test_lite_distribution_and_cli_use_the_new_namespace() -> None:
    with (LITE_PROJECT_ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)["project"]

    assert project["name"] == "mindmemos-lite"
    assert project["scripts"]["mindmemos-lite-api"] == "mindmemos_lite.api.cli:main"

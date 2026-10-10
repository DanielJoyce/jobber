import importlib

import pytest

MODULES = [
    "jobhunter",
    "jobhunter.cli",
    "jobhunter.config",
    "jobhunter.core",
    "jobhunter.core.models",
    "jobhunter.core.db",
    "jobhunter.core.fetch",
    "jobhunter.core.textnorm",
    "jobhunter.core.geo",
    "jobhunter.sources",
    "jobhunter.sources.registry",
    "jobhunter.sources.adapters",
    "jobhunter.sources.adapters.base",
    "jobhunter.pipeline",
    "jobhunter.scoring",
    "jobhunter.console",
    "jobhunter.apply",
    "jobhunter.apply.paste",
    "jobhunter.apply.score",
]


@pytest.mark.parametrize("name", MODULES)
def test_imports(name):
    assert importlib.import_module(name)

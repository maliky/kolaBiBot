# -*- coding: utf-8 -*-
"""
Setup.py.

Check https://packaging.python.org/tutorials/packaging-projects/
and file in python/Docs/python-in-nutshell.pdf
upload to https://test.pypi.org/manage/projects/.
"""
from pathlib import Path

from setuptools import find_packages, setup

ROOT = Path(__file__).resolve().parent


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _read_requirements(path: Path, *, _seen: set[Path] | None = None) -> list[str]:
    seen = _seen if _seen is not None else set()
    resolved = path.resolve()
    if resolved in seen:
        return []
    seen.add(resolved)
    requirements: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            nested = (path.parent / line[3:].strip()).resolve()
            requirements.extend(_read_requirements(nested, _seen=seen))
            continue
        if line.startswith("--"):
            continue
        requirements.append(line)
    return requirements


def _is_test_dependency(requirement: str) -> bool:
    normalized = requirement.strip().lower()
    if normalized.startswith("pytest"):
        return True
    return normalized.startswith("hypothesis")


long_description = (ROOT / "README.org").read_text(encoding="utf-8")
install_requires = _unique(_read_requirements(ROOT / "requirements.txt"))
dev_requires = _unique(
    [req for req in _read_requirements(ROOT / "requirements-dev.txt") if req not in install_requires]
)
test_requires = _unique(
    [req for req in dev_requires if _is_test_dependency(req)]
    + [req for req in install_requires if req.strip().lower() == "responses"]
)

setup(
    name="kolabi",
    version="1.1.11",
    description="Kraken Futures trading bot and local market-data services.",
    long_description=long_description,
    long_description_content_type="text/plain",
    author="Malik Koné",
    author_email="malik.kone@pm.me",
    url="https://github.com/maliky/kolabi",
    packages=find_packages(exclude="secrets.py"),
    zip_safe=False,
    python_requires=">=3.13",
    entry_points={
        "console_scripts": [
            "kolabi-kraken-tree=kolabi.tree.kraken:main",
            "kolabi-kraken-account=kolabi.tree.account:main",
            "kolabi-kraken=kolabi.bargain.cli:main",
            "kolabi-kraken-smoke=kolabi.bargain.smoke:main",
        ]
    },
    # la version des dependances est pilotee par requirements*.txt
    install_requires=install_requires,
    extras_require={
        "dev": dev_requires,
        "packaging": ["twine"],
        "test": test_requires,
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: Financial and Insurance Industry",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: GNU General Public License (GPL)",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.13",
        "Topic :: Office/Business :: Financial",
        "Topic :: Utilities",
        "Topic :: System :: Monitoring",
    ],
    package_data={"Doc": ["Doc/*"], "demo_Orders": ["orders/*demo*.org"]},
)

# -*- coding: utf-8 -*-
"""
Setup.py.

Check https://packaging.python.org/tutorials/packaging-projects/
and file in python/Docs/python-in-nutshell.pdf
upload to https://test.pypi.org/manage/projects/.
"""
from setuptools import find_packages, setup

with open("README.rst", "r") as f:
    long_description = f.read()

setup(
    name="kolabi",
    version="1.1.11",
    description="Kraken Futures trading bot and local market-data services.",
    long_description=long_description,
    long_description_content_type="text/x-rst",
    author="Malik Koné",
    author_email="malikykone@gmail.com",
    url="https://github.com/maliky/kolabi",
    packages=find_packages(exclude="secrets.py"),
    zip_safe=False,
    python_requires=">=3.13",
    entry_points={
        "console_scripts": [
            "run_multi_kola=kolabi.runtime.run_multi_kola:main_prg",
            "multi_kola=kolabi.runtime.multi_kola:main_prg",
            "kolabi-kraken-tree=kolabi.tree.kraken:main",
            "kolabi-kraken-account=kolabi.tree.account:main",
            "kolabi-kraken=kolabi.bargain.cli:main",
            "kolabi-kraken-smoke=kolabi.bargain.smoke:main",
        ]
    },
    # la verions de websocket est importante
    install_requires=[
        "pandas",
        "numpy",
        "sqlalchemy",
        "python-binance",
        "requests",
        "dateparser",
        "websocket-client==0.53.0",
        "websockets>=15,<16",
    ],
    extras_require={
        "dev": ["mypy", "flake8", "black", "ruff", "pyright", "pylint"],
        "packaging": ["twine"],
        "test": ["pytest", "hypothesis", "responses"],
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
    package_data={"Doc": ["Doc/*"], "demo_Orders": ["Orders/*demo*.tsv"]},
)

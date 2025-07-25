# -*- coding: utf-8 -*-
"""
Setup.py.

Check https://packaging.python.org/tutorials/packaging-projects/
and file in python/Docs/python-in-nutshell.pdf
upload to https://test.pypi.org/manage/projects/.
"""
from setuptools import setup, find_packages

with open("README.rst", "r") as f:
    long_description = f.read()

setup(
    name="kolaBot",
    version="1.1.11",
    description="Trading bot with trail stop and chained orders for pour Bitmex and maybe more...",
    long_description=long_description,
    long_description_content_type="text/x-rst",
    author="Malik Koné",
    author_email="malikykone@gmail.com",
    url="https://github.com/maliky/kolaBot",
    packages=find_packages(exclude="secrets.py"),
    zip_safe=False,
    python_requires=">=3.8",
    entry_points={
        "console_scripts": [
            "kolabot_run_multi=kolaBot.run_multi_kola:main_prg",
            "kolabot_multi=kolaBot.multi_kola:main_prg",
            "kolabot_test=Tests.test_kola:main_prg",
        ]
    },
    # la verions de websocket est importante
    install_requires=["pandas", "numpy", 'websocket-client==0.53.0', "requests", "dateparser"],
    extras_require={
        "dev": ["mypy", "flake8", "black"],
        "packaging": ["twine"],
        "test": ["pytest", "hypothesis"],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: Financial and Insurance Industry",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: GNU General Public License (GPL)",
        "Natural Language :: French",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.8",
        "Topic :: Office/Business :: Financial",
        "Topic :: Utilities",
        "Topic :: System :: Monitoring",
    ],
    package_data={"Doc": ["Doc/*"], "demo_Orders": ["Orders/*demo*.tsv"]},
)

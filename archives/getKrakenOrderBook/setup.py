# -*- coding: utf-8 -*-
"""
Setup.py.

Check https://packaging.python.org/tutorials/packaging-projects/
and file in python/Docs/python-in-nutshell.pdf
upload to https://test.pypi.org/manage/projects/.
https://setuptools.readthedocs.io/en/latest/setuptools.html#including-data-files
"""
from setuptools import setup, find_packages

with open("README.rst", "r") as f:
    long_description = f.read()

setup(
    name="getKrakenOrderBook",
    version="0.1.0",
    description="Utility to download kraken Orderbook and store it",
    long_description=long_description,
    long_description_content_type="text/x-rst",
    author="Malik Koné",
    author_email="malik.kone@pm.me",
    url="https://github.com/maliky/getKrakenOrderBook",
    packages=find_packages(),
    zip_safe=False,
    python_requires=">=3.11.3",
    entry_points={
        "console_scripts": [
            "get_kraken_orderbook=getKrakenOrderBook.get_kraken_orderbook:main_prg",
        ]
    },
    install_requires=[
    ],
    extras_require={
        'dev': ['mypy', 'flake8', 'black'],
        'packaging': ['twine'],
        #        "test": ['pytest', 'hypothesis'],
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
        "Topic :: Office/Business :: Financial",
        "Topic :: Utilities",
    ],
    package_data={"Doc": ["*txt"]},
)

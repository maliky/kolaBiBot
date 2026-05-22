"""Order orchestration (kolaBiBot).

Pure package import only. Impure service/runtime shells stay in explicit
submodules so importing `kolabi.bot` does not pull SQLAlchemy, requests, or
exchange adapters.
"""

__all__: list[str] = []

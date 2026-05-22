from __future__ import annotations

import json
import subprocess
import sys


def test_pure_bot_modules_import_without_impure_dependencies() -> None:
    script = """
import json
import sys

import kolabi.bot
import kolabi.bot.domain
import kolabi.bot.pricing
import kolabi.bot.pair_cycle
import kolabi.bot.isis
import kolabi.bot.horus
import kolabi.bot.dragon

banned = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] in {"sqlalchemy", "pandas", "dateparser", "requests"}
    or name.startswith("kolabi.shared.exchanges")
)
if "kolabi.bot.service" in sys.modules:
    banned.append("kolabi.bot.service")
print(json.dumps(banned))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == []

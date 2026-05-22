"""Ogun command executor.

Purpose: execute DragonSong commands against an async exchange-agnostic port.
Inputs: DragonSong values and an ExchangePort implementation.
Outputs: typed order acknowledgements.
Side effects: exchange calls through the injected ExchangePort.
Important types: DragonSong, ExchangePort, OrderAck.
Role: async interpreter shell.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    AmendTailCommand,
    CancelCommand,
    DragonSong,
    ExchangePort,
    PlaceHeadCommand,
    PlaceTailCommand,
)


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_delay_seconds: float = 0.25


class OgunExecutor:
    """Single active async executor for DragonSong commands."""

    def __init__(
        self,
        port: ExchangePort,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.port = port
        self.retry_policy = retry_policy or RetryPolicy()

    async def execute(self, command: DragonSong) -> OrderAck:
        attempts = max(1, self.retry_policy.attempts)
        delay = max(0.0, self.retry_policy.base_delay_seconds)
        for attempt in range(1, attempts + 1):
            try:
                return await self._dispatch(command)
            except Exception:
                if attempt >= attempts:
                    raise
                await asyncio.sleep(delay * attempt)
        raise RuntimeError("unreachable retry loop")

    async def _dispatch(self, command: DragonSong) -> OrderAck:
        if isinstance(command, PlaceHeadCommand):
            return cast(OrderAck, await self.port.place_head(command))
        if isinstance(command, PlaceTailCommand):
            return cast(OrderAck, await self.port.place_tail(command))
        if isinstance(command, AmendHeadCommand):
            return cast(OrderAck, await self.port.amend_head(command))
        if isinstance(command, AmendTailCommand):
            return cast(OrderAck, await self.port.amend_tail(command))
        if isinstance(command, CancelCommand):
            return cast(OrderAck, await self.port.cancel(command))
        raise TypeError(f"Unsupported DragonSong type: {type(command)!r}")

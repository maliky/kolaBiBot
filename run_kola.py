import argparse
from typing import Any, Dict

from kolabi.shared.core.bargain import Bargain


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Kola bot")
    parser.add_argument("--exchange", default="binance", help="Exchange adapter to use")
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading symbol to bind")
    parser.add_argument(
        "--testnet",
        action="store_true",
        help="Prefer testnet credentials/base URL when available",
    )
    parser.add_argument("--api-key", help="Override API key")
    parser.add_argument("--api-secret", help="Override API secret")
    parser.add_argument("--base-url", help="Override REST base URL")
    parser.add_argument("--order-id-prefix", help="BitMEX orderID prefix override")
    parser.add_argument(
        "--post-only",
        action="store_true",
        help="Force ParticipateDoNotInitiate flag when supported",
    )
    parser.add_argument(
        "--timeout", type=float, help="Adapter specific timeout (seconds)"
    )
    args = parser.parse_args()

    config_kwargs: Dict[str, Any] = {}
    if args.api_key:
        config_kwargs["api_key"] = args.api_key
    if args.api_secret:
        config_kwargs["api_secret"] = args.api_secret
    if args.base_url:
        config_kwargs["base_url"] = args.base_url

    adapter_kwargs: Dict[str, Any] = {}
    if args.order_id_prefix:
        adapter_kwargs["orderIDPrefix"] = args.order_id_prefix
    if args.timeout is not None:
        adapter_kwargs["timeout"] = args.timeout
    if args.post_only:
        adapter_kwargs["postOnly"] = True
    if adapter_kwargs:
        config_kwargs["adapter_kwargs"] = adapter_kwargs

    config_kwargs["testnet"] = args.testnet

    try:
        bargain = Bargain.from_env(args.exchange, symbol=args.symbol, **config_kwargs)
    except (ImportError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))

    print(
        f"Adapter '{args.exchange}' ready for {bargain.describe()} "
        f"via {bargain.config.base_url}"
    )


if __name__ == "__main__":
    main()

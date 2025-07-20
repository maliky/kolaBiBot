import argparse
from kola.exchanges import get_adapter


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Kola bot")
    parser.add_argument("--exchange", default="binance", help="Exchange adapter to use")
    args = parser.parse_args()
    try:
        get_adapter(args.exchange)
    except ImportError as exc:
        parser.error(str(exc))
    print(f"Adapter '{args.exchange}' loaded")


if __name__ == "__main__":
    main()

from datetime import timezone

from kolabi.tree.binance import (
    BinanceConfig,
    extract_book_payload,
    parse_binance_time,
    public_stream_url,
    ticker_prices_from_message,
    unwrap_combined_stream,
)


def test_public_stream_url_uses_combined_futures_stream() -> None:
    config = BinanceConfig(pair="BTCUSDT", ws_url="wss://example/stream", depth=20)

    assert public_stream_url(config) == (
        "wss://example/stream?streams="
        "btcusdt@depth20@500ms/btcusdt@markPrice@1s/btcusdt@ticker"
    )


def test_public_stream_url_uses_spot_stream_without_mark_price() -> None:
    config = BinanceConfig(
        pair="BTCUSDT",
        ws_url="wss://example/stream",
        depth=20,
        market_type="spot",
    )

    assert public_stream_url(config) == (
        "wss://example/stream?streams=btcusdt@depth20@500ms/btcusdt@ticker"
    )


def test_extract_depth_payload_from_combined_stream() -> None:
    message = unwrap_combined_stream(
        {
            "stream": "btcusdt@depth20@500ms",
            "data": {
                "e": "depthUpdate",
                "E": 1_717_000_000_000,
                "s": "BTCUSDT",
                "u": 7,
                "b": [["100.0", "2"], ["99.5", "0"]],
                "a": [["100.5", "3"]],
            },
        }
    )

    payload = extract_book_payload(message)

    assert payload is not None
    assert payload.symbol == "BTCUSDT"
    assert payload.bids == ((100.0, 2.0),)
    assert payload.asks == ((100.5, 3.0),)
    assert payload.sequence == 7


def test_ticker_prices_from_mark_and_ticker_events() -> None:
    mark = ticker_prices_from_message(
        {"e": "markPriceUpdate", "s": "BTCUSDT", "p": "100.1", "i": "99.9"}
    )
    ticker = ticker_prices_from_message({"e": "24hrTicker", "s": "BTCUSDT", "c": "101.2"})

    assert mark is not None
    assert mark.mark_price == 100.1
    assert mark.index_price == 99.9
    assert ticker is not None
    assert ticker.last_price == 101.2


def test_parse_binance_time_is_utc() -> None:
    parsed = parse_binance_time(1_717_000_000_000)

    assert parsed is not None
    assert parsed.tzinfo == timezone.utc

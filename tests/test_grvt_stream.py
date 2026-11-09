"""El stream público conserva identidad, frescura y bid/ask ejecutable."""

import json

import pytest

from app.marketdata.grvt_stream import (GrvtStreamError, GrvtTickerStream, URL,
                                        instruments_for, parse_ticker_frame, subscription)


NOW = 1_791_897_992_000_000_000
INSTRUMENT = "BTC_USDT_Perp"
SYMBOL = "BTC/USDT:PERP"


def ack():
    return json.dumps({"jsonrpc": "2.0", "result": {"stream": "v1.ticker.s",
                       "subs": [INSTRUMENT + "@500"], "unsubs": []},
                       "id": 1, "method": "subscribe"})


def frame(**changes):
    feed = {"event_time": str(NOW - 1_000_000_000), "instrument": INSTRUMENT,
            "mark_price": "100.1", "last_price": "200", "best_bid_price": "100",
            "best_ask_price": "100.2"}
    feed.update(changes)
    return json.dumps({"stream": "v1.ticker.s", "selector": INSTRUMENT + "@500",
                       "sequence_number": "10", "feed": feed})


def test_subscription_and_live_frame_use_exact_perp_identity():
    instruments = instruments_for((SYMBOL,))
    assert instruments == {INSTRUMENT: SYMBOL}
    request = json.loads(subscription(instruments))
    assert request["params"] == {"stream": "v1.ticker.s", "selectors": [INSTRUMENT + "@500"]}
    assert parse_ticker_frame(ack(), instruments, NOW) is None
    symbol, quote = parse_ticker_frame(frame(), instruments, NOW)
    assert symbol == SYMBOL and quote.instrument == INSTRUMENT
    assert str(quote.bid) == "100" and str(quote.ask) == "100.2"
    assert str(quote.mark) == "100.1"  # no usa last_price como precio de disparo


@pytest.mark.parametrize("raw", [
    "not json", json.dumps({"error": {"code": 1}}),
    json.dumps({"stream": "v1.trade", "selector": INSTRUMENT + "@500", "feed": {}}),
    frame(instrument="ETH_USDT_Perp"), frame(event_time=str(NOW + 3_000_000_000)),
    frame(event_time=str(NOW - 16_000_000_000)), frame(best_bid_price="101"),
    frame(best_ask_price="99"), frame(mark_price=None),
])
def test_rejects_untrusted_or_non_executable_frames(raw):
    with pytest.raises(GrvtStreamError):
        parse_ticker_frame(raw, {INSTRUMENT: SYMBOL}, NOW)


def test_rejects_spot_duplicate_and_unexpected_ack():
    with pytest.raises(GrvtStreamError):
        instruments_for(("BTC/USDT",))
    with pytest.raises(GrvtStreamError):
        instruments_for((SYMBOL, SYMBOL))
    wrong_ack = json.loads(ack())
    wrong_ack["result"]["subs"] = ["ETH_USDT_Perp@500"]
    with pytest.raises(GrvtStreamError):
        parse_ticker_frame(json.dumps(wrong_ack), {INSTRUMENT: SYMBOL}, NOW)


def test_stream_closes_socket_on_success_and_rejected_ack():
    class Socket:
        def __init__(self, confirmation):
            self.confirmation = confirmation
            self.sent = None
            self.closed = False

        def send(self, raw):
            self.sent = json.loads(raw)

        def recv(self, *, timeout):
            return self.confirmation if timeout == 7 else frame()

        def close(self):
            self.closed = True

    good = Socket(ack())
    def connector(url, **options):
        assert url == URL and options["open_timeout"] == 7
        return good
    with GrvtTickerStream((SYMBOL,), connector=connector, clock_ns=lambda: NOW) as stream:
        assert stream.recv()[0] == SYMBOL
        assert good.sent["params"]["selectors"] == [INSTRUMENT + "@500"]
    assert good.closed

    bad = Socket(json.dumps({"error": {"code": 1}}))
    with pytest.raises(GrvtStreamError):
        with GrvtTickerStream((SYMBOL,), connector=lambda *_args, **_kw: bad,
                              clock_ns=lambda: NOW):
            pass
    assert bad.closed


def test_testnet_stream_uses_only_the_fixed_public_testnet_host():
    from app.marketdata.grvt_stream import TESTNET_URL
    class Socket:
        def send(self,message): pass
        def recv(self,*,timeout): return ack() if timeout==7 else frame()
        def close(self): pass
    connected=[]
    def connect(url,**options): connected.append(url); return Socket()
    with GrvtTickerStream((SYMBOL,),network='TESTNET',connector=connect,clock_ns=lambda:NOW) as stream:
        assert stream.recv()[0]==SYMBOL
    assert connected==[TESTNET_URL] and TESTNET_URL=='wss://market-data.testnet.grvt.io/ws/full'

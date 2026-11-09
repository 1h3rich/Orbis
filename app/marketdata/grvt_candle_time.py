"""Nanosecond close evidence shared by GRVT TRADE and MARK history."""

NS = 1_000_000_000


class GrvtHistoryError(ValueError):
    """El histórico solicitado no es completo o no es verificable."""


class GrvtCandleNotClosed(GrvtHistoryError):
    """La última vela solicitada aún no tiene un cierre confirmado."""

    def __init__(self, closes_at_ns: int):
        super().__init__("Última vela GRVT pendiente de cierre")
        self.closes_at_ns = closes_at_ns


def read_candle_close(open_time: object, close_time: object, *,
                      start: int, end: int, step: int) -> tuple[int, bool]:
    """Return the open second and whether the latest candle has closed."""
    times = (open_time, close_time)
    if any(not isinstance(value, str) or not 1 <= len(value) <= 20
           or not value.isascii() or not value.isdecimal() for value in times):
        raise GrvtHistoryError("Tiempos GRVT de vela no verificables")
    opened, closed = map(int, times)
    boundary = opened + step * NS
    stamp = opened // NS
    if (opened % NS or not start <= stamp < end or (stamp - start) % step
            or not opened <= closed <= boundary):
        raise GrvtHistoryError("Tiempos GRVT de vela fuera del rango cerrado")
    if closed < boundary and boundary != end * NS:
        raise GrvtHistoryError("Vela GRVT histórica incompleta")
    return stamp, closed == boundary

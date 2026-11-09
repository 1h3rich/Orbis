"""Remove future tables before representing a genuinely older owned database."""


def drop_observed_tables(db):
    db.execute('DROP TABLE IF EXISTS paper_observed_gap_closes')
    for name in ('paper_observed_trades','paper_observed_candles','paper_observed_events','paper_observed_sessions'):
        db.execute('DROP TABLE '+name)

"""Public-GRVT-labelled synthetic history, wholly owned and prior to PAPER."""
from app.lab.engine import validate_candles,CURRENT_ENGINE_VERSION
from app.lab.validation import chronological_holdout,VALIDATION_ENGINE_VERSION
from app.lab.store import content_hash


def historical_grvt_reserve(lab,version_id,before_t):
    rows=[{'t':before_t+(i-120)*3600,'o':'100','h':'102','l':'98','c':'99','v':'10'} for i in range(120)]
    bars,gaps=validate_candles(rows,'1h',now=before_t)
    dataset=lab.save_dataset('BTC/USDT:PERP','1h','grvt-perp-trade-public',bars,gaps,verified_public=True)
    version=lab.version(version_id)
    return lab.save_validation(dataset['id'],version_id,dataset['hash'],chronological_holdout(bars,version['definition'],'1h'),
        VALIDATION_ENGINE_VERSION,lab_engine_version=CURRENT_ENGINE_VERSION,expected_strategy_hash=content_hash(version['definition']))

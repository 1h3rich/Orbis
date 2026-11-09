"""A separate long historical reserve, preceding the small live PAPER tape."""
from app.lab.engine import INTERVALS, CURRENT_ENGINE_VERSION, validate_candles
from app.lab.store import content_hash
from app.lab.validation import chronological_holdout, VALIDATION_ENGINE_VERSION
from app.automation.validation import chronological_dca_holdout, VALIDATION_ENGINE_VERSION as DCA_VALIDATION_VERSION


def historical_reserve(lab, version_id, *, before_t, symbol='BTC/USDT', timeframe='1h', automation=None):
    step = INTERVALS[timeframe]
    rows = [{'t':before_t+(i-120)*step,'o':'100','h':'102','l':'98','c':'99','v':'10'} for i in range(120)]
    bars,gaps = validate_candles(rows,timeframe,now=before_t)
    dataset = lab.save_dataset(symbol,timeframe,'mexc-spot-public',bars,gaps,verified_public=True)
    version = (automation or lab).version(version_id)
    definition = version['definition']
    if automation is not None:
        return automation.save_validation(version_id,dataset['id'],dataset['hash'],
            chronological_dca_holdout(bars,definition,timeframe),DCA_VALIDATION_VERSION,
            lab_path=lab.path,expected_definition_hash=content_hash(definition))
    return lab.save_validation(dataset['id'],version_id,dataset['hash'],
        chronological_holdout(bars,definition,timeframe),VALIDATION_ENGINE_VERSION,
        lab_engine_version=CURRENT_ENGINE_VERSION,expected_strategy_hash=content_hash(definition))

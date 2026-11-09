"""Empty owned LAB/PAPER plus a recently captured minute dataset and OOS proof."""
import time

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.paper_store import PaperPerpStore
from app.derivatives.perp_observed_archive import ObservedArchive
from app.derivatives.perp_observed_origin import verified_observed_origin
from app.derivatives.perp_observed_types import observed_version
from app.derivatives.perp_observed_wire import read_observed_seed,specification_input
from app.derivatives.paper_math import InstrumentSpec
from app.lab.access import lab_access
from app.lab.engine import validate_strategy
from app.lab.perp_dataset import read_perp_dataset
from app.lab.perp_experiments import PerpExperiments
from app.lab.perp_profile import PerpProfile,read_profile
from app.lab.perp_store import PerpDatasetStore
from app.lab.perp_validation import PerpValidationPlan
from app.lab.perp_validation_store import PerpValidations
from app.lab.store import LabStore,content_hash
from tests.perp_validation_fixture import validation_inputs
from tests.test_perp_limit_simulation import limit_profile

real_time_ns=time.time_ns


LAB_KEY='owned observed LAB source password'
PAPER_KEY='owned observed PAPER archive password'
NS=10**9


def owned_archive(tmp_path,monkeypatch,*,limited=False,direction='LONG',margin_mode='ISOLATED',partial_targets=None,
                  strategy_changes=None, optimized=False):
    lab_path=tmp_path/'lab.db';paper_path=tmp_path/'paper.db'
    lab_access.create(lab_path,LAB_KEY);lab=LabStore(lab_path)
    raw,strategy,configured=validation_inputs(monkeypatch,120,direction=direction,margin_mode=margin_mode);raw=raw.payload()
    if strategy_changes:
        strategy=validate_strategy({**strategy,**strategy_changes})
    now=real_time_ns();start=(now//(3600*NS)+1)*3600*NS+300*NS
    end=now//(60*NS)*60;first=end-120*60
    raw.update(timeframe='1m',start=first,end=end,captured_start_ns=str(now),captured_end_ns=str(now+1))
    for index,(trade,mark) in enumerate(zip(raw['trade']['bars'],raw['mark']['bars'])):
        trade['t']=mark['t']=first+index*60
    for key in ('rules','specification'):raw[key]['fetched_at_ns']=str(now)
    template=raw['funding'][0]
    raw['funding']=[{**template,'funding_time':str(stamp*NS),'funding_rate':'0','mark_price':'100'}
        for stamp in range((first-1)//3600*3600,(end-1)//3600*3600+1,3600)]
    data=read_perp_dataset(raw)
    version=lab.create_strategy(f'Observed owned {direction}',strategy)
    configured=PerpProfile.model_validate({**configured.payload(),'strategy_version_id':version['version_id'],
                                           'strategy_hash':content_hash(strategy)})
    if limited:
        configured=limit_profile(strategy,offset='0.01',valid_bars=1,margin_mode=margin_mode,
                                 strategy_version_id=version['version_id'])
    if partial_targets is not None:
        configured=read_profile({**configured.payload(),'schema':'orbis.lab.perp.profile.v3',
                                 'partial_targets':partial_targets})
    stored=PerpDatasetStore(lab).save(data);profile=PerpExperiments(lab).save_profile(configured)
    validations = PerpValidations(lab)
    if optimized:
        from app.lab.perp_optimization import PerpOptimizationPlan
        from app.lab.perp_optimization_store import PerpOptimizations

        optimizations = PerpOptimizations(lab)
        grid = PerpOptimizationPlan(stop_losses=['0.02','0.03'], take_profits=['0.01','0.015'], train_fraction='0.5')
        saved = optimizations.create(stored['id'], profile['id'], grid)['optimization']
        optimizations.materialize(saved['id'], saved['hash'])
        report = validations.create_from_optimization(saved['id'], saved['hash'])
        selected = validations._verified(report['id'])
        configured, strategy = selected['profile'], selected['definition']
    else:
        report = validations.create(stored['id'], profile['id'],
            PerpValidationPlan(method='CHRONOLOGICAL_HOLDOUT', train_fraction='0.5'))
    origin=verified_observed_origin(lab,report['id'])
    history=[b.model_dump() for b in data.trade.bars]
    for stamp in range(end,start//NS,60):history.append({**history[-1],'t':stamp})
    revision, engine = observed_version(configured)
    seed=read_observed_seed({'schema':f'orbis.paper.perp.observed.seed.v{revision}','engine':engine,
        'profile':configured.payload(),'definition':strategy,'timeframe':'1m','received_at_ns':str(start),
        'quote':{'instrument':data.instrument,'event_time_ns':str(start),'mark_price':'100','bid':'99','ask':'100'},
        'rules':{**data.rules.model_dump(),'fetched_at_ns':str(start),
            'risk_brackets':[{**row.model_dump(),'max_leverage':str(row.max_leverage)} for row in data.rules.risk_brackets]},
        'specification':specification_input(InstrumentSpec.from_payload(
            {**data.specification.model_dump(),'fetched_at_ns':start},data.instrument,start)).payload(),'history':history,
        'funding':[{'instrument':data.instrument,'funding_time':str(start//(3600*NS)*(3600*NS)),
                    'funding_rate':'0','mark_price':'100','funding_interval_hours':1}]})
    paper_perp_access.create(paper_path,PAPER_KEY);paper=PaperPerpStore(paper_path)
    clock={'now':start};archive=ObservedArchive(paper,clock_ns=lambda:clock['now'])
    return lab,paper,archive,origin,seed,clock

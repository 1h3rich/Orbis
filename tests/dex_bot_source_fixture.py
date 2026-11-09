"""Tested LAB and encrypted PAPER BNB sources; no wallet or exchange writes."""
from datetime import datetime,timedelta,timezone
from pathlib import Path

from app.automation.spot_paper_access import spot_paper_access
from app.automation.spot_strategy_paper import SpotStrategyPaper
from app.lab.engine import CURRENT_ENGINE_VERSION,run_backtest,validate_candles
from app.lab.store import LabStore
from tests.test_spot_strategy_paper import definition

PASSWORD='isolated DEX bot source password'


class SourceCase:
    def __init__(self,directory:Path,*,symbol='BNB/USDT',tested=True,timeframe='1h',research=False):
        from app.lab.engine import INTERVALS
        step=INTERVALS[timeframe]
        self.now=datetime.fromtimestamp(int(datetime.now(timezone.utc).timestamp())//step*step+1,timezone.utc)
        boundary=int(self.now.timestamp())//step*step
        self.rows=[{'t':boundary+(i-4)*step,'o':str(c+1),'h':str(c+2),'l':str(c-1),'c':str(c),'v':'10'}
                   for i,c in enumerate((100,99,98,97,96,95))]
        self.now-=timedelta(seconds=step)
        self.lab=LabStore(directory/'lab.db')
        bars,gaps=validate_candles(self.rows[:3],timeframe,now=int(self.now.timestamp()))
        self.dataset=self.lab.save_dataset(symbol,timeframe,'mexc-spot-public',bars,gaps,verified_public=True)
        self.version=self.lab.create_strategy('BNB probado',definition())
        self.run=self.lab.save_run(self.dataset['id'],self.version['version_id'],
            run_backtest(bars,self.version['definition'],timeframe),CURRENT_ENGINE_VERSION)
        case=self
        class Source:
            def get_candles(self,symbol,*,since,limit,**_):return [row.copy() for row in case.rows if row['t']>=since][:limit]
            def get_paper_quote(self,symbol):return {'symbol':symbol,'source':'mexc-spot-public','event_time_ns':int(case.now.timestamp())*10**9,'bid':'96','ask':'97','last':'96'}
        spot_paper_access.create(directory/'paper.db',PASSWORD)
        self.paper=SpotStrategyPaper(directory/'paper.db',self.lab.path,adapter_factory=Source,
            clock_ns=lambda:int(self.now.timestamp())*10**9)
        self.session=self.paper.create(self.version['version_id'],self.dataset['id'],self.run['id'] if tested else None)
        self.now+=timedelta(seconds=step)
        assert self.paper.poll(self.session['id'])['poll_result']=='OBSERVED'
        if research:
            from tests.mexc_research_fixture import historical_reserve
            self.validation=historical_reserve(self.lab,self.version['version_id'],before_t=self.dataset['start_t'],
                symbol='BNB/USDT',timeframe=self.dataset['timeframe'],automation=getattr(self,'store',None))

    def close(self):spot_paper_access.lock(self.paper.path)


DEX_EMA_RULE = {"ma_adjustment": {"kind": "ema", "period": 2, "condition": "below",
                                  "matched_factor": "0.00005", "unmatched_factor": "0"}}
DEX_VOLUME_RULE = {"volume_gate": {"period": 3, "min_ratio": "2"}}


class DcaSourceCase:
    def __init__(self,directory:Path,*,research=False,average_rule=None,volume=False):
        from app.automation.access import automation_access
        from app.automation.dca import dca_engine_for_rule,simulate_dca,validate_dca
        from app.automation.paper_dca import PaperDca
        from app.automation.store import AutomationStore
        from app.lab.store import content_hash
        day=int(datetime.now(timezone.utc).timestamp())//86400*86400
        self.now=datetime.fromtimestamp(day-86400+1,timezone.utc)
        count=6 if volume else 3
        prices=(100,99,98,97,97,97,97,96,95) if volume else (100,99,98,97,96,95)
        self.rows=[{'t':day+(i-count-1)*86400,'o':str(c),'h':str(c+2),'l':str(c-1),'c':str(c),
                    'v':'20' if volume and i==3 else '10'} for i,c in enumerate(prices)]
        self.lab=LabStore(directory/'lab.db')
        bars,gaps=validate_candles(self.rows[:count],'1d',now=int(self.now.timestamp()))
        self.dataset=self.lab.save_dataset('BNB/USDT','1d','mexc-spot-public',bars,gaps,verified_public=True)
        automation_access.create(directory/'dca.db',PASSWORD)
        self.store=AutomationStore(directory/'dca.db',allow_create=False)
        rule=validate_dca({'frequency':'monthly','amount':'100','budget_total':'1000','monthly_limit':'300',
            'max_exposure':'1','fee_rate':'0.001','slippage_rate':'0.002','dips':[{'drop_pct':'0.1','amount':'50'}],
            **(DEX_VOLUME_RULE if volume else average_rule or {})})
        self.version=self.store.create_rule('DCA BNB probado',rule)
        self.run=self.store.save_run(self.version['version_id'],self.dataset['id'],self.dataset['hash'],
            simulate_dca(bars,rule,timeframe='1d'),dca_engine_for_rule(rule),
            lab_path=self.lab.path,expected_definition_hash=content_hash(rule))
        case=self
        class Source:
            def get_candles(self,symbol,*,since,limit,**_):return [row.copy() for row in case.rows if row['t']>=since][:limit]
            def get_paper_quote(self,symbol):return {'symbol':symbol,'source':'mexc-spot-public','event_time_ns':int(case.now.timestamp())*10**9,'bid':'97','ask':'98','last':'97'}
        self.paper=PaperDca(self.store.path,self.lab.path,adapter_factory=Source,clock_ns=lambda:int(self.now.timestamp())*10**9)
        self.session=self.paper.create(self.version['version_id'],self.dataset['id'],self.run['id'])
        self.now+=timedelta(days=1)
        assert self.paper.poll(self.session['id'])['poll_result']=='OBSERVED'
        if research:
            from tests.mexc_research_fixture import historical_reserve
            self.validation=historical_reserve(self.lab,self.version['version_id'],before_t=self.dataset['start_t'],
                symbol='BNB/USDT',timeframe=self.dataset['timeframe'],automation=getattr(self,'store',None))

    def close(self):
        from app.automation.access import automation_access
        automation_access.lock(self.store.path)

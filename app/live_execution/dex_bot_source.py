"""Read-only preparation of a tested LAB/PAPER source for the bounded BNB DEX.

Signal candles remain MEXC Spot BNB/USDT; execution is USDT/WBNB on Uniswap.
This preparation grants no signing permission and copies no fictitious capital.
"""
from __future__ import annotations

from datetime import datetime,timedelta
from decimal import Decimal,InvalidOperation,localcontext
from typing import Literal
from uuid import UUID
from types import SimpleNamespace

from app.automation.dca import dca_signal_window_key
from app.automation.spot_paper_access import spot_paper_format
from app.lab.engine import INTERVALS
from app.lab.store import content_hash
from app.live_execution.dca_bot_source import DcaBotSource,verified_native_bot_observation
from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio.wallet_funding import _time

SourceKind=Literal['STRATEGY','DCA']
UNIT=10**18


def exact_units(value:str,field:str,*,maximum:int) -> int:
    """Require exact token units; never round user capital or accept JSON floats."""
    try:
        if type(value) is not str or not 1<=len(value)<=100:raise ValueError()
        number=Decimal(value)
        if not number.is_finite() or number<=0 or number>maximum or abs(number.as_tuple().exponent)>100:
            raise ValueError()
        with localcontext() as context:
            context.prec=300
            units=number*UNIT
            if units!=units.to_integral_value():raise ValueError()
        return int(units)
    except (ValueError,InvalidOperation,OverflowError):
        raise DexExecutorError(field+': importe positivo exacto de hasta 18 decimales') from None


class DexBotSources:
    """Encrypted source discovery and reproducible preparation; no chain I/O."""
    def __init__(self,paper=None,dca_paper=None):
        self.paper=paper
        self.dca=DcaBotSource(dca_paper,allow_moving_average=True,allow_volume=True) if dca_paper is not None else None

    def _repository(self,kind:SourceKind):
        if kind not in ('STRATEGY','DCA'):raise DexExecutorError('Tipo de bot no admitido')
        repository=self.dca if kind=='DCA' else self.paper
        if (repository is None or spot_paper_format(repository.path)!='encrypted_or_invalid'
                or repository.current_passphrase() is None):
            raise DexExecutorError('Desbloquea '+('DCA' if kind=='DCA' else 'Spot PAPER')+' cifrado')
        return repository

    def research_repository(self, kind:SourceKind):
        """Archived PAPER can qualify without reopening its live observer.

        LAB/DCA still enforce their own encrypted-store access on each read.
        """
        if kind not in ('STRATEGY','DCA'):raise DexExecutorError('Tipo de bot no admitido')
        repository=self.dca if kind=='DCA' else self.paper
        if repository is None:raise DexExecutorError('El almacén de investigación no está configurado')
        return repository

    def research_options(self, kind:SourceKind, source:dict) -> list[dict]:
        from app.research.mexc_binding import mexc_research_options
        identity=SimpleNamespace(**source)
        identity.binding=lambda:source
        return mexc_research_options(self.research_repository(kind),identity)

    def check_research(self, kind:SourceKind, source:dict, candle_t:int, validation_id:str):
        from app.research.dex_binding import check_dex_research
        return check_dex_research(self.research_repository(kind),source,candle_t,validation_id)

    def observation(self,kind:SourceKind,session_id:str,*,now:datetime,verify_backtest:bool=True):
        now=_time(now)
        repository=self._repository(kind)
        source=verified_native_bot_observation(repository,str(UUID(session_id)),now=now,verify_backtest=verify_backtest)
        if source.symbol!='BNB/USDT':
            raise DexExecutorError('Este ejecutor opera USDT/WBNB; prueba BNB/USDT en LAB y PAPER')
        # Native source verifiers reproduce all nested and multi-timeframe rules.
        if kind=='STRATEGY' and source.definition['direction']!='LONG':
            raise DexExecutorError('El DEX Spot admite LONG; los shorts pertenecen a derivados')
        return source

    @staticmethod
    def _require_latest(source,now:datetime) -> None:
        step=INTERVALS[source.timeframe]
        if source.candle_t!=int(_time(now).timestamp())//step*step-step:
            raise DexExecutorError('Actualiza PAPER: falta la última vela cerrada')

    def candidates(self, *, now:datetime, selection:tuple[SourceKind,str]|None=None) -> list[dict]:
        results=[];available=False
        for kind in (selection[0],) if selection is not None else ('STRATEGY','DCA'):
            try:repository=self._repository(kind)
            except DexExecutorError:
                if selection is not None:raise
                continue
            available=True
            sessions = (repository.sessions(session_id=selection[1]) if selection is not None else repository.sessions())
            if not sessions:continue
            versions={item['id']:item for item in (repository.lab.all_versions() if kind=='STRATEGY' else
                [{**version,'name':rule['name']} for rule in repository.store.rules() for version in repository.store.versions(rule['id'])])}
            for session in sessions:
                if session['status']!='ACTIVE':continue
                version=versions.get(session['version_id'],{})
                item={'source_kind':kind,'session_id':session['id'],'version_id':session['version_id'],
                    'name':version.get('name','DCA' if kind=='DCA' else 'Estrategia'),'version':version.get('version'),
                    'symbol':session['symbol'],'timeframe':'1d' if kind=='DCA' else session['timeframe'],
                    'preparation_only':True,'automatic_execution':False,'paper_ready':False,'research_options':[]}
                try:
                    source=self.observation(kind,session['id'],now=now,verify_backtest=False)
                    self._require_latest(source,now)
                    item.update(ready=True,paper_ready=True,reason=None,source=source.binding())
                    options=self.research_options(kind,source.binding())
                    item.update(research_options=options,research_verification='ON_PREVIEW_BIND_AND_START',ready=bool(options),
                        reason=None if options else 'Valida fuera de muestra esta versión antes de vincularla')
                except ValueError as exc:item.update(ready=False,reason=str(exc))
                results.append(item)
        if selection is not None:self._repository(selection[0])
        if not available:raise DexExecutorError('Desbloquea Spot PAPER o DCA cifrado para consultar estrategias probadas')
        return results

    def preview(self,*,kind:SourceKind,session_id:str,entry_usdt:str,budget_usdt:str,exposure_usdt:str,
                loss_usdt:str,wbnb_quota:str,monthly_usdt:str|None,now:datetime) -> dict:
        now=_time(now)
        source=self.observation(kind,session_id,now=now)
        self._require_latest(source,now)
        amounts={key:exact_units(value,label,maximum=cap) for key,value,label,cap in (
            ('entry_units',entry_usdt,'Entrada USDT',1000),('budget_units',budget_usdt,'Presupuesto USDT',10000),
            ('exposure_units',exposure_usdt,'Exposición USDT',10000),('loss_units',loss_usdt,'Pérdida USDT',10000),
            ('wbnb_quota_units',wbnb_quota,'Cuota WBNB',1000000))}
        if amounts['entry_units']>min(amounts['budget_units'],amounts['exposure_units']):
            raise DexExecutorError('La entrada supera el presupuesto o la exposición')
        dca=None
        if kind=='DCA':
            monthly=exact_units(monthly_usdt,'Límite mensual USDT',maximum=10000)
            rule=source.definition
            rule_monthly=exact_units(rule['monthly_limit'],'Límite mensual DCA probado',maximum=10**30)
            if dca_signal_window_key(rule):
                for tier in rule['dips']:
                    exact_units(tier['amount'],'Extra DCA probado',maximum=10**30)
            dca={'monthly_units':str(monthly),'effective_monthly_units':str(min(monthly,rule_monthly)),
                 'frequency':rule['frequency'],'rule_amount_units':str(exact_units(rule['amount'],'Compra DCA probada',maximum=10**30)),
                 'reference_mode':rule.get('reference_mode','initial')}
        elif monthly_usdt is not None:raise DexExecutorError('El límite mensual corresponde a un bot DCA')
        result={'protocol':'orbis.dex.bot.preparation.v1','source_kind':kind,'source':source.binding(),
            'definition':source.definition,'observation_digest':source.observation_digest,'candle_t':source.candle_t,
            'signal_market':'MEXC · BNB/USDT','execution_market':'Uniswap v3 · USDT/WBNB · BNB Chain',
            'chain_id':'0x38','amounts':{key:str(value) for key,value in amounts.items()},'dca':dca,
            'allow_buy':True,'allow_sell':kind=='STRATEGY','created_at':now.isoformat(),
            'expires_at':(now+timedelta(seconds=60)).isoformat(),'preparation_only':True,'automatic_execution':False,
            'pending':['EXECUTOR','POLICY','TOKEN_ALLOWANCES','OPERATOR_GAS','OPERATOR_SESSION','WORKER']}
        result['preparation_digest']=content_hash(result)
        # Locking PAPER during verification invalidates the result as well.
        self._repository(kind)
        return result

    def researched_preview(self, *, research_validation_id:str|None, **parameters) -> dict:
        if research_validation_id is None:raise DexExecutorError('Selecciona una reserva fuera de muestra')
        result=self.preview(**parameters)
        checked=self.check_research(parameters['kind'],result['source'],result['candle_t'],research_validation_id)
        checked.assert_current()
        self._repository(parameters['kind'])
        result.update(protocol='orbis.dex.bot.preparation.v2',research_evidence=checked.evidence.exported())
        result['preparation_digest']=content_hash({k:v for k,v in result.items() if k!='preparation_digest'})
        return result

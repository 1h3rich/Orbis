"""Un único exchange/wallet ficticios para ingreso → bot → retirada.

Los clientes de ORBIS y su persistencia son reales; ninguna dependencia de
este escenario llama a un exchange, nodo o wallet del usuario.
"""

from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal,localcontext
from importlib import import_module
from threading import current_thread
from urllib.parse import parse_qs,urlparse
from uuid import UUID

from fastapi import FastAPI

from app.live_execution.mexc_bot_supervisor import MexcBotSupervisor
from app.live_execution.mexc_withdrawal_transport import MexcWithdrawalClient
from app.portfolio.api import create_real_router
from app.portfolio.mexc_withdrawal_journal import amount_text
from tests.mexc_bot_fixture import make_environment,advance_signal
from tests.test_mexc_execution_api import Probe
from tests.test_wallet_funding import CONTRACT,DESTINATION
from tests.test_wallet_funding_credit import evidence as funding_evidence,snapshot as funding_snapshot
from tests.test_mexc_withdrawal import source as withdrawal_source
from tests.test_mexc_withdrawal_receipt import evidence as withdrawal_evidence,snapshot as withdrawal_snapshot


DEPOSIT_HASH = "0x"+"b"*64
WITHDRAWAL_HASH = "0x"+"e"*64
AMOUNT = "40.123456789012345678"


class Roundtrip:
    def __init__(self,directory,monkeypatch=None):
        self.env = make_environment(directory,monkeypatch)
        self.clock,self.venue = self.env["clock"],self.env["venue"]
        self.wallet = None
        self.wallet_usdt,self.wallet_bnb_wei = Decimal("100"),10**18
        self.wallet_sends,self.withdrawals,self.workers = [],[],[]
        self.external_deposits = []
        self.funding_rpc,self.withdrawal_rpc = None,None
        self.funding_credited,self.withdrawal_completed = False,False
        self.withdrawal_timeout = False
        self.quote = {"bid":"65000","ask":"65001"}
        self.venue.quote_get = self.quote_get
        self.venue.evm_token_deposit_destination = self.deposit_destination
        self.venue.evm_token_contracts_by_direction = self.token_contracts
        self.venue.bsc_withdrawal_route = self.withdrawal_route
        self.venue.transfers_recent = self.transfers
        self._install_clock(monkeypatch)

    def _install_clock(self,monkeypatch):
        scenario = self
        class ScenarioDatetime(datetime):
            @classmethod
            def now(cls,tz=None):
                value = scenario.clock()
                return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
        # Sólo este proceso de pruebas: todas las fechas comparten el avance
        # de velas. Incluye recorded_at; no se relajan guardas de producción.
        for name in ("app.portfolio.api","app.portfolio.repository","app.portfolio.mexc_parity","app.portfolio.valuation",
                     "app.portfolio.wallet_funding_api","app.portfolio.mexc_withdrawal_api",
                     "app.live_execution.mexc_withdrawal","app.live_execution.mexc_withdrawal_reconciliation",
                     "app.live_execution.mexc_order_lifecycle"):
            module = import_module(name)
            if monkeypatch is None:
                module.datetime = ScenarioDatetime
            else:
                monkeypatch.setattr(module,"datetime",ScenarioDatetime)
        def public_quote(*_,**__):
            return {"price":self.quote["bid"],"source":"mexc-roundtrip-fixture",
                    "as_of":self.clock().isoformat(),"stale":False}
        module = import_module("app.portfolio.api")
        if monkeypatch is None:
            module.get_configured_price_with_meta = public_quote
        else:
            monkeypatch.setattr(module,"get_configured_price_with_meta",public_quote)

    def quote_get(self,symbol):
        result={"symbol":symbol,"closeTime":int(self.clock().timestamp()*1000),
                "bidPrice":self.quote["bid"],"askPrice":self.quote["ask"],"lastPrice":self.quote["bid"]}
        if getattr(self,'clock_shift_armed',False) and current_thread().name.startswith('orbis-mexc-bot-'):
            from tests.mexc_bot_fixture import advance_market
            self.clock_shift_armed=False
            advance_market(self.env)
        return result

    def deposit_destination(self,asset,*,chain_id):
        assert asset == "USDT" and chain_id == "0x38"
        return {"source":"MEXC_SPOT_PRIVATE_READONLY","network":"BNB_SMART_CHAIN_MAINNET","asset":"USDT",
                "token_contract":CONTRACT,"deposit_address":DESTINATION,"observed_at":self.clock().isoformat(),
                "stored":False,"transfer_sent":False}

    def token_contracts(self,*,chain_id):
        assert chain_id == "0x38"
        return {"deposit":{"USDT":CONTRACT},"withdrawal":{"USDT":CONTRACT}}

    def withdrawal_route(self,address):
        assert self.wallet is not None and address.lower() == self.wallet.lower()
        return withdrawal_source(self.clock())["route"]

    def wallet_send(self,transaction):
        assert not self.wallet_sends and transaction["chainId"] == "0x38" and transaction["value"] == "0x0"
        assert transaction["to"].lower() == CONTRACT and transaction["data"].startswith("0xa9059cbb")
        assert "0x"+transaction["data"][34:74].lower() == DESTINATION
        self.wallet = transaction["from"].lower()
        units = int(transaction["data"][74:],16)
        amount = Decimal(amount_text(units,18))
        gas_fee = 60000*int(transaction["gasPrice"],16)
        assert self.wallet_usdt >= amount and self.wallet_bnb_wei >= gas_fee
        with localcontext() as context:
            context.prec = 300
            self.wallet_usdt -= amount
        self.wallet_bnb_wei -= gas_fee
        at = self.clock()
        self.wallet_sends.append({"transaction":deepcopy(transaction),"amount":str(amount),"at":at})
        self.funding_rpc = funding_evidence({"transaction":transaction,"amount_units":str(units)},at,
            wallet=self.wallet,destination=DESTINATION,tx_hash=DEPOSIT_HASH)
        self.funding_rpc["block"]["transactions"] = [DEPOSIT_HASH]
        return {"hash":DEPOSIT_HASH,"evidence":deepcopy(self.funding_rpc)}

    def credit_funding(self):
        assert self.wallet_sends
        if not self.funding_credited:
            with localcontext() as context:
                context.prec = 300
                self.venue.quote = str(Decimal(self.venue.quote)+Decimal(self.wallet_sends[0]["amount"]))
            self.funding_credited = True

    def transfers(self,*,days,include_source_ids=False,include_withdrawal_evm=False):
        now = self.clock()
        deposits,withdrawals = [],[]
        if self.wallet_sends:
            item = self.wallet_sends[0]
            deposits = funding_snapshot(item["at"],amount=item["amount"],status=5 if self.funding_credited else 1,
                tx_hash=DEPOSIT_HASH)["deposits"]
        deposits.extend(dict(row) for row in self.external_deposits
                        if int((now-timedelta(days=days)).timestamp()*1000) <= row["time_ms"])
        for item in self.withdrawals:
            withdrawals.extend(withdrawal_snapshot({"id":item["id"]},item["at"],observed=now,
                status=7 if self.withdrawal_completed else 4,amount=item["amount"],
                fee="0.01" if self.withdrawal_completed else "0",chain=WITHDRAWAL_HASH if self.withdrawal_completed else None,
                destination=item["address"],external=item["external_id"])["withdrawals"])
        if not include_withdrawal_evm:
            for row in withdrawals:
                row.pop("request_identity", None)
                row.pop("chain_identity", None)
        if not include_source_ids:
            private = {"source_identity","destination_identity","request_identity","chain_identity"}
            deposits = [{key:value for key,value in item.items() if key not in private} for item in deposits]
            withdrawals = [{key:value for key,value in item.items() if key not in private} for item in withdrawals]
        return {"source":"MEXC_SPOT_PRIVATE_READONLY","accounting":"OBSERVED_NOT_IMPORTED",
                "coverage":"RECENT_WINDOW_BELOW_PAGE_LIMIT","start_ms":int((now-timedelta(days=days)).timestamp()*1000),
                "end_ms":int(now.timestamp()*1000),"observed_at":now.isoformat(),"deposits":deposits,"withdrawals":withdrawals}

    def withdrawal_client(self,key,secret):
        def get(url,headers):
            if urlparse(url).path == "/api/v3/apiKeyInfo":
                return {"status":"VALID","permissions":"SPOT_ACCOUNT_READ,SPOT_WITHDRAW_READ,SPOT_WITHDRAW_WRITE","apikey":key}
            assert urlparse(url).path == "/api/v3/uid"
            return {"uid":self.venue.uid if key == "fixture-withdraw" else "987654321"}
        def post(url,body,headers):
            assert urlparse(url).path == "/api/v3/capital/withdraw" and not self.withdrawals
            values = parse_qs(body)
            amount = values["amount"][0]
            assert values["address"][0] == self.wallet and values["netWork"] == ["BSC"]
            assert values["contractAddress"] == [CONTRACT]
            with localcontext() as context:
                context.prec = 300
                assert Decimal(self.venue.quote) >= Decimal(amount)
                self.venue.quote = str(Decimal(self.venue.quote)-Decimal(amount))
            external_id = "fixture-roundtrip-withdrawal"
            self.withdrawals.append({"id":str(UUID(values["withdrawOrderId"][0])),"external_id":external_id,
                                     "amount":amount,"address":self.wallet,"at":self.clock()})
            if self.withdrawal_timeout:
                raise RuntimeError("fixture accepted withdrawal with lost reply")
            return {"id":external_id}
        return MexcWithdrawalClient(key,secret,http_get=get,http_post=post,
                                    clock_ms=lambda:int(self.clock().timestamp()*1000))

    def complete_withdrawal(self):
        assert len(self.withdrawals) == 1
        item = self.withdrawals[0]
        with localcontext() as context:
            context.prec = 300
            net = Decimal(item["amount"])-Decimal("0.01")
            if not self.withdrawal_completed:
                self.wallet_usdt += net
        self.withdrawal_completed = True
        rpc = withdrawal_evidence(self.clock(),received=str(net),recipient=self.wallet,chain=WITHDRAWAL_HASH)
        height,block_hash = "0x2345","0x"+"d"*64
        for value in (rpc["transaction"],rpc["receipt"],*rpc["receipt"]["logs"]):
            value.update(blockNumber=height,blockHash=block_hash)
        for value in (rpc["block"],rpc["finalized"]): value.update(number=height,hash=block_hash)
        rpc["block"]["transactions"] = [WITHDRAWAL_HASH]
        self.withdrawal_rpc = rpc
        return deepcopy(rpc)

    def advance(self):
        return advance_signal(self.env,workers=self.workers)

    def target(self):
        self.quote.update(bid="72000",ask="72001")

    def make_app(self, *, manual=False, funding_nonce=False, withdrawal_bnb=False):
        assert not (funding_nonce and withdrawal_bnb)
        def factory(*args,**kwargs):
            worker = MexcBotSupervisor(*args,interval_seconds=0.1,**kwargs)
            self.workers.append(worker)
            return worker
        env = self.env
        app = FastAPI()
        # La valoración usa el precio controlado anterior: nunca abrir la
        # base de mercado por defecto ni escribir caché del usuario.
        from app.database.database import get_db
        app.dependency_overrides[get_db] = lambda:None
        origins = {} if manual else {"lab_path":env["lab"].path,"spot_paper":env["paper"]}
        rpc_factory = None
        if funding_nonce:
            from tests.test_wallet_funding_api import FundingNonceRpc
            rpc_factory = lambda:FundingNonceRpc(self.funding_rpc)
        if withdrawal_bnb:
            from tests.test_mexc_withdrawal_api import WithdrawalBnbRpc
            rpc_factory = lambda:WithdrawalBnbRpc(self.withdrawal_rpc)
        from tests.mexc_cancel_fixture import controlled_view
        app.include_router(create_real_router(env["vault"].path,**origins,
            mexc_factory=lambda key,secret:controlled_view(self.venue,key,secret),mexc_trade_factory=Probe,mexc_live_factory=lambda *_:env["trader"],
            mexc_withdrawal_factory=self.withdrawal_client,mexc_quote_get=self.venue.quote_get,
            mexc_rules_fetch=lambda symbol,now:self.venue.rules_fetch(symbol,now),
            mexc_bot_clock=self.clock,mexc_bot_supervisor_factory=factory,dex_rpc_factory=rpc_factory))
        return app

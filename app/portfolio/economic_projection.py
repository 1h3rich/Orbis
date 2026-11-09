"""Pure, versioned economic ordering with intact source events.

Claims must come from authenticated receipt/order/booking ports. This module
does not create those claims, persist results, or grant financial authority.
It propagates derived transfer cost through the complete account closure.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from decimal import Context,Decimal,ROUND_HALF_EVEN,localcontext
import heapq,re
from typing import Literal,Mapping,Sequence
from types import MappingProxyType
from uuid import UUID

from app.portfolio.domain import (
    CALC_PRECISION,AccountSnapshot,EventInput,PortfolioError,PortfolioEvent,
    apply_portfolio_event,utc_datetime,
)


ENGINE='portfolio-economic-order-1.0'
GAS_ENGINE='portfolio-economic-order-2.0'
EXTERNAL_ENGINE='portfolio-economic-order-3.0'
GAS_SOURCE_KINDS=frozenset(('WALLET_SWAP','OPERATOR','CALL_RESOLUTION','DEPLOYMENT','CONTROL',
    'GAS_FUNDING','GAS_RETURN','DEPLOYMENT_RESOLUTION','NATIVE_RESOLUTION'))
PAIR_KINDS=frozenset(('TRANSFER_OUT','TRANSFER_IN','CASH_TRANSFER_OUT','CASH_TRANSFER_IN'))


class ProjectionError(PortfolioError):pass


@dataclass(frozen=True,slots=True)
class ProjectionLimits:
    max_events: int=1000000
    max_claims: int=100000
    max_accounts: int=10000
    max_edges: int=2000000

    def __post_init__(self):
        if any(type(value) is not int or value<1 for value in
                (self.max_events,self.max_claims,self.max_accounts,self.max_edges)):
            raise ProjectionError('Límites de proyección inválidos')


def _uuid(value: str) -> str:
    try:
        if not isinstance(value,str) or str(UUID(value))!=value:raise ValueError
    except (ValueError,TypeError,AttributeError):raise ProjectionError('Identidad de proyección inválida') from None
    return value


@dataclass(frozen=True,slots=True)
class _ChainCoordinates:
    source_kind: str
    source_id: str
    order_record_id: str
    order_record_digest: str
    context_account_id: str
    account_id: str
    block_number: int
    transaction_index: int
    block_at: datetime
    block_hash: str
    block_transactions_ref: str
    block_transaction_count: int
    signer_digest: str
    nonce: int
    def __post_init__(self):
        for value in (self.source_id,self.order_record_id,self.context_account_id,self.account_id):_uuid(value)
        for value in (self.order_record_digest,self.signer_digest,self.block_transactions_ref):
            if not isinstance(value,str) or re.fullmatch(r'[a-f0-9]{64}',value) is None:raise ProjectionError('Referencia de proyección inválida')
        if not isinstance(self.block_hash,str) or re.fullmatch(r'0x[a-f0-9]{64}',self.block_hash) is None:
            raise ProjectionError('Identidad de bloque inválida')
        if (type(self.block_number) is not int or not 0<self.block_number<2**256
                or type(self.block_transaction_count) is not int or not 1<=self.block_transaction_count<=100000
                or type(self.transaction_index) is not int or not 0<=self.transaction_index<self.block_transaction_count
                or type(self.nonce) is not int or not 0<=self.nonce<2**64):
            raise ProjectionError('Coordenadas o gastos de proyección inválidos')
        object.__setattr__(self,'block_at',utc_datetime(self.block_at))

    @property
    def node_id(self) -> str:return 'chain:'+self.order_record_id


def _gas_members(values: tuple[str,...],maximum: int) -> None:
    if type(values)is not tuple or not 1<=len(values)<=maximum:
        raise ProjectionError('Coordenadas o gastos de proyección inválidos')
    for value in values:_uuid(value)
    if len(set(values))!=len(values):raise ProjectionError('La transacción repite un evento')


@dataclass(frozen=True,slots=True)
class ChainOrderClaim(_ChainCoordinates):
    source_kind: Literal['WALLET_SWAP','OPERATOR','CALL_RESOLUTION']
    trade_event_id: str
    gas_event_ids: tuple[str,...]

    def __post_init__(self):
        if self.source_kind not in ('WALLET_SWAP','OPERATOR','CALL_RESOLUTION'):
            raise ProjectionError('Origen de proyección DEX inválido')
        _ChainCoordinates.__post_init__(self);_uuid(self.trade_event_id);_gas_members(self.gas_event_ids,99)
        if self.trade_event_id in self.gas_event_ids:raise ProjectionError('La transacción repite un evento')

    @property
    def event_ids(self) -> tuple[str,...]:return (self.trade_event_id,*self.gas_event_ids)


@dataclass(frozen=True,slots=True)
class GasOrderClaim(_ChainCoordinates):
    """Authenticated native fees, without inventing a trade or external flow.

    The source port must separately authenticate whether its original call
    executed, reverted or was cancelled. A principal transfer is never a fee.
    """
    gas_event_ids: tuple[str,...]

    def __post_init__(self):
        if not isinstance(self.source_kind,str) or self.source_kind not in GAS_SOURCE_KINDS:raise ProjectionError('Origen de gas DEX inválido')
        _ChainCoordinates.__post_init__(self);_gas_members(self.gas_event_ids,100)

    @property
    def trade_event_id(self) -> None:return None

    @property
    def event_ids(self) -> tuple[str,...]:return self.gas_event_ids


@dataclass(frozen=True,slots=True)
class ExternalOrderMember:
    event_id: str
    account_id: str
    role: Literal['EFFECT','GAS']

    def __post_init__(self):
        _uuid(self.event_id);_uuid(self.account_id)
        if self.role not in ('EFFECT','GAS'):raise ProjectionError('Miembro externo inválido')


@dataclass(frozen=True,slots=True)
class ExternalOrderClaim(_ChainCoordinates):
    """A separately reviewed external receipt, possibly spanning owned accounts.

    A source port must authenticate every binding, declaration and original.
    This class supplies chronology, never proof of a strategy fill. Native gas
    precedes declared effects; both legs of an owned transfer stay in this node.
    """
    members: tuple[ExternalOrderMember,...]

    def __post_init__(self):
        _ChainCoordinates.__post_init__(self)
        if (self.source_kind!='EXTERNAL_OBSERVATION' or type(self.members)is not tuple
                or not 0<=len(self.members)<=8192
                or any(not isinstance(item,ExternalOrderMember)for item in self.members)
                or len({item.event_id for item in self.members})!=len(self.members)):
            raise ProjectionError('Prueba externa inválida o con miembros repetidos')
        gas=False
        for member in reversed(self.members):
            if member.role=='GAS':gas=True
            elif gas:raise ProjectionError('El gas externo debe preceder a sus efectos')
        if len(self.gas_event_ids)>100:raise ProjectionError('Demasiados gastos de gas externos')

    @property
    def event_ids(self) -> tuple[str,...]:return tuple(member.event_id for member in self.members)

    @property
    def gas_event_ids(self) -> tuple[str,...]:return tuple(member.event_id for member in self.members if member.role=='GAS')

    @property
    def trade_event_id(self) -> None:return None


OrderClaim=ChainOrderClaim|GasOrderClaim|ExternalOrderClaim


@dataclass(frozen=True,slots=True)
class TieOrder:
    account_id: str
    at: datetime
    nodes: tuple[str,...]

    def __post_init__(self):
        _uuid(self.account_id);object.__setattr__(self,'at',utc_datetime(self.at))
        if type(self.nodes) is not tuple or not self.nodes or any(not isinstance(value,str) for value in self.nodes):
            raise ProjectionError('Orden manual de empate inválido')


@dataclass(frozen=True,slots=True)
class ProjectedEvent:
    original: PortfolioEvent
    accounting_input: EventInput
    ordinal: int
    node_id: str
    order_record_id: str|None
    transfer_basis_derived: bool

    @property
    def accounting_at(self) -> datetime:return self.accounting_input.occurred_at


@dataclass(frozen=True,slots=True)
class ProjectionResult:
    engine: str
    events: tuple[ProjectedEvent,...]
    snapshots: dict[str,AccountSnapshot]
    claimed_transactions: int
    adjusted_transfer_legs: int


@dataclass(frozen=True,slots=True)
class ScheduledNode:
    id: str
    account_id: str
    at: datetime
    members: tuple[str,...]
    order_record_id: str|None
    priority: tuple[datetime,str]


@dataclass(frozen=True,slots=True)
class ProjectionSchedule:
    originals: Mapping[str,PortfolioEvent]
    nodes: tuple[ScheduledNode,...]
    accounts: tuple[str,...]
    claimed_transactions: int


def _pairs(events: dict[str,PortfolioEvent]) -> dict[str,tuple[PortfolioEvent,PortfolioEvent]]:
    grouped=defaultdict(list)
    for event in events.values():
        if event.input.kind in PAIR_KINDS:grouped[event.request_id].append(event)
    result={}
    for request,values in grouped.items():
        if len(values)!=2:raise ProjectionError('Falta una cuenta o un lado de un traslado pareado')
        kinds={event.input.kind:event for event in values}
        asset_pair=set(kinds)=={'TRANSFER_OUT','TRANSFER_IN'}
        if not asset_pair and set(kinds)!={'CASH_TRANSFER_OUT','CASH_TRANSFER_IN'}:
            raise ProjectionError('El traslado pareado mezcla clases de movimiento')
        outgoing,incoming=(kinds['TRANSFER_OUT'],kinds['TRANSFER_IN']) if asset_pair else (kinds['CASH_TRANSFER_OUT'],kinds['CASH_TRANSFER_IN'])
        left,right=outgoing.input,incoming.input
        with localcontext(Context(prec=CALC_PRECISION,rounding=ROUND_HALF_EVEN)):
            if (outgoing.account_id==incoming.account_id or left.asset!=right.asset
                    or left.quote_currency!=right.quote_currency or left.occurred_at!=right.occurred_at
                    or left.note!=right.note or left.gross_amount!=right.gross_amount or right.fee!=0
                    or (left.quantity-left.fee!=right.quantity if asset_pair else left.amount!=right.amount)):
                raise ProjectionError('Los datos originales del traslado no son coherentes')
        result[request]=(outgoing,incoming)
    return result


def schedule_events(events: Sequence[PortfolioEvent],claims: Sequence[OrderClaim],*,
                   tie_orders: Sequence[TieOrder]=(),prefix_orders: Sequence[TieOrder]=(),
                   limits: ProjectionLimits=ProjectionLimits()) -> ProjectionSchedule:
    """Topological chronology without computing balances or modifying originals.

    Default manual ties retain their relative recorded order per account.
    A full explicit tie permutation can replace that assumption, while chain
    and paired-transfer constraints remain mandatory. Cycles fail closed.
    """
    if (not isinstance(limits,ProjectionLimits) or not isinstance(events,Sequence) or not isinstance(claims,Sequence)
            or not 0<=len(events)<=limits.max_events or not 0<len(claims)<=limits.max_claims
            or not events and not all(isinstance(claim,ExternalOrderClaim)and not claim.members for claim in claims)):
        raise ProjectionError('El lote de proyección está vacío o supera sus límites')
    originals={};accounts=set()
    for event in events:
        if (not isinstance(event,PortfolioEvent) or not isinstance(event.input,EventInput)
                or event.accounting is not None):raise ProjectionError('La proyección necesita asientos originales sin contexto derivado')
        for value in (event.id,event.account_id,event.request_id):_uuid(value)
        utc_datetime(event.recorded_at)
        if event.id in originals:raise ProjectionError('El libro repite una identidad de evento')
        originals[event.id]=event;accounts.add(event.account_id)
    accounts.update(claim.account_id for claim in claims if isinstance(claim,ExternalOrderClaim))
    if len(accounts)>limits.max_accounts:raise ProjectionError('Demasiadas cuentas dependientes')
    pairs=_pairs(originals);claimed={};nodes={};event_node={};positions=set();records=set();sources=set();nonces=set();blocks={}
    if any(not isinstance(claim,(ChainOrderClaim,GasOrderClaim,ExternalOrderClaim)) for claim in claims):raise ProjectionError('Prueba de orden de proyección inválida')
    chain=sorted(claims,key=lambda claim:(claim.block_number,claim.transaction_index))
    previous=None
    for claim in chain:
        if not isinstance(claim,(ChainOrderClaim,GasOrderClaim,ExternalOrderClaim)):raise ProjectionError('Prueba de orden de proyección inválida')
        position=claim.block_number,claim.transaction_index;key=claim.source_kind,claim.source_id
        if position in positions or claim.order_record_id in records or key in sources or (claim.signer_digest,claim.nonce) in nonces:
            raise ProjectionError('La proyección repite una transacción o un nonce')
        positions.add(position);records.add(claim.order_record_id);sources.add(key);nonces.add((claim.signer_digest,claim.nonce))
        anchor=(claim.block_at,claim.block_hash,claim.block_transactions_ref,claim.block_transaction_count)
        if claim.block_number in blocks and blocks[claim.block_number]!=anchor:
            raise ProjectionError('Las pruebas de proyección no conservan el mismo bloque')
        blocks[claim.block_number]=anchor
        if previous is not None and previous.block_at>claim.block_at:raise ProjectionError('Los tiempos de bloque contradicen su orden')
        previous=claim
        external={member.event_id:member for member in claim.members}if isinstance(claim,ExternalOrderClaim)else None
        for identifier in claim.event_ids:
            event=originals.get(identifier)
            expected_account=external[identifier].account_id if external is not None else claim.account_id
            if event is None or identifier in claimed or event.account_id!=expected_account:
                raise ProjectionError('La transacción perdió o reutiliza un evento de su cuenta')
            item=event.input
            gas_member=(external[identifier].role=='GAS')if external is not None else identifier!=claim.trade_event_id
            if (abs(item.occurred_at-claim.block_at)>timedelta(minutes=2)
                    or external is None and identifier==claim.trade_event_id and (item.kind not in ('BUY','SELL') or item.asset!='WBNB' or item.quote_currency!='USDT' or item.fee!=0)
                    or gas_member and (item.kind!='FEE' or not (item.asset=='BNB' or item.asset is None and item.quote_currency=='BNB'))):
                raise ProjectionError('Los eventos no corresponden a una operación DEX y su gas nativo')
            claimed[identifier]=claim;event_node[identifier]=claim.node_id
        gas=tuple(sorted(claim.gas_event_ids,key=lambda identifier:(originals[identifier].recorded_at,identifier)))
        members=claim.event_ids if external is not None else (claim.trade_event_id,*gas)if isinstance(claim,ChainOrderClaim)else gas
        if external is not None:
            positions_by_member={value:index for index,value in enumerate(members)}
            requests={originals[value].request_id for value in external if originals[value].input.kind in PAIR_KINDS}
            for request in requests:
                outgoing,incoming=pairs[request]
                if (outgoing.id not in external or incoming.id not in external
                        or positions_by_member[outgoing.id]>=positions_by_member[incoming.id]):
                    raise ProjectionError('La transacción externa necesita ambos lados del traslado, salida antes de entrada')
        priority=min(((utc_datetime(originals[identifier].recorded_at),identifier) for identifier in members),
            default=(claim.block_at,claim.order_record_id))
        nodes[claim.node_id]=ScheduledNode(claim.node_id,claim.account_id,claim.block_at,members,claim.order_record_id,priority)
    signer_last={}
    for claim in chain:
        if claim.signer_digest in signer_last and claim.nonce<=signer_last[claim.signer_digest]:
            raise ProjectionError('La proyección contradice los nonces del emisor')
        signer_last[claim.signer_digest]=claim.nonce
    for identifier,event in originals.items():
        if identifier in claimed:continue
        node='event:'+identifier;event_node[identifier]=node
        nodes[node]=ScheduledNode(node,event.account_id,event.occurred_at,(identifier,),None,(utc_datetime(event.recorded_at),identifier))
    by_time=defaultdict(list);by_account_time=defaultdict(set)
    for node in nodes.values():
        by_time[node.at].append(node.id)
        for account in {originals[member].account_id for member in node.members}or{node.account_id}:
            by_account_time[account,node.at].add(node.id)
    adjacency={identifier:set() for identifier in nodes};degrees={identifier:0 for identifier in nodes};edge_count=0
    def edge(left: str,right: str):
        nonlocal edge_count
        if left==right:return
        if nodes[left].at>nodes[right].at:raise ProjectionError('Una dependencia invierte fechas económicas')
        if right not in adjacency[left]:
            adjacency[left].add(right);degrees[right]+=1;edge_count+=1
            if edge_count>limits.max_edges:raise ProjectionError('Demasiadas dependencias de proyección')
    for left,right in zip(chain,chain[1:]):edge(left.node_id,right.node_id)
    for outgoing,incoming in pairs.values():edge(event_node[outgoing.id],event_node[incoming.id])
    explicit={}
    # One external chain node may belong to several account/time groups.
    if len(tie_orders)>len(by_account_time):raise ProjectionError('Demasiados órdenes manuales')
    for order in tie_orders:
        if not isinstance(order,TieOrder):raise ProjectionError('Orden de empate inválido')
        key=order.account_id,order.at
        if (key in explicit or len(set(order.nodes))!=len(order.nodes)
                or set(order.nodes)!=by_account_time.get(key,set())):
            raise ProjectionError('El orden manual no cubre exactamente el empate de esa cuenta')
        explicit[key]=order.nodes
    prefixes={}
    if len(prefix_orders)>len(by_account_time):raise ProjectionError('Demasiados prefijos de orden')
    for order in prefix_orders:
        if not isinstance(order,TieOrder):raise ProjectionError('Prefijo de orden inválido')
        key=order.account_id,order.at
        if (key in prefixes or key in explicit or len(set(order.nodes))!=len(order.nodes)
                or not set(order.nodes)<=by_account_time.get(key,set())):
            raise ProjectionError('El prefijo no corresponde al orden aceptado de esa cuenta')
        prefixes[key]=order.nodes
    for key,identifiers in by_account_time.items():
        if key in explicit:ordered=explicit[key]
        elif key in prefixes:
            prefix=prefixes[key]
            for left,right in zip(prefix,prefix[1:]):edge(left,right)
            remaining=identifiers-set(prefix)
            for right in remaining:edge(prefix[-1],right)
            ordered=sorted((identifier for identifier in remaining if nodes[identifier].order_record_id is None),key=lambda identifier:nodes[identifier].priority)
        else:ordered=sorted((identifier for identifier in identifiers if nodes[identifier].order_record_id is None),key=lambda identifier:nodes[identifier].priority)
        for left,right in zip(ordered,ordered[1:]):edge(left,right)
    ordered=[]
    for at in sorted(by_time):
        queue=[(*nodes[identifier].priority,identifier) for identifier in by_time[at] if degrees[identifier]==0]
        heapq.heapify(queue);done=0
        while queue:
            _,_,identifier=heapq.heappop(queue);node=nodes[identifier];done+=1;ordered.append(node)
            for target in adjacency[identifier]:
                degrees[target]-=1
                if degrees[target]==0 and nodes[target].at==at:heapq.heappush(queue,(*nodes[target].priority,target))
        if done!=len(by_time[at]):raise ProjectionError('El orden económico contiene un ciclo; revisa las posiciones manuales')
    if sum(len(node.members) for node in ordered)!=len(events):raise ProjectionError('La proyección perdió eventos')
    return ProjectionSchedule(MappingProxyType(originals),tuple(ordered),tuple(sorted(accounts)),len(chain))


def _apply_scheduled_event(original: PortfolioEvent,node: ScheduledNode,
                           states: dict[str,AccountSnapshot],basis: dict[str,Decimal]) -> tuple[EventInput,bool]:
    """Shared financial step; the replay/planner owns the canonical Decimal context."""
    item=original.input
    effective=replace(item,occurred_at=node.at) if node.order_record_id is not None else item
    derived=False
    if item.kind=='TRANSFER_OUT':
        position=states[original.account_id].positions.get((item.asset,item.quote_currency))
        if position is None or item.quantity>position.quantity:
            raise ProjectionError('El traslado supera el inventario económico de origen')
        cost=position.cost_basis if item.quantity==position.quantity else position.average_cost*item.quantity
        basis[original.request_id]=cost;effective=replace(effective,gross_override=cost);derived=True
    elif item.kind=='TRANSFER_IN':
        if original.request_id not in basis:raise ProjectionError('El traslado no tiene coste de origen calculado')
        effective=replace(effective,gross_override=basis[original.request_id]);derived=True
    apply_portfolio_event(states[original.account_id],replace(original,input=effective))
    return effective,derived


def project_events(events: Sequence[PortfolioEvent],claims: Sequence[OrderClaim],*,
                   tie_orders: Sequence[TieOrder]=(),prefix_orders: Sequence[TieOrder]=(),
                   limits: ProjectionLimits=ProjectionLimits()) -> ProjectionResult:
    """Replay the shared schedule without changing source events or inputs."""
    scheduled=schedule_events(events,claims,tie_orders=tie_orders,prefix_orders=prefix_orders,limits=limits)
    external_nodes={claim.node_id for claim in claims if isinstance(claim,ExternalOrderClaim)}
    states={account:AccountSnapshot(account) for account in scheduled.accounts};output=[];basis={};adjusted=0
    with localcontext(Context(prec=CALC_PRECISION,rounding=ROUND_HALF_EVEN)):
        for node in scheduled.nodes:
            for member in node.members:
                original=scheduled.originals[member]
                effective,derived=_apply_scheduled_event(original,node,states,basis)
                if node.id in external_nodes:
                    cash_debit=(effective.kind in ('BUY','WITHDRAWAL','CASH_TRANSFER_OUT')
                        or effective.kind=='FEE'and effective.asset is None
                        or effective.kind=='SELL'and effective.fee_asset!=effective.asset and effective.fee>effective.gross_amount)
                    if cash_debit and states[original.account_id].cash[effective.quote_currency]<0:
                        raise ProjectionError('El efecto externo supera la caja contable; registra primero su financiación real')
                output.append(ProjectedEvent(original,effective,len(output),node.id,node.order_record_id,derived))
                if derived and effective.gross_amount!=original.input.gross_amount:adjusted+=1
    engine=EXTERNAL_ENGINE if external_nodes else GAS_ENGINE if any(isinstance(claim,GasOrderClaim)for claim in claims)else ENGINE
    return ProjectionResult(engine,tuple(output),states,scheduled.claimed_transactions,adjusted)

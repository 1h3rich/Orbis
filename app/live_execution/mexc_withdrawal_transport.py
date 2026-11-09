"""Credencial puntual de retirada: permisos separados, UID y un único POST.

El coordinador debe consumir antes el permiso durable. Ningún error prueba
que no se recibió la solicitud; este transporte no implementa reintentos.
"""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import hmac
import re
import secrets
import time
from threading import Lock
from typing import Callable
from urllib.parse import urlencode
from uuid import UUID

import httpx

from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError


class MexcWithdrawalError(RuntimeError):
    """Mensaje saneado: nunca incluye claves, dirección, UID o cuerpo de red."""


@dataclass(frozen=True,slots=True)
class WithdrawalRequest:
    identifier: str
    address: str
    contract: str
    amount: str


def _http_post(url: str, body: str, headers: dict[str,str]) -> object:
    try:
        response = httpx.post(url,content=body,headers=headers,timeout=5,
                              follow_redirects=False,trust_env=False)
        if response.status_code != 200:
            raise ValueError
        return response.json()
    except Exception:
        raise MexcWithdrawalError("Resultado de retirada incierto; consultar MEXC, nunca reenviar") from None


class MexcWithdrawalClient:
    def __init__(self, api_key: str, secret_key: str, *, http_get: Callable | None = None,
                 http_post: Callable = _http_post, clock_ms: Callable | None = None):
        options = {"clock_ms":clock_ms} if clock_ms is not None else {}
        if http_get is not None:
            options["http_get"] = http_get
        try:
            self._reader = MexcReadOnlyClient(api_key,secret_key,**options)
        except MexcReadOnlyError:
            raise MexcWithdrawalError("Formato de credencial de retirada inválido") from None
        self._api_key,self._secret = api_key,secret_key
        self._post,self._clock = http_post,clock_ms or (lambda:time.time_ns()//1_000_000)
        self._paired_until = 0.0
        self._used = False
        self._attempt_lock = Lock()

    def __repr__(self):
        return "MexcWithdrawalClient(<redacted>)"

    def pair_view_account(self, view_uid: str) -> None:
        try:
            data = self._reader._signed_get("/api/v3/apiKeyInfo",{"accessKey":self._api_key})
            raw = data.get("permissions")
            permissions = set(part.strip() for part in raw.split(",")) if isinstance(raw,str) else set(raw) if isinstance(raw,list) and all(isinstance(p,str) for p in raw) else set()
            echoed = data.get("apikey",data.get("accessKey",self._api_key))
            validity = data.get("remainingValidity",-999)
            uid = self._reader._signed_get("/api/v3/uid").get("uid")
            if (data.get("status") != "VALID" or permissions != {"SPOT_ACCOUNT_READ","SPOT_WITHDRAW_READ","SPOT_WITHDRAW_WRITE"}
                    or not isinstance(echoed,str) or not secrets.compare_digest(echoed,self._api_key)
                    or type(validity) is not int or validity != -999 and validity <= 0
                    or not isinstance(uid,str) or not re.fullmatch(r"[1-9][0-9]{0,31}",uid)
                    or not isinstance(view_uid,str) or not secrets.compare_digest(uid,view_uid)):
                raise ValueError
        except Exception:
            raise MexcWithdrawalError("La clave requiere sólo Account Read, Withdraw Read/Write y la misma cuenta View; nunca Trade") from None
        self._paired_until = time.monotonic()+30

    def withdraw_once(self, request: WithdrawalRequest) -> str:
        with self._attempt_lock:
            if self._used or time.monotonic() >= self._paired_until or not isinstance(request,WithdrawalRequest):
                raise MexcWithdrawalError("Permiso puntual de retirada no vigente o ya utilizado")
            self._used = True
        try:
            identifier = UUID(request.identifier).hex
            if (any(not isinstance(value,str) or not re.fullmatch(r"0x[0-9a-f]{40}",value)
                    or int(value[2:],16) == 0 for value in (request.address,request.contract))
                    or not isinstance(request.amount,str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?",request.amount)
                    or len(request.amount)>100 or Decimal(request.amount) <= 0):
                raise ValueError
            timestamp = self._clock()
            if type(timestamp) is not int or timestamp <= 0:
                raise ValueError
        except (TypeError,ValueError,AttributeError):
            raise MexcWithdrawalError("Términos puntuales de retirada inválidos") from None
        body = urlencode([("coin","USDT"),("withdrawOrderId",identifier),("netWork","BSC"),
            ("contractAddress",request.contract),("address",request.address),("amount",request.amount),
            ("recvWindow","5000"),("timestamp",str(timestamp))])
        signature = hmac.new(self._secret.encode("ascii"),body.encode("ascii"),hashlib.sha256).hexdigest()
        try:
            result = self._post("https://api.mexc.com/api/v3/capital/withdraw",body+"&signature="+signature,
                {"X-MEXC-APIKEY":self._api_key,"Content-Type":"application/json"})
            external = result.get("id") if isinstance(result,dict) and "code" not in result else None
            if not isinstance(external,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",external):
                raise ValueError
            return external
        except Exception:
            raise MexcWithdrawalError("Resultado de retirada incierto; consultar MEXC, nunca reenviar") from None

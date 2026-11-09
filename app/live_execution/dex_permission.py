"""Explicit duration and lifecycle of a DEX signing permission."""
from dataclasses import dataclass
from typing import Literal
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_versions import LONG_POLICY_OUTCOME as LONG_ENGINE_V6

MAX_PERMISSION_SECONDS=86400
LEGACY_PERMISSION_SECONDS=900
LONG_ENGINE_V1='orbis.dex.bot.long.v1'
LONG_ENGINE_V2='orbis.dex.bot.long.v2'
LONG_ENGINE_V3='orbis.dex.bot.long.v3'
LONG_ENGINE_V4='orbis.dex.bot.long.v4'
LONG_ENGINE_V5='orbis.dex.bot.long.v5'
PermissionMode=Literal['SESSION','OPERATIONAL']


@dataclass(frozen=True,slots=True)
class DexPermission:
    mode:PermissionMode
    requested_seconds:int

    def validate(self)->None:
        if (self.mode not in ('SESSION','OPERATIONAL') or type(self.requested_seconds) is not int
                or not 1<=self.requested_seconds<=MAX_PERMISSION_SECONDS):
            raise DexExecutorError('Modo o vigencia del permiso DEX no válido')

    def json(self)->dict:
        self.validate();return {'schema':'orbis.dex.permission.v1','mode':self.mode,'requested_seconds':self.requested_seconds}

    @classmethod
    def parse(cls,value:dict)->'DexPermission':
        if not isinstance(value,dict) or set(value)!={'schema','mode','requested_seconds'} or value['schema']!='orbis.dex.permission.v1':
            raise DexExecutorError('Permiso DEX archivado no válido')
        result=cls(value['mode'],value['requested_seconds']);result.validate();return result

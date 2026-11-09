#!/usr/bin/env python3
"""Captura privada de solo lectura de un FOK protegido y un IOC en GRVT Testnet."""

import argparse
from getpass import GetPassWarning, getpass
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.live_execution.grvt_testnet_market_probe import MarketProbeError, capture_market_probe
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient, GrvtTestnetOrderError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sub-account', required=True, help='Subcuenta aislada de GRVT Testnet')
    parser.add_argument('--parent', required=True, help='ID cliente de la entrada OTOCO de ORBIS')
    ioc = parser.add_mutually_exclusive_group()
    ioc.add_argument('--ioc-client-id', help='ID cliente de un IOC de la API')
    ioc.add_argument('--ioc-order-id', help='ID de exchange del IOC')
    parser.add_argument('--out', required=True, type=Path, help='Ruta nueva para la muestra privada')
    args = parser.parse_args(argv)
    try:
        if args.out.exists() or args.out.is_symlink():
            raise FileExistsError
        if not sys.stdin.isatty():
            raise GetPassWarning
        with warnings.catch_warnings():
            warnings.simplefilter('error', GetPassWarning)
            client = GrvtTestnetOrderClient(getpass('API View de GRVT Testnet: '))
        sample = capture_market_probe(client, args.sub_account, args.parent,
                                      ioc_client_id=args.ioc_client_id, ioc_order_id=args.ioc_order_id)
        payload = json.dumps(sample,ensure_ascii=True,indent=2) + '\n'
        with NamedTemporaryFile(mode='w',encoding='utf-8',dir=args.out.parent,delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(payload)
                output.close()
                os.link(temporary,args.out)
            finally:
                temporary.unlink()
    except (MarketProbeError, GrvtTestnetOrderError) as exc:
        print(str(exc),file=sys.stderr)
        return 1
    except (GetPassWarning, OSError, EOFError):
        print('La captura requiere una terminal interactiva, una subcuenta Testnet verificable y una ruta nueva.',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Ensayo aislado del horizonte de 30 días; exchange y reloj controlados.

Ejecutar como módulo. No forma parte de la colección rápida de pytest.
"""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from tests.mexc_continuity_fixture import make_dca_continuity_environment
from tests.test_mexc_bot_api import authorize
from tests.test_mexc_dca_bot import activate


def main(half_days:int=60):
    if type(half_days) is not int or not 6 <= half_days <= 60: raise ValueError("half_days: 6..60")
    with TemporaryDirectory(prefix="orbis-continuous-horizon-") as directory,MonkeyPatch.context() as patches:
        scenario = make_dca_continuity_environment(Path(directory),patches)
        with TestClient(scenario.make_app(driven=True)) as client:
            headers = authorize(client)
            response,url,_ = activate(client,scenario,headers,execution_mode="CONTINUOUS",
                confirmation="ACTIVAR_BOT_CONTINUO",continuous_until=(scenario.clock()+timedelta(days=30)).isoformat())
            assert response.status_code == 200,response.text
            original = response.json(); worker = scenario.workers[0]; deadline = worker._deadline
            assert worker.run_once()["action"] == "WAIT"
            longest = 0
            for half_day in range(2,half_days):
                # Observa después de entrar en la ventana de renovación, no
                # justo unos milisegundos ANTES y luego al borde de caducar.
                # En producción el trabajador vuelve cada cinco segundos.
                scenario.advance(1+half_day//2,seconds=(half_day%2)*43200+5+(half_day-2)*60)
                start = monotonic()
                try: result = worker.run_once()
                except Exception as error: raise AssertionError(f"Fallo en semidía {half_day}") from error
                longest = max(longest,monotonic()-start)
                assert result["action"] in ("WAIT","BUY"),result
                assert not result["paused"] and worker._deadline == deadline
                if half_day%10 == 0: print(f"Día {half_day//2}: sin pausa; ciclo máximo {longest:.2f}s",flush=True)
            final = client.get(url,headers=headers).json()[0]
            assert final["execution_permission"] == original["execution_permission"]
            assert final["renewals"] >= (50 if half_days == 60 else half_days-3) and len(scenario.workers) == 1
            assert Decimal(final["position"]["cost_quote"]) <= Decimal("100")
            start = monotonic()
            worker.vault.backup(Path(directory)/"horizon-backup.db")
            print(f"Concesión 30 días; {half_days/2:g} días de ventana: {final['renewals']} renovaciones, misma concesión/hebra, "
                  f"presupuesto conservado, copia verificada; ciclo máximo {longest:.2f}s; copia {monotonic()-start:.2f}s",flush=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--half-days",type=int,default=60,help="Ventana del ensayo; 60 conserva el horizonte completo")
    main(parser.parse_args().half_days)

"""Dashboard /ui: montaje estático, identidad intacta y humo PAPER.

* ``GET /ui/`` sirve el dashboard (200, html) sin tocar ``GET /`` (JSON),
  ``/docs`` ni ``/portfolio/buys``.
* El montaje usa ``check_dir=False``: registrar sobre una carpeta inexistente
  no debe romper el arranque.
* ``#/live`` ya no existe: una URL heredada vuelve a Inicio.
* Humo buy/sell con TestClient: doble request_id = 1 operación; venta sin
  posición devuelve 200 con ``executed:false`` y motivo literal; publicar
  precio manual cambia ``current_price`` tras refrescar; el patrimonio cumple
  ``equity == cash + positions`` con valores del servidor.
"""

import re
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.web.mount import register_ui

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_ui_serves_html_without_changing_identity(client):
    root = client.get("/")
    assert root.status_code == 200
    assert root.json() == {"name": "Orbis", "version": "0.1.0", "status": "running"}
    assert "text/html" not in root.headers.get("content-type", "")

    page = client.get("/ui/")
    assert page.status_code == 200
    assert "text/html" in page.headers.get("content-type", "")
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "no-referrer"
    assert '<script src="/ui/home-workspaces.js?v=2">' in page.text
    assert '<script src="/ui/market-alerts.js?v=1">' in page.text
    assert '<a href="#/alertas">Alertas</a>' in page.text
    assert '<script src="/ui/app.js?v=102">' in page.text
    assert '<script src="/ui/search.js?v=5">' in page.text
    assert '<script src="/ui/decimal.js?v=3">' in page.text
    assert '<script src="/ui/signal-frequency.js?v=2">' in page.text
    assert '<script src="/ui/spot-paper.js?v=37">' in page.text
    assert '<script src="/ui/dca.js?v=31">' in page.text
    assert '<script src="/ui/research-reference.js?v=1">' in page.text
    assert '<script src="/ui/perp.js?v=46">' in page.text
    assert '<link rel="stylesheet" href="/ui/app.css?v=134">' in page.text
    assert '<script src="/ui/portfolio-history.js?v=2">' in page.text
    assert '<script src="/ui/real.js?v=113">' in page.text
    assert '<script src="/ui/perp-real-review.js?v=2">' in page.text
    assert '<script src="/ui/grvt-mainnet.js?v=15">' in page.text
    assert '<script src="/ui/grvt-mainnet-limit.js?v=6">' in page.text
    assert '<script src="/ui/grvt-mainnet-chart.js?v=4">' in page.text
    assert '<script src="/ui/grvt-perp-review-evidence.js?v=1">' in page.text
    assert '<script src="/ui/grvt-mainnet-bots.js?v=9">' in page.text
    assert page.text.index("/ui/grvt-perp-review-evidence.js") < page.text.index("/ui/grvt-mainnet-bots.js")
    assert '<script src="/ui/grvt-bracket.js?v=17">' in page.text
    assert '<script src="/ui/grvt-bot.js?v=8">' in page.text
    assert '<script src="/ui/grvt-oco.js?v=1">' in page.text
    assert '<script src="/ui/grvt-testnet.js?v=1">' in page.text
    assert '<script src="/ui/mexc-trading.js?v=11">' in page.text
    assert '<script src="/ui/research-download.js?v=2"></script>' in page.text
    assert '<script src="/ui/real-fill-chart.js?v=4">' in page.text
    assert '<script src="/ui/dex-bot-chart.js?v=3">' in page.text
    assert '<script src="/ui/dex-receipt-order.js?v=3">' in page.text
    assert '<script src="/ui/economic-review.js?v=4">' in page.text
    assert '<script src="/ui/external-review.js?v=1">' in page.text
    assert '<script src="/ui/dex-receipt-booking.js?v=4">' in page.text
    assert '<script src="/ui/mexc-bot-chart.js?v=4">' in page.text
    assert '<script src="/ui/mexc-bots.js?v=24">' in page.text
    assert '<script src="/ui/wallet.js?v=35">' in page.text
    assert '<script src="/ui/wallet-dex-runs.js?v=18">' in page.text
    assert '<script src="/ui/wallet-dex-bot.js?v=14">' in page.text
    assert '<script src="/ui/wallet-dex-return.js?v=4">' in page.text
    assert '<script src="/ui/wallet-dex-cancellation.js?v=1">' in page.text
    assert page.text.index('/ui/wallet-dex-cancellation.js')<page.text.index('/ui/wallet-dex-return.js')
    assert '<script src="/ui/wallet-dex-gas.js?v=4">' in page.text
    assert '<script src="/ui/wallet-dex-funding-cancellation.js?v=1">' in page.text
    assert page.text.index('/ui/wallet-dex-funding-cancellation.js')<page.text.index('/ui/wallet-dex-gas.js')
    assert '<script src="/ui/deployment-resolution.js?v=6">' in page.text
    assert '<script src="/ui/wallet-dex-setup.js?v=6">' in page.text
    assert '<script src="/ui/wallet-dex-operator.js?v=3">' in page.text
    assert '<script src="/ui/wallet-swap.js?v=10">' in page.text
    assert '<script src="/ui/mexc-withdrawal.js?v=10">' in page.text
    assert '<script src="/ui/mexc-journey-report.js?v=2">' in page.text
    assert '<script src="/ui/mexc-direct-journeys.js?v=2">' in page.text
    assert '<script src="/ui/transfer-history-focus.js?v=1">' in page.text
    assert '<script src="/ui/wallet-funding.js?v=17">' in page.text
    assert page.text.index("/ui/mexc-journey-report.js") < page.text.index("/ui/wallet-funding.js")
    assert '<script src="/ui/market-chart-history.js?v=1">' in page.text
    assert '<script src="/ui/estudio.js?v=56">' in page.text
    assert '<script src="/ui/chart-range.js?v=9">' in page.text
    assert '<script src="/ui/lab-ai-archive.js?v=3">' in page.text
    assert '<script src="/ui/lab.js?v=70">' in page.text
    assert '<script src="/ui/perp-datasets.js?v=3">' in page.text
    assert '<script src="/ui/perp-rule-comparison.js?v=2">' in page.text
    assert '<script src="/ui/perp-run-comparison.js?v=1">' in page.text
    assert '<script src="/ui/perp-cost-comparison.js?v=1">' in page.text
    assert '<script src="/ui/perp-margin-comparison.js?v=1">' in page.text
    assert '<script src="/ui/perp-experiments.js?v=13">' in page.text
    assert '<script src="/ui/perp-validation.js?v=9">' in page.text
    assert '<script src="/ui/lab-parameter-map.js?v=1">' in page.text
    assert '<script src="/ui/perp-optimization.js?v=4">' in page.text
    assert '<script src="/ui/perp-observed.js?v=8">' in page.text
    assert '<script src="/ui/perp-gap-close.js?v=2">' in page.text
    assert page.text.index("/ui/perp-gap-close.js") < page.text.index("/ui/perp-observed.js")
    assert '<script src="/ui/storage-protection.js?v=2">' in page.text
    assert page.text.index("/ui/storage-protection.js") < page.text.index("/ui/app.js")
    assert page.text.index("/ui/perp-datasets.js") < page.text.index("/ui/lab.js")
    assert '<option value="bitstamp">Bitstamp</option>' in page.text
    # No se anuncian secciones de operativa real sin implementación.
    assert 'href="#/estudio"' in page.text
    assert 'href="#/live"' not in page.text
    assert '#/live' not in APP_JS
    assert 'id="toasts"' in page.text
    assert 'id="mode-badge"' not in page.text
    assert 'id="mode-live"' not in page.text
    assert page.text.count('href="#/operativa"') == 1
    assert 'href="#/portfolio" data-help="Saldo y posiciones ficticias' in page.text
    assert '>Patrimonio registrado</a>' in page.text
    assert "Trading real" not in page.text
    assert 'id="mode-paper"' not in page.text
    assert '<script src="/ui/app.js?v=102">' in page.text
    assert '<script src="/ui/manual-paper.js?v=1">' in page.text
    assert 'id="runtime-status"' in page.text
    assert '"#/portfolio"' in APP_JS and '"#/real"' in APP_JS
    assert '>Patrimonio registrado</a>' in page.text
    assert '>Simulación PAPER</a>' in page.text
    assert 'aria-label="Herramientas de simulación" hidden' in page.text
    assert 'href="#/spot-paper" data-help="Seguimiento de estrategias MEXC Spot con capital ficticio.">Spot</a>' in page.text
    assert 'href="#/perpetuos" data-help="Posiciones y estrategias GRVT con capital ficticio.">GRVT</a>' in page.text
    assert '<a href="#/real">Cartera real</a>' not in page.text
    assert 'Orbis · datos locales</span>' in page.text
    assert 'Cartera REAL · separada de PAPER' not in APP_JS
    assert 'no habilita compras ni ventas reales' in APP_JS

    bare = client.get("/ui/", follow_redirects=False)
    assert bare.status_code == 200
    redir = client.get("/ui", follow_redirects=False)
    assert redir.status_code in (301, 302, 303, 307, 308)
    assert redir.headers.get("location", "").endswith("/ui/")

    assert client.get("/docs").status_code == 200


def test_portfolio_buys_untouched(client, create_scenario):
    create_scenario()
    buys = client.get("/portfolio/buys?mode=PAPER")
    assert buys.status_code == 200
    assert isinstance(buys.json(), list)


def test_mount_survives_a_missing_static_dir(tmp_path):
    app = FastAPI()
    register_ui(app, static_dir=tmp_path / "no-such-dir")
    with TestClient(app) as local:
        assert local.get("/docs").status_code == 200


def test_legacy_live_route_is_not_a_fake_feature():
    assert '"#/live":' not in APP_JS
    assert "function showLive" not in APP_JS
    assert 'history.replaceState(null, "", location.pathname + location.search + h)' in APP_JS


def test_legacy_live_preference_cannot_switch_paper_operations():
    match = re.search(r"function orbisMode\(\)\s*\{(.*?)\n  \}", APP_JS, re.S)
    assert match is not None
    assert 'return "PAPER"' in match.group(1)
    assert "localStorage" not in match.group(1)


def test_js_uses_safe_uuid_and_same_origin_base():
    assert "crypto.getRandomValues" in APP_JS
    assert "randomUUID" not in APP_JS
    assert "window.ORBIS_API_BASE" in APP_JS
    assert "orbis_api_base" in APP_JS
    assert "Intl.NumberFormat" in APP_JS
    assert 's + "Z"' in APP_JS or '+\"Z\"' in APP_JS or '+ "Z"' in APP_JS
    code = re.sub(r"/\*.*?\*/", "", APP_JS, flags=re.S)
    assert "/paper/operations" in code  # el ledger PAPER sí se lee
    assert code.count("/operations") == code.count("/paper/operations")
    assert 'api("/operations' not in code


def _buy(client, **overrides):
    body = {
        "request_id": str(uuid4()),
        "asset": "BTC",
        "quote_currency": "EUR",
        "order_amount": "300",
        "price": "100000",
        "estimated_fee": "0",
    }
    body.update(overrides)
    return client.post("/paper/buy", json=body)


def test_double_submit_with_same_request_id_creates_one_operation(client, create_scenario):
    create_scenario()
    shared = str(uuid4())
    first = _buy(client, request_id=shared)
    second = _buy(client, request_id=shared)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["executed"] is True
    assert second.json()["operation_id"] == first.json()["operation_id"]
    assert len(client.get("/paper/operations").json()) == 1


def test_sell_without_position_reports_literal_reason(client, create_scenario):
    create_scenario()
    response = client.post("/paper/sell", json={
        "request_id": str(uuid4()),
        "asset": "BTC",
        "quote_currency": "EUR",
        "quantity": "0.001",
        "price": "110000",
        "fee": "0",
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["executed"] is False
    assert isinstance(payload["reason"], str) and payload["reason"]


def test_manual_price_changes_current_price_after_refresh(client, create_scenario, monkeypatch):
    # Fijar manual explícito: el defecto para demo local es kraken (con red).
    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "manual")
    create_scenario()
    bought = _buy(client)
    assert bought.status_code == 200

    published = client.post(
        "/paper/prices",
        json={"asset": "BTC", "quote_currency": "EUR", "price": "120000"},
    )
    assert published.status_code == 201

    portfolio = client.get("/paper/portfolio?quote_currency=EUR").json()
    [position] = portfolio["positions"]
    assert Decimal(str(position["current_price"])) == Decimal("120000")
    equity = Decimal(str(portfolio["equity"]))
    cash = Decimal(str(portfolio["cash_balance"]))
    positions_value = Decimal(str(portfolio["positions_value"]))
    assert abs(equity - (cash + positions_value)) < Decimal("0.000000001")


def test_unknown_quote_currency_is_empty_with_cta_shape(client):
    response = client.get("/paper/portfolio?quote_currency=ZZZ")
    assert response.status_code == 404
    assert response.json()["detail"] == "Paper scenario not found"


def test_estudio_drag_drawing_and_select_arrow():
    js = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    # Dibujo por arrastre: mousedown->mousemove->mouseup en #st-chart
    assert "onChartMouseDown" in js and "onChartMouseMove" in js and "onChartMouseUp" in js
    assert 'addEventListener("mousedown"' in js
    assert 'addEventListener("mousemove"' in js
    assert 'addEventListener("mouseup"' in js
    # Umbral anti-basura, preview punteada, hints y crosshair
    assert "DRAG_MIN_PX" in js and "4" in js
    assert "6 3" in js  # línea punteada de preview
    assert "de A a B" in js
    assert "st-tool-active" in js and "crosshair" in css
    assert 'aria-pressed' in js
    # Selects con flecha visible siempre (chevron claro sobre dark)
    assert "background-image" in css and "data:image/svg+xml" in css
    assert "appearance: none" in css or "appearance:none" in css.replace(" ", "")
    # Herramientas 1-clic intactas + magnet OHLC real
    for tool in ("trend", "fib", "fibext", "rect", "measure", "long", "short"):
        assert tool in js
    assert "magnetize" in js and "c.open" in js


def test_toasts_layer_and_durations():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="toasts"' in html
    assert "showToast" in APP_JS
    assert "4000" in APP_JS and "6000" in APP_JS
    assert "children.length >= 3" in APP_JS or "max" in APP_JS.lower()
    assert "#toasts" in css and ".toast" in css
    assert "position:fixed" in css.replace(" ", "").replace("\n", "") or "position: fixed" in css
    # Los avisos quedan sobre la cabecera (40) y los menús contextuales (60).
    assert "z-index: 150" in css or "z-index:150" in css.replace(" ", "")


def test_estudio_ws_race_timer_escape_and_timescale_alignment():
    """Regresión juez técnico (sin browser): race WS, no alertTimer leak, escA, SVG vs LWC."""
    js = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
    # 1. Race WS: closure my=ws y guard ws===my en onclose/onerror; stopStream no mata la nueva.
    assert "var my = ws" in js
    assert "ws === my" in js
    assert "instancia vieja" in js or "vieja" in js
    # 2. stopStream limpia pollTimer y Estudio delega las alertas al observador global.
    assert "clearInterval(pollTimer)" in js or "clearTimeout(pollTimer)" in js
    assert "if (pollTimer)" in js
    assert "OrbisMarketAlerts.onChange(paintAlerts)" in js
    assert "checkAlerts(" not in js
    assert "stopStream(); return;" in js  # guard isConnected del keydown limpia al salir
    # 3. escA completo para <text> SVG con datos persistidos.
    assert "&" in js and "<" in js and ">" in js
    assert '"' in js and "'" in js
    m = re.search(r"function escA\(v\)\s*\{([^}]*)\}", js)
    assert m is not None
    body = m.group(1)
    for ent in ("&", "<", ">", '"', "'"):
        assert ent in body, ent
    # 4. SVG vs LWC: tx usa timeToCoordinate y tAt usa coordinateToTime, fallback índice.
    assert "timeToCoordinate" in js
    assert "coordinateToTime" in js
    assert "priceToCoordinate" in js and "coordinateToPrice" in js
    # coordinateToLogical/logicalToTime NO están en la API pública estable v5 confirmada
    assert "coordinateToLogical no está en la API estable" in js
    # Todos los dibujos y el preview comparten la misma conv.
    for name in ("drawOne", "previewSvg", "pointFromEvent", "redrawDrawings"):
        assert name in js
    assert "conv.tx" in js and "conv.py" in js and "conv.tAt" in js and "conv.pAt" in js


def test_estudio_tools_dockable_with_icons():
    """Toolbar solo iconos + Configuración compacta en Estudio (sin browser).
    
    Requisitos: Toolbar principal tiene selects (par, provider, TF, tipo, refresh).
    Panel compacto <details class="st-cfg-tools"> solo con Posición herramientas.
    Se ELIMINA panel grande "Configuración del gráfico" (sin duplicar selects).
    """
    js = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # 1. Dock: contenedor sin select propio en la toolbar; el ajuste vive en
    # Estudio panel compacto <details class="st-cfg-tools">, no en #/config.
    assert "st-dockwrap" in js
    assert '<select id="st-dock"' not in js  # toolbar .drawtools: solo iconos
    assert "stchartdock" in js and "stchartdock" in css
    assert 'href="#/config"' in html
    assert "#/config" in APP_JS
    assert "Configuraci" in APP_JS  # vista Configuración del servicio (preferencias globales)
    # El dock está en el panel compacto de Estudio (st-cfg-dock)
    assert "st-cfg-dock" in js
    assert "st-cfg-tools" in js  # panel compacto <details>
    for pos in ("izquierda", "derecha", "arriba", "abajo"):
        assert pos in js, pos
    for cls in ("dock-left", "dock-right", "dock-top", "dock-bottom"):
        assert cls in css, cls
        assert cls in js, cls
    # La toolbar conserva solo iconos + undo/redo + magnet.
    assert "drawtools" in js
    # 2. Persistencia global del dock (misma clave, sin pérdida): localStorage
    # + /ux/state kind=toolsdock en Estudio (no en Config).
    assert "orbis.estudio.toolsdock" in js
    assert "toolsdock" in js
    assert 'kind=toolsdock' in js or '"toolsdock"' in js or "'toolsdock'" in js
    # 3. Iconos propios inline (SVG, sin descargas): cada herramienta con
    # icono + texto corto + title descriptivo + aria-pressed.
    for tool in ("h", "trend", "v", "rect", "text", "measure", "long",
                 "short", "fib", "fibext", "alertline"):
        assert 'data-tool="' + tool + '"' in js, tool
    assert js.count("<svg") >= 12  # 11 herramientas + undo/redo + magnet
    assert js.count("title=") >= 12
    assert "tlabel" in js and "tic" in js
    assert "st-undo" in js and "st-redo" in js
    assert "magnet" in js.lower()
    # Sin emoji ambiguos como único indicador: texto corto siempre presente.
    for label in ("Tendencia", "Vertical", "Zona", "Texto", "Medidor",
                  "Alcista", "Bajista", "Fib", "Alerta", "Deshacer", "Rehacer"):
        assert label in js, label
    # 4. Layout: grid adaptable intacto + responsive móvil (abajo por defecto).
    assert ".stgrid" in css
    assert "dock-left" in css and "@media" in css
    assert "768" in js or "abajo" in js  # default móvil abajo
    # 5. Config muestra solo preferencias globales (tema, sonido, persistencia).
    for cfg in ("cfg-theme", "cfg-alertsound", "cfg-persist"):
        assert cfg in APP_JS, cfg
    # 6. Toolbar principal YA tiene selects: st-prov, st-tf, st-ctype, st-refresh.
    # NO hay panel grande "Configuración del gráfico" (st-cfg-panel, etc. eliminados).
    assert "st-cfg-panel" not in js
    assert "st-cfg-toggle" not in js
    assert "st-cfg-provider" not in js
    assert "st-cfg-tf" not in js
    assert "st-cfg-ctype" not in js
    assert "st-cfg-refresh" not in js
    assert "st-cfg-magnet" not in js
    assert "st-cfg-overlays" not in js
    assert "st-cfg-apply" not in js
    assert "st-cfg-save" not in js


def test_all_selects_have_visible_chevron():
    """Auditoría textual: TODOS los <select> (Estudio/Operativa/Portfolio/
    Estrategias/Ledger/global) tienen chevron visible en dark; los input
    text no parecen desplegables."""
    js_estudio = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # Chevron claro sobre dark con appearance:none + padding-right.
    flat = css.replace(" ", "").replace("\n", "")
    assert "appearance:none" in flat
    assert "data:image/svg+xml" in css
    assert "background-image" in css
    assert "padding-right" in css
    # Cobertura global: regla genérica `select {` (no solo .stoolbar/.studio).
    assert re.search(r"(^|\n)select\s*\{", css) is not None
    # Inputs text no llevan chevron.
    assert "background-image: none" in css or "background-image:none" in flat
    # N selects auditados por id/nombre en cada vista (la posición de la
    # barra vive en Estudio panel inline como st-cfg-dock, no en #/config).
    sources = js_estudio + APP_JS + html
    for sel in ("st-prov", "st-tf", "st-ctype", "st-alop", "st-cfg-dock",
                "t-asset", "t-quote", "p-quote", "p-asset", "p-tf",
                "provider-sel", "strategy_type", "frequency", "side"):
        assert sel in sources, sel
    n_selects = len(re.findall(r"<select", sources))
    assert n_selects >= 15, n_selects  # todos cubiertos por la regla global


def test_estudio_zoom_controls_and_pan_handoff():
    """Zoom rueda/pinch/botones + Reset; dibujo no compite con pan (sin browser)."""
    js = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
    vendor = (STATIC_DIR / "vendor" / "lightweight-charts.standalone.production.js").read_text(encoding="utf-8")
    # handleScroll (rueda) + handleScale (pinch/ctrl) en createChart.
    assert "handleScroll" in js
    assert "handleScale" in js
    assert "pinch" in js
    # priceScale auto + timeScale.
    assert "autoScale" in js
    assert "rightPriceScale" in js or "priceScale" in js
    # Botones +/−/Reset funcionales (st-reset existe + zin/zout nuevos).
    assert "st-reset" in js and "resetTimeScale" in js
    assert "st-zin" in js and "st-zout" in js
    assert "zoomBy" in js and "setVisibleLogicalRange" in js
    # Handoff pan/dibujo: con herramienta activa se desactiva el scroll;
    # sin herramienta, pan/zoom libres (restaura opciones).
    assert "handleScroll: false" in js or "handleScroll:false" in js.replace(" ", "")
    assert "setTool" in js and "applyOptions" in js
    # Degradación honesta con vendor stub: try/catch + rango lógico + aviso.
    assert "fitContent" in js
    assert "getVisibleLogicalRange" in vendor and "setVisibleLogicalRange" in vendor
    assert "resetTimeScale" in vendor


def test_estudio_color_xss_safe():
    """CRÍTICO 1: Color en panel flotante validado como hex estricto; sin inyección."""
    js = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
    # Validador estricto #RRGGBB
    assert "isValidHexColor" in js
    assert "/^#[0-9a-fA-F]{6}$/" in js
    # Uso seguro en el input color del panel flotante
    assert "safeColor" in js
    assert "isValidHexColor(obj.color)" in js
    assert "U.esc(safeColor)" in js
    # input type=color solo acepta hex válido; value escapado
    assert 'type="color"' in js
    assert 'id="fc-col"' in js


def test_estudio_cleanup_exposed_and_called():
    """CRÍTICO 2: Cleanup idempotente expuesto y llamado al cambiar de ruta."""
    js = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    # Estudio expone cleanup en window.OrbisEstudio (wrapper que llama activeCleanup)
    assert "window.OrbisEstudio = {" in js
    assert "render: render" in js
    assert "cleanup: function" in js
    assert "activeCleanup" in js
    assert "activeCleanup = null" in js
    assert "activeCleanup = cleanup" in js
    # cleanup cierra WS, timers y la suscripción de alertas del Estudio
    assert "cleanup = function" in js or "var cleanup = function" in js
    assert "ws.close()" in js
    assert "clearTimeout(pollTimer)" in js or "clearInterval(pollTimer)" in js
    assert "stopAlertUpdates()" in js
    assert "removeEventListener" in js
    assert "chart.remove()" in js
    # app.js route() llama cleanup antes de cambiar de vista
    assert "window.OrbisEstudio.cleanup()" in app_js
    assert "OrbisEstudio && typeof window.OrbisEstudio.cleanup" in app_js


def test_estudio_time_conversion_public_apis_only():
    """ALTO 4-5: Solo APIs públicas LWC v5 (timeToCoordinate, coordinateToTime).

    Fase 2 (equivalencia documentada): onChartWheel se eliminó (la rueda la
    gestiona el motor de forma nativa, sin conversión propia que auditar);
    la comprobación "sin coordinateToLogical" se refuerza a TODO el fichero
    (antes solo se miraba dentro de onChartWheel): ninguna llamada a la API
    no estable, solo la mención del comentario que lo declara no estable.
    """
    js = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
    # Usa timeToCoordinate y coordinateToTime (públicas v5)
    assert "timeToCoordinate" in js
    assert "coordinateToTime" in js
    # NO usa coordinateToLogical/logicalToTime (no públicas estables)
    # El comentario lo deja claro
    assert "coordinateToLogical no está en la API estable" in js
    # Fase 2: navegación delegada al motor (sin zoom/pan propios que auditar)
    assert "function onChartWheel(ev)" not in js
    assert "coordinateToLogical(" not in js  # ninguna llamada en todo el fichero
    # Fallback honesto a índice lineal declarado
    assert "idxTAt" in js or "fallback honesto" in js
    # Función pública para conversión segura
    assert "coordinateToTimePublic" in js
    # svgConv tAt usa solo coordinateToTime + fallback
    assert "tAt:" in js

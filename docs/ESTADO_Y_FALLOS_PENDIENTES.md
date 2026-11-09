# Estado del proyecto y fallos pendientes

> Documento histórico del 6 de octubre de 2026. Las cifras de tests y las
> incidencias de esta página no describen el estado actual. Para avances y
> límites vigentes, consulta [PROGRESO_V0.2.md](PROGRESO_V0.2.md).

Última actualización: 6 de octubre de 2026
Servidor: `uvicorn app.main:app --host 127.0.0.1 --port 8000` (sin `--reload`), log en `/tmp/opencode/orbis-uvicorn.log`
Tests: **259 pasan** (`pytest -q`, 2 warnings heredados)
Versión de cache-bust de assets: `?v=16`

---

## 1. Estado general

- Motor PAPER completo: compra/venta, idempotencia, Guard de riesgo, Ledger, portfolio con coste medio.
- Datos de mercado multiproveedor (10 adaptadores REST+WS, caché LRU+TTL, estados de salud).
- Estudio con Lightweight Charts vendorizado local (sin CDN ni npm).
- Alertas de precio con aviso visual y campana (Web Audio), con control de sonido.
- Portfolio, Operativa y Configuración operativos.
- `node --check` limpio en `app.js`, `estudio.js` y el vendor.

## 2. Fallos de interfaz resueltos el 6 de octubre de 2026

Los cinco puntos que el usuario reportó quedaron cerrados y revisados por los dos jueces
(funcional y técnico), ambos en APROBADO.

### 2.1 Zoom real
Causa raíz: el archivo local de Lightweight Charts no es la librería real, es un stub propio de ~12,9 KB
que dibuja en canvas. En `_paint()` el eje X se calculaba como `X(i, n) = 8 + i/(n-1) * (w-72)` sobre todas
las velas: `chart._range` se guardaba correctamente en `setVisibleLogicalRange()`, pero **ninguna función
de pintado lo leía**. Por eso acercar, alejar y restablecer no cambiaban nada visible.

Arreglo: `_paint()` lee la ventana lógica visible, recorta el pintado y escala el eje X con ella
(`from` en el margen izquierdo, `to` en el derecho), con autoescala de precios y etiquetas de tiempo.
`timeToCoordinate`/`coordinateToTime` usan la misma ventana e interpolan entre velas vecinas.
Medida con harness en Node y con Chromium: separación entre velas 5,74 px en vista completa frente a
28,40 px con zoom aplicado, con ida y vuelta exacta y restablecer a 0-99.

### 2.2 "Funciones del Estudio" como submenú
Los `<details>` anidados mostraban todos los títulos a la vez, con huecos y sin contenido visible.
Ahora es un grupo de 5 botones con `aria-expanded`/`aria-controls` y navegación por flechas y Esc,
donde al pulsar una entrada se abre solo ese panel y se cierra el anterior.

### 2.3 Pantalla completa
El botón llamaba a `requestFullscreen()` sobre `.studio` pero no existía ninguna regla `:fullscreen`
en la hoja de estilos. Ahora hay maquetación completa de pantalla completa (altura `100vh/100dvh`,
gráfico al alto disponible, barra lateral con scroll propio, barra compacta, dock visible), con
`ResizeObserver` y el evento de cambio de pantalla para recalcular, más un modo expandido de
reserva si el navegador no soporta pantalla completa. Esc sigue saliendo.

### 2.4 Ayuda ordenada
Era un único párrafo con todo pegado. Ahora son 6 secciones plegables en este orden:
1. Navegar el gráfico
2. Herramientas de dibujo
3. Seleccionar y configurar un objeto
4. Preferencias de herramientas
5. Atajos de teclado
6. Alertas de precio y sonido

### 2.5 Refresco según la temporalidad
Era un valor fijo (15 s, 30 s, 1 min, 5 min, apagado). Ahora hay opción **Auto por defecto**, calculada
de la temporalidad activa (aproximadamente la mitad de la vela, con mínimo de 1 s y máximo de 60 s):
`1s→1s, 30s→5s, 1m→5s, 5m→15s, 1h→30s, 1d→1min`.
El selector muestra el intervalo efectivo (por ejemplo "Auto (5 s)"), se recalcula y reinicia el
temporizador solo al cambiar de temporalidad. Temporizadores independientes por vista:
Estudio (según vela), Operativa 15 s, Portfolio 24 h. Nada de temporizador global.

## 3. Fallo de crecimiento infinito del gráfico (6 de octubre de 2026)

Reportado por el usuario: "la gráfica se estira de forma infinita hacia abajo".

**Causa (bucle de realimentación, tres piezas):**
1. `estudio.js` → `fitChartSize()` medía `chartBox.clientHeight`, que es el alto TOTAL del contenedor
   incluyendo el panel de volumen, y se lo pasaba a `chart.resize(w, h)`.
2. vendor → `resize(w, h)` asignaba ese `h` **solo al panel principal**, así que el alto total resultante
   era siempre `h + volumen`, es decir, mayor que lo que se había medido.
3. `app.css` → `#st-chart` solo tenía `min-height: 380px` y no altura fija, de modo que la altura del
   contenedor la definía su propio contenido.

Circuito: el `ResizeObserver` mide el contenedor → se hace más alto el panel principal → crece el
contenedor → el observador vuelve a medir → repite. Cada vuelta sumaba la altura del panel de volumen.

Se detectó además un segundo sumatorio latente: los `<canvas>` se comportaban en línea y dejaban un
hueco de línea-base de unos 11-13 px entre ellos, por lo que el contenedor medía más que la suma real
de sus paneles.

**Arreglo aplicado:**
- El `resize` del vendor interpreta `h` como alto total disponible y calcula el panel principal como
  `max(80, h − suma de los demás paneles)`. Eso crea un punto fijo: una vuelta converge en vez de sumar.
  Funciona con N paneles (volumen, RSI, MACD).
- Los canvas pasan a `display: block`, de modo que la altura del contenedor sea exactamente la suma de
  los paneles y el punto de medición sea preciso.
- `fitChartSize()` incorpora guardas: ignora cambios de menos de 1 px y una bandera de reentrada
  (`stFitting`, en `try/finally` para no quedarse activa si algo falla).
- CSS sin altura fija, decisión deliberada: fijarla habría cortado los paneles extra de RSI/MACD.

**Evidencia de estabilidad:**
- Simulación de 10 vueltas midiendo y redimensionando con el mismo alto: antes `[430, 540, 650, 760,
  870, 980, 1090]` (+110 px por vuelta); después `[430 × 10]` sin variación.
- En navegador, el alto de `#st-chart`: 432 / 432 / 432 px medido a 0,8 s, 2,3 s y 6,3 s tras la carga.
  Al cambiar el tamaño de la ventana: 432 / 432 y vuelve a 432.
- Conversión de precio a coordenada y viceversa sigue exacta, con volumen a 110 px intacto.

## 4. Defectos detectados en la revisión técnica y cerrados

1. **Fugas de recursos en `cleanup()`** (crítico). El listener de `resize`, el temporizador de 5 s y el
   `keydown` de `document` se registraban sin guardarse referencia. El `removeEventListener("keydown", onKey)`
   apuntaba a un nombre fuera de ámbito y lanzaba `ReferenceError` dentro de un `try/catch`, de modo que
   **nunca se eliminaba**: cada entrada y salida a Estudio dejaba un handler y un temporizador vivos.
   Ahora hay referencias guardadas (`winResizeHandler`, `repaintTimer`, `keyHandler`) que se eliminan y
   se ponen a `null`, con idempotencia verificada.
2. **Banderas de sincronización entre pestañas** (medio). `window._orbisAlertSoundArmed` y
   `window._orbisDockStorageArmed` quedaban en `true` para siempre, así que al volver a Estudio los
   manejadores de `storage` no se volvían a registrar. Ahora se reinician en `cleanup()`.
3. **Rango del vendor sin normalizar** (alto). `getVisibleLogicalRange()` devolvía el rango sin
   normalizar mientras `_paint` pintaba con otro, de modo que al hacer pane agresivo fuera del histórico
   la capa SVG de dibujos se desplazaba respecto a las velas. Ahora un único `normalizeRange()`
   (valores finitos, orden, separación mínima 0.5) se usa en guardar, devolver, `keep` de `setData`
   y pintado, y `visRange()` de Estudio replica la misma normalización como único punto de paso.

## 5. Revisión de modelos del 6 de octubre de 2026 (03:10 CEST)

Todo coincide con `AGENTS.md`: `default_agent` es `orquestador`, modelo global
`opencode/mimo-v2.6-flash-free`, integrados build/plan/general/explore con su modelo y disponibles,
y los agentes personalizados con el modelo previsto. `contexto-terra` sigue desactivado.
No hubo nada que restaurar.

## 5bis. Rediseño responsive de la interfaz (6 de octubre de 2026, tarde)

Trabajo de diseño aplicado sobre los cinco defectos que el usuario vio en Estudio, más un defecto
de scroll horizontal que solo aparecía en pantallas estrechas.

**Scroll horizontal en móvil (nuevo, no estaba en la lista del usuario).**
Causa raíz: en `@media (max-width: 700px)` la cabecera declaraba
`grid-template-areas: "marca marca" "controles controles" …` (dos columnas) junto a
`grid-template-columns: 1fr` (una sola pista). Las áreas crean una **pista implícita** dimensionada
a `auto`, que se ajustaba al contenido y empujaba la cabecera fuera de la ventana: hasta +110 px de
scroll a 320 px y +40 px a 390 px. Arreglo: áreas y columnas declaran el mismo número de columnas y
todas las pistas usan `minmax(0, …)` para que ningún mínimo automático supere la ventana.
Comprobado en Chromium headless a 19 anchos (320–1440 px) y en las 7 vistas: cero desbordes.

**Caja fantasma bajo el gráfico.** `#st-tip` (globo de ayuda) lleva `hidden`, pero `.ctxmenu`
declara `display: grid` y ganaba al `display: none` del navegador: se veía un cuadro vacío de 12×12 px.
Arreglo: regla global `[hidden] { display: none !important; }`, que sustituye al parche
`.st-func-panel[hidden]` que ya existía por el mismo motivo.

**Barra de herramientas cortada / gráfico estrecho.** A 390 px la barra lateral (104 px) se comía un
tercio del ancho y el gráfico quedaba en 236 px. Ahora, por debajo de 760 px, las cuatro colocaciones
(`izquierda`, `derecha`, `arriba`, `abajo`) se presentan como **banda horizontal de 7 columnas** sobre
el gráfico. Solo cambia la presentación: las clases `dock-*` y la preferencia guardada siguen siendo las
mismas. A 390 px el gráfico pasa a 348 px de ancho y la barra ocupa 160 px de alto con las 14 piezas
enteras. Por debajo de 360 px la banda baja a 5 columnas para que ningún rótulo se corte.

**Columna lateral vacía.** El bloque «Análisis y organización» (Espacios guardados, Indicadores,
Comparar, Alertas, Señales) se movió de debajo del gráfico a la columna lateral. Ahora la columna es
«Compra y venta → Mercados seguidos → Análisis y organización → Cómo usar el Estudio»: primero el
estado actual, luego los datos, luego las funciones, al final la ayuda. El campo de texto y el botón
«Añadir» comparten línea (antes el botón caía debajo y dejaba una caja enorme), los rótulos de todos
los paneles usan el mismo estilo y el bloque de objetos dibujados comparte marco.

**Barra de ajustes.** El plegable «Ubicación de las herramientas» ocupaba una fila entera con solo un
título; ahora la posición de la barra es el sexto ajuste de la fila, junto a mercado, fuente, intervalo,
gráfico y actualización. Se conservan `st-cfg-tools` y `st-cfg-dock` porque las pruebas los comprueban.

**Textos.** `modeTag()` ya devolvía cadena vacía (no se repite «Modo PAPER · dinero simulado»). Se
sustituyeron los guiones sueltos por frases honestas («Este mercado no publica precios de compra ni de
venta»), «Vacía. Añade un par» por «Todavía no sigues ningún mercado» y el botón «Del» por «Eliminar».
El aviso de proveedor se compactó a una línea.

**Nombres.** Todo lo nuevo es descriptivo y en español (`panel-dato`, `panel-titulo`). Se conservan sin
renombrar, por ser contrato, los `id` `st-*`, los `data-tool`/`data-stf`, las clases `.studio`, `.stgrid`,
`.stoolbar`, `.stchartdock`, `.drawtools`, `.dock-*`, `.stside`, `.principal-estudio` y los `aria-*`;
el motivo está escrito al principio de `app.css` y en el bloque de marcado de `estudio.js`.

**Verificación.** `pytest -q`: 217 en verde. `node --check` limpio en `app.js`, `estudio.js` y el vendor.
Chromium headless a 1440, 1100, 820, 600 y 390 px en `#/estudio`, `#/inicio`, `#/portfolio` y
`#/operativa`: sin scroll horizontal, barra sin corte, sin rótulos solapados, submenú abriendo un solo
panel y Ayuda con sus 6 secciones. **Alto del gráfico medido en pestaña nueva a 0,8 s / 2,3 s / 6,3 s**
en 1440, 1100, 820, 600 y 390 px: variación **0 px** en los cinco anchos, con los 2 lienzos (precio y
volumen) intactos. El modo expandido (reserva sin API de pantalla completa) tampoco crece: 612/612/612.

## 6. Motor gráfico real (Fase 0, 7 de octubre de 2026)

El vendor `app/web/static/vendor/lightweight-charts.standalone.production.js`
es desde esta fase **Lightweight Charts v5.0.9 real** (Apache License 2.0,
cabecera `@license` intacta, 182311 bytes, servido local sin CDN ni npm). El
stub propio anterior (canvas 2D que imitaba la API v5) se retiró; su copia de
seguridad quedó **fuera del repo** en `/tmp/opencode/lwc-stub-legacy.js` (no
está en git). El nombre del fichero no cambió, así que el `?v=16` de
`index.html` y el `Cache-Control: no-cache` del servidor siguen valiendo.

**Adaptaciones mínimas en `estudio.js` (solo compatibilidad con el motor):**
- `createChart` con `autoSize: false` (verificado en fuente: `resize()` es
  NO-OP con `autoSize:true`). `fitChartSize()` sigue midiendo el contenedor
  y llamando a `chart.resize(w,h)` con las mismas guardas (`lastFitW/lastFitH`,
  `stFitting`, ResizeObserver): con el motor real, repetir `resize(w,h)` con
  el mismo `h` devuelve alturas idénticas (punto fijo verificado en harness
  Node: 10 vueltas `[371 × 10]` con volumen en 110).
- **Marcadores con instancia única**: cada `createSeriesMarkers` adjunta un
  primitivo NUEVO (antes se llamaba en cada pintada: duplicados + fuga). Ahora
  hay una sola instancia por serie (`priceMarkers`) actualizada con
  `setMarkers(paperMarks.concat(planMarks))` al llegar cada fuente async, con
  guarda anti-carrera por mercado y reset al recrear el chart. Se eliminó la
  lectura del interno del stub `priceSeries._markers.markers()`.
- **M1 · Marcadores PAPER legibles + tooltip propio** (cierra B1-1, B1-2,
  B1-4): el motor v5.0.9 NO renderiza el campo `title` de los marcadores
  (barrido hover exhaustivo: 0 tooltips), así que `title` deja de usarse.
  Compras = círculo verde con texto "P · <cantidad>" (p. ej. "P · 0,01 BTC"),
  ventas = cuadrado rojo "S", ambos con `size: 2` (las flechas medían ~2 px
  con 500 velas). `paperOps` guarda los datos crudos de /paper/operations y
  `buildPaperMarks(paperOps, mk)` construye los marcadores; sin `paperOps`
  no hay marcadores. La tarjeta `#st-marker-tip` (dentro de #st-wrap,
  `pointer-events: none`, posición con `getBoundingClientRect` + clamp) se
  actualiza en `subscribeCrosshairMove` con la operación más cercana por
  tiempo (fecha, lado, cantidad, precio, importe, comisión con los
  formateadores de Operativa) y se oculta en `mouseleave`/crosshair vacío.
  Con la casilla PAPER apagada ni se pide /paper/operations ni se pinta ni
  hay tooltip (`paperOps` se vacía); la capa se resetea donde `priceMarkers`
  se pone a null. Sin umbral de cercanía: con operaciones cargadas, cada
  hover muestra la más próxima (pendiente: umbral por ancho de vela).
- **Precisión adaptativa del eje**: el motor usa `precision:2/minMove:.01` por
  defecto (ilegible en 0.00000123). `priceDecimalsFor()` la calcula desde los
  datos (`ceil(3 - log10(max))`, entre 2 y 8) y se aplica con
  `series.applyOptions({priceFormat})` en cada `paintAll`: BTC (~1e5) → 2,
  altcoin 1.2e-6 → 8.
- **Leyenda OHLC (#st-ohlc)**: con el motor real `subscribeCrosshairMove` sí
  entrega `param.time`, así que la leyenda se actualiza al mover el ratón
  (con el stub nunca llegaba). El guard `if (!param || !param.time) return`
  se conserva: solo filtra el ratón fuera del gráfico.
- El zoom/pan siguen siendo los propios de Estudio (`handleScroll`/
  `handleScale` en false, `setVisibleLogicalRange` centrado en cursor):
  el comportamiento no cambia, solo el motor que pinta.

**Diferencias honestas del motor real frente al stub (fijadas en tests):**
- El rango por defecto incluye desplazamiento a la derecha (`{-1.33, 99}`
  con 100 velas a 640 px), no `0-99` exactos; `fitContent()` (botón Centrar)
  sí encuadra `0-99`. Los tests exigen cobertura en vez de igualdad literal.
- El rango invertido **lanza** (`from` no puede ir tras `to`); Estudio nunca
  lo envía gracias a la normalización de `visRange()`.
- Span mínimo impuesto por el motor: 1 unidad lógica (el 0.5 de `visRange()`
  solo estabiliza la capa SVG de dibujos).
- El pan fuera del histórico se recorta conservando orden y span; re-fijar
  lo devuelto es punto fijo. `setData` conserva el rango visible.
- `timeToCoordinate` devuelve `null` en tiempos sin vela (el stub
  interpolaba) y fuera del histórico; los fallbacks `idxTx/idxTAt` de
  `svgConv` ya cubrían esos casos.
- El motor reparte píxeles enteros por stretch al redimensionar: un tercer
  panel nominal de 140 px puede leerse como 139, estable entre vueltas.

**Cierre (Fases 1-3, 7 de octubre de 2026):** EMA (Fase 1), colores de velas
(Fase 2) y dibujos editables + mano alzada (Fase 3, ver §9) están cerrados.
No es de estas fases: timeframes adicionales, solo se usa escala lineal (el
motor trae logarítmica/porcentual, sin usar). Sin pruebas end-to-end con
navegador interactivo más allá de la verificación manual con
`agent-browser` de cada fase (ver §7 y §9).

## 6bis2. EMA correcta (Fase 1, 7 de octubre de 2026)

- **Semilla SMA**: `ema()` se sembraba con `vals[0]`; ahora es la SMA de los
  primeros `p` (primer válido en `p−1`), misma recurrencia y orientación.
- **Bug previo hallado al verificar en navegador**: `closes()` leía `r.c`
  (fila cruda) pero recibe velas mapeadas (`{close}`), así que SMA/EMA/BB/
  RSI/MACD pintaban **ceros** (línea plana en 0). Ahora lee `close` con
  fallback a `c`. La Fase 0 no lo detectó porque solo miraba consola, no
  píxeles/valores.
- **Historial**: `limit = clamp(maxPeriodo×4, 500, 1000)` (`candleLimitFor`;
  EMA-200 → 800) en principal y comparación; al activar un periodo largo con
  pocas velas se recarga solo. Tope 1000 del endpoint, backend intacto.
- **Multi-periodo**: `indCfg.ema.p` admite número (legado) o lista `"9,20,50"`;
  una línea por periodo con color estable (9 azul, 20 morado histórico,
  50 dorado, 100 verde, 200 rojo) y título `"EMA <p>"` en la leyenda del
  motor (el Estudio no tiene tooltip propio de indicadores).
- **Caché**: `paintIndicators` reutiliza los arrays si no cambian datos,
  mercado, TF ni parámetros; las series se siguen recreando
  (`removeSeries`+`addSeries`): reutilizarlas queda para la fase de
  rendimiento (riesgo con los panes RSI/MACD).
- Verificación: `tests/test_estudio_ema.py` (función real en Node vs
  referencia Python, p=9/20/50/100/200 + bordes + `closes` + `limit`) y
  navegador real (EMA-20/200 pintadas, triple coincidencia
  navegador=Node=Python en el último valor, refill a 800, recálculo al
  cambiar TF/proveedor, screenshot `/tmp/opencode/fase1-ema.png`).

## 6bis. Limitaciones reales conocidas (vigentes tras la Fase 2)

- (Fase 2) Navegación delegada al motor: `handleScroll` {rueda,
  arrastre} + `handleScale` {rueda, pinch, eje, doble clic} nativos; se
  eliminó el zoom/pan propios. Sigue sin haber crosshair magnético.
- (Fase 2) El pan más allá del histórico con autoescala YA NO colapsa la
  escala: al salir a zona sin velas se congela el último rango con datos y
  al volver se re-auto (ver §8). Solo si el usuario fijó la escala a mano
  se respeta la suya en vez del congelado.
- No hay pruebas end-to-end automatizadas con navegador interactivo: la verificación de clics, arrastres y
  gestos se hace con comprobaciones de DOM en Chromium, texto fuerte en los tests y harnesses en Node sobre
  el motor real. Este límite importa: el bucle de crecimiento no lo detectó ninguna revisión con una sola
  medición estática, sino la queja del usuario. Toda mejora del tamaño del gráfico debe comprobarse midiendo
  en varios instantes.
- Al salir de pantalla completa o del modo expandido, el gráfico se queda estable en el último alto en vez
  de volver al valor inicial. Es un comportamiento adaptativo del diseño responsive, no un defecto: llena el
  espacio disponible y se detiene. Devolverlo al valor anterior exigiría resetear la altura del panel al salir.
- `/health` responde 404 y no se tocó por quedar fuera de estas tareas.
- No se ha implementado el envío de correo para alertas: sigue habiendo solo aviso visual y campana.
- La barra de herramientas se presenta como banda horizontal por debajo de 760 px aunque el usuario
  haya elegido «Izquierda» o «Derecha». Es una decisión de diseño deliberada para no estrechar el
  gráfico; la preferencia se respeta tal cual en pantallas anchas y no se sobrescribe.
- En columna lateral, por debajo del último panel queda espacio vacío cuando no hay dibujos ni ayuda
  desplegada. Se prefiere aire a rellenar con contenido de relleno.

## 7. Notas para retomar

- Si se retoma el trabajo en el vendor, usar la copia del stub previo en
  `/tmp/opencode/lwc-stub-legacy.js` (fuera del repo, no está en git) solo
  como referencia; el estado actual es Lightweight Charts v5.0.9 real con los
  246 tests en verde (217 de la Fase 0 + 2 reparaciones A + 20 de la Fase 1 + 7 de la Fase 2).
- Toda mejora del gráfico debe comprobarse contra la alineación de los dibujos: un cambio en el rango
  visible afecta a `svgConv`, al hit-test, al imán OHLC y al panel contextual.
- PENDIENTE (fase de tiempo real, no hacer aquí): la actualización de
  indicadores en vivo vía WebSocket y la coherencia de refresco/vela
  abierta. Incidencia ya abierta. Nota de entorno 7-oct-2026: el venv trae
  uvicorn sin librería de WebSocket (`websockets`/`wsproto` ausentes;
  aviso "No supported WebSocket library detected"), así que `/market/stream`
  no entrega ticks en ese servidor y el Estudio tira de `loadSnapshot` cada
  30 s (1h) + `fallbackPoll`; el contrato de escala fijada ante `update()`
  quedó verificado en harness Node (FIXED-OK), no con ticks vivos.

## 8. Fase 2: navegación nativa + colores de velas (7 de octubre de 2026)

**A1 Decisión (delegar al motor, con evidencia).** Se probó el cálculo
propio frente al nativo en harness Node sobre el motor real: 3 gestos de
rueda nativos pasan el span 99 → 74,13 (~×1,1 por gesto, progresivo) con
**deriva 0 px** en la vela bajo el cursor (el motor ancla en clientX) y
vuelta simétrica {-2,16, 99,9}; Mayús+rueda (deltaX) panea con span
intacto sin código (fuente del vendor: `Ym` hace deltaY→`bc(x)` zoom y
deltaX→`Sc` scroll). El cálculo propio (1,15/0,87 fraccional) anclaba peor
y duplicaba lógica, así que `onChartWheel`/`onPanMouse*`/`panState` se
**eliminaron** (~70 líneas). Se conservan `zoomBy(0,75)`/`zoomBy(1,33)`
(botones, mismo mecanismo) y Centrar (`fitContent` + re-auto, ver A4).
Cambio de comportamiento declarado: el paneo es con botón izquierdo
(nativo `pressedMouseMove`); el botón central ya no panea. Sin herramienta
activa el arrastre mueve; con herramienta, `setTool` desactiva
navegación (`handleScroll/Scale: false`) y al salir restaura la nativa.
Tras un arrastre nativo se suprime el clic resultante (registro mínimo
`navDown`, no pan duplicado) para no seleccionar dibujos al mover.

**A2-A6 Navegación.** Rueda = zoom anclado al puntero (verificado en
navegador: misma vela 9:00 en leyenda tras 3 zooms; 55,25 → 73,62 px por
vela = ×1,333 exacto). Botones +/−: 43,8 → 77,7 px = ×1,774 (≈0,75⁻²).
Táctil/pinch y arrastres con dos dedos: opciones nativas intactas
(`horzTouchDrag/vertTouchDrag/pinch`, sin gestos bloqueados más allá del
modo dibujo). Mayús+rueda panea (harness); no se forzó nada más con
Shift. **A4 escala vertical:** arrastrar el eje la fija
(`axisPressedMouseMove` nativo; el motor apaga `autoScale` solo);
Estudio lo detecta en `onNavMouseUp` (cambio sin orden programática,
`priceProgTag`, solo si auto quedó off) y marca `priceUserFixed`: el
rango fijado sobrevive a pan/zoom/repintado/update (harness FIXED-OK:
idéntico tras 5 paneos + 3 zooms + update + setData + overscroll; en
navegador se comprobó el tramo automático — paneos nativos reescalan con
auto — y la detección manual por revisión de código + fuente del vendor).
Doble clic en el eje (`onNavDblClick`, zona por `priceScale.width()`) o
Centrar → `unlockPriceAuto` + rango completo. **A3 huecos:** con auto,
ir a {-100,-50} colapsaba {99,200}→{99,102} (medido); ahora
`guardPriceOnScroll` congela el último rango con datos y re-auto al
volver (solo si el usuario no fijó escala). **A7 dibujos:** `svgConv`
intacto (usa `timeToCoordinate`/`coordinateToTime`/`priceToCoordinate`
del motor); verificado en navegador con línea horizontal creada antes de
navegar: etiqueta 2407,91 constante tras 6 paneos + 10 zooms, y1
150,5 → -48,3 → 211,0 siguiendo la escala (anclada al precio).

**B Colores de velas.** Panel «Velas» en Análisis y organización (mismo
estilo/lenguaje): presets verde/rojo (= tonos del motor #26a69a/#ef5350,
aspecto sin cambios por defecto), azul/rojo (#2962ff/#f23645) y
blanco/negro (cuerpo negro con borde blanco + mecha #e8e8e8, legible
sobre #08100f); 5 colores manuales + «Mecha del color del cuerpo»
(`wickAuto`, por defecto mechas del color del cuerpo = idéntico a antes).
Persistencia global con el mecanismo existente (`saveLocal`
`cfg|all|x|candles` + `PUT /ux/state kind=candles`): se aplica al crear
la serie (cubre mercado/TF/proveedor/tipo) y sobrevive a recargar
(verificado en navegador: localStorage + píxeles blancos tras recarga,
5m y ETH/EUR y proveedor binance, PUT 200). Volumen teñido con los
bordes al 50 % (legible en los 3 presets; cambia levemente el tono
anterior fijo rgba(63,185,80,.5)→color de vela). Comparada: se deja el
dorado #e3b341 a propósito (línea de % normalizada, teñirla confundiría).
Leyenda/crosshair/línea de último precio sin cambios (legibles).

**Verificación.** `pytest -q`: 246 en verde. `node --check` limpio.
Navegador real (Chromium via agent-browser, servidor propio puerto
56221, datos Kraken/Binance en vivo): cero errores de consola y cero
errores de página en carga, ruedas, arrastres, clics, cambio de
mercado/TF/proveedor, panel de colores, recargas y Centrar. Screenshots
`/tmp/opencode/fase2-navegacion.png` (zoom profundo con línea anclada)
y `/tmp/opencode/fase2-colores.png` (preset blanco/negro).
**Aserción actualizada (única):**
`test_estudio_time_conversion_public_apis_only` pedía que existiese
`function onChartWheel(ev)`; al eliminarse la rueda propia por A1 se
sustituyó por su equivalencia reforzada: esa función ya no existe y
`coordinateToLogical(` no se llama en todo el fichero (antes solo se
auditaba dentro de onChartWheel). Sin debilitamiento.
**Límites honestos:** la rueda y el arrastre del CLI de automatización
son no-op (no conservan estado entre comandos), así que rueda/pan se
ejercitaron con `WheelEvent`/`MouseEvent` reales despachados en el DOM
(`preventDefault` del motor observado) y clics/botones reales; el
arrastre del eje de precios no fue accionable con la automatización
disponible (los sintéticos no activan el `rp` del widget) y su tramo
motor→detección quedó cubierto por fuente del vendor
(`eo`→`Pl({sn:!1})`) + harness + revisión de código, no E2E.

## 9. Fase 3: dibujos editables + mano alzada (7 de octubre de 2026)

**3.1 Edición.** No existía arrastre de objeto ni de extremos: `hitTest`
devolvía `part` pero ningún handler lo usaba (el clic solo seleccionaba).
Ahora, sin herramienta activa, arrastrar sobre un objeto lo mueve entero
(todos sus `pts`, misma forma, anclaje tiempo/precio intacto tras zoom) y
arrastrar un punto de control mueve solo ese extremo (`moveEndpoint`).
Durante el arrastre la navegación del motor se desactiva (`setNavEnabled`,
los mismos literales que `setTool`, sin duplicar) y al soltar se restaura;
el clic resultante se suprime para no reseleccionar. Decisión UX: el objeto
tiene prioridad sobre la navegación solo donde hay algo agarrado; en zona
vacía el arrastre sigue paneando. El cuerpo se mueve sin imán (lo
deformaría); los extremos sí respetan `#st-magnet`. Bloqueado (`locked`):
seleccionable pero no arrastrable (`canDragDrawing`); oculto: no clicable
(como antes) y al mostrarse vuelve a serlo. Los puntos de control se pintan
fuera del grupo de opacidad para seguir agarrables con opacidad baja.

**3.2 Propiedades.** El panel flotante suma grosor (Fino 1 / Normal 1.5 /
Grueso 2.5 / Muy grueso 4 px), estilo (Sólida / Discontinua) y opacidad
(0-100 %, defecto 100), más Duplicar (copia profunda con offset de 3 velas
+ 0,5 % en precio, queda seleccionada). Modelo: campos `width`, `style`,
`opacity`, opcionales y retrocompatibles (sin migración; `persistDrawings`
con misma clave y formato). El `dash` legado lo absorbe `style` al
cambiarlo. Una sola ruta de pintado (`drawOne`; se verificó que
`DrawingPrimitive.prototype.draw` no existe: la otra ruta aparente era el
preview de creación) más `previewSvg`/`paintFreehandPreview` para gestos.
El hit-test suma medio grosor a la tolerancia (6 px cuerpo / 10 px
extremos con width 1.5, como antes).

**3.3 Mano alzada.** Herramienta `freehand` (botón Trazo, mismo patrón
icono+title+aria-pressed, hint y ayuda §2-3). El gesto acumula píxeles con
distancia mínima 5 px, se mapea a tiempo/precio con `svgConv` (nunca se
guardan píxeles: sobrevive a zoom/pan/escala) y sin imán (lo escalonaría).
Selección por distancia a la polilínea; solo primero/último son extremos
agarrables (el interior es cuerpo). Persistencia por el mismo mecanismo;
round-trip JSON idéntico.

**3.4 Reparaciones.** `unlockPriceAuto` resetea `lastInDataPriceRange` y
`navDown` (defensivo). Test de apilamiento en `tests/test_estudio_fase3.py`:
canvases del vendor zIndex 1/2, logo `z-index:3`, `#st-draw` 5, overlays de
arrastre 49/50, menú 60, panel flotante 100, toasts 150 (leídos del vendor y
de `app.css`, orden estricto). Sin ese 5 el SVG quedaba bajo los canvas
aunque vaya después en el DOM y los dibujos eran invisibles;
`pointer-events:none` intacto.

**Verificación.** `pytest -q` en verde (base 248 + 11 nuevos).
`node --check` limpio. Navegador real con `agent-browser`: crear trend,
mover cuerpo y cada extremo, grosor/estilo/opacidad, duplicar, bloquear
(ya no se mueve), desbloquear, eliminar; trazo libre creado/visible/
seleccionado/movido, anclado tras zoom+pan+Centrar y persistente tras
recarga; navegación intacta sin herramienta; clic vacío deselecciona; cero
errores de consola. Screenshots `/tmp/opencode/fase3-dibujos-editables.png`
y `/tmp/opencode/fase3-freehand.png`.
**Aserción actualizada (única):** la rama `measure` duplicada al final del
hitTest se eliminó (cubierta por la rama `trend||measure` con el mismo
check); ningún test anterior la fijaba. Sin debilitamiento.

## 9bis. Reparación Fase 3: Escape cancela el arrastre + sin gestos a medias (7 de octubre de 2026)

Dos bloqueantes de los jueces sobre el estado de arrastre, más un menor de
hit-test. Solo `app/web/static/estudio.js` + `tests/test_estudio_reparacion_escape.py`.

**B1 Escape no cancelaba la edición.** El handler de `Escape` llamaba a
`setTool(null)` (limpia `dragState`) pero no a `editDrag`: como el `mousemove`
muta `obj.pts` en directo, al pulsar Escape no se revertía nada y el arrastre
seguía en el siguiente movimiento. Ahora Escape restaura los puntos originales
(`cancelEditDragApply`: cuerpo = `orig` completo, extremo = `orig[part],
texto flotante = `fx0/fy0`) y anula el estado + restaura la navegación.
**Decisión `pushUndo`:** diferido al primer movimiento real (antes se guardaba
al agarrar, así que hasta un clic simple dejaba un Ctrl+Z fantasma y borraba
el redo); al cancelar se retira ese paso (pop) y se devuelve `redoBackup`.
Efecto colateral honesto: el clic simple sin mover ya no ensucia el undo.

**B2 `loadAll`/`applyLayout`/`cleanup` no limpiaban el arrastre.** Cambiar de
mercado/TF/proveedor o de layout a mitad de un gesto dejaba `editDrag`/
`dragState` obsoletos. Ahora existe una única `cancelDrawingInteractions()`
(anula ambos + `setNavEnabled(true)`) llamada desde los 4 sitios (Escape,
`loadAll`, `applyLayout`, `cleanup`); `cleanup` además fija `editDrag = null`
en su reset de estado. Sin duplicar lógica.

**Menor `rect`/`text`.** `rect` usaba ±4 px fijos; ahora usa `tol = 6 +
grosor/2` como el resto (fijado por test: a 7 px del borde, width 4 selecciona
y width 1 no). `text` conserva su radio fijo 12 px con comentario que explica
por qué (no es un trazo) + test que lo fija.

**Verificación.** `pytest -q`: 266 en verde (259 + 7 nuevos).
`node --check` limpio. Navegador real (`agent-browser`, puerto 45675):
arrastre de cuerpo cancelado con Escape vuelve al px exacto
(246.0,170.8→492.0,294.0) y no continúa; un solo Ctrl+Z elimina la creación
(sin fantasma); cambio de TF y layout a mitad de arrastre sin errores y con el
siguiente arrastre (cuerpo y extremo) funcionando; rueda sobre el canvas con
zoom real y dibujo anclado; cero errores de consola/página en toda la sesión.
Screenshot `/tmp/opencode/reparacion-escape.png`.

## 10. Fase 4: intervalos 1s/30s, refresco coherente y tiempo real (7 de octubre de 2026)

**4.0 Reparaciones heredadas.**
1. Invariante «herramienta activa ⇒ navegación desactivada»:
   `cancelDrawingInteractions()` reactivaba la navegación siempre, así que
   cambiar de mercado/TF/layout con una herramienta pulsada dejaba el botón
   activo con la navegación encendida. Ahora restaura según herramienta
   (`setNavEnabled(!activeTool)`) en `cancelDrawingInteractions` y en los
   tres cierres de `editDrag` (mouseup, mouseleave, objeto desaparecido).
   Se eligió condicionar en vez de `setTool(null)` para no desarmar la
   herramienta que el usuario eligió; los botones conservan su aria-pressed
   tras `loadAll`/`applyLayout`. Al recargar la página el Estudio nace sin
   herramienta (`activeTool=null`), luego ese caso no cambia.
2. `Supr`/`Retroceso` con `editDrag` activo cancelaba el arrastre primero
   (restaura puntos + retira su paso de undo diferido): el borrado deja un
   solo paso en el undo en vez de dos, y no queda `id` huérfano.
3. Tests de comportamiento en `tests/test_estudio_fase4.py` (funciones
   reales en Node + cableado de los tres caminos).
4. Decisión `onChartMouseLeave`: al salir el puntero en mitad de un
   arrastre de edición se CONSOLIDA lo movido (persistir + reactivar
   navegación) en vez de cancelarlo. Motivo: fuera del lienzo no hay más
   `mousemove`, así que el gesto no puede continuar; cancelar tiraría el
   trabajo del usuario y dejaría el paso de undo diferido huérfano. La
   creación a medias sí se consolida/aborta según tenga o no trazo
   (freehand con trazo se termina; `dragState` sin mover se descarta).

**4.1 Disponibilidad real (probes del usuario).** Solo binance/okx/gate
sirven 1s y solo okx sirve 30s. Backend: `"1s"` en `TF` de
binance/okx/gate y `"30s"` solo en okx (único cambio en adapters, con
`diff -u` contra respaldo en `/tmp/opencode/adapters-backup/`).
`normalize.py` no necesita ajuste (dedupe genérico por `t`, sin mapa de
duraciones). Frontend `TF_PROVIDER_UNSUPPORTED` corregido a la tabla real
(okx sin entrada: sirve ambas). La nota de TF no soportados persiste tras
cargar y `loadCandles` muestra el motivo de `available:false` (HTTP 200)
en vez de un «Sin datos» genérico.

**4.2 Refresco coherente.** El timer llamaba solo a `loadSnapshot`
(ticker): ahora llama a `refreshEstudioTick()` (ticker + `refreshOpenCandle`,
fusión incremental con `mergeCandleRows`: update in situ o `paintAll(true)`
conservando vista). Se piden las últimas 5 SIN `since` (el `since` no
significa lo mismo por exchange: en OKX `after` pagina hacia atrás) y con
`cache: "no-store"` (sin esto el navegador congelaba la vela con su caché
heurística: misma URL + API sin `Cache-Control`; medido `transferSize`
constante y 2-3 ms). Seguimiento en vivo solo en el borde derecho (si se
inspecciona histórico, la vista no se mueve) y sin repinte si el cierre no
cambió. Regla única de Auto: cuarta parte de la vela en segundos enteros,
1 s–60 s (`TF_SECONDS` + `autoRefreshFor`; `AUTO_TF_MS` eliminada):
1s→1s, 30s→8s, 1m→15s, 5m y mayores→60s; nunca más de 1 pet/s. La nota
distingue duración de vela, frecuencia de refresco y vela abierta (texto +
`title`). Límite honesto: la caché de 60 s del backend (`service`/`cache`,
fuera del alcance de esta fase) marca el ritmo real contra el exchange.

**4.3 Tiempo real.** `ws.onmessage` repinta indicadores tras cada tick (la
caché de la Fase 1 solo recalcula si cambió el cierre). El fallback sin WS
(`fallbackPoll`/`fallbackPollOnce`) refresca lo mismo que el timer.
Entorno: `websockets` 17.1 y `wsproto` 1.3.2 SÍ están instalados, WS
`/market/stream` entrega ticks y el SSE responde; sin cambios en
`requirements.txt`. La nota de §7 sobre falta de librería WS queda
desactualizada para este venv.

**Verificación.** `pytest -q`: 278 en verde. `node --check` limpio.
Navegador real con datos vivos: binance/1s, okx/1s, okx/30s y gate/1s con
velas de paso correcto; `since`→ventana avanzando cada segundo en 1s;
leyenda OHLC 4:40:00→4:41:00 en 30s con seguimiento de vista; EMA-20
repintada; kraken/1s, binance/30s y gate/30s con motivo honesto en UI;
cero errores de consola/página. Screenshots `/tmp/opencode/fase4-1s.png`
(GATE 1s) y `/tmp/opencode/fase4-30s.png` (OKX 30s + EMA 20).

**Pendientes honestos.** Granularidad real ≈ mín(cadencia UI, caché
backend 60 s): bajar el TTL de velas para 1s/30s queda para backend (fuera
de alcance aquí). El tick WS muta la última vela local sin rolado por
frontera de vela (el refresco lo corrige en el siguiente ciclo). `grvt`
tampoco anuncia 30m/1w y el selector no lo refleja (preexistente, fuera de
esta fase). El comentario de `app/web/mount.py` («ningún adaptador anuncia
1s/30s») quedó desactualizado (fichero fuera de alcance, no tocado).

## 11. Fase 4b: caché con frescura proporcional + guarda WS↔REST (7 de octubre de 2026)

Repara el bloqueante del juez técnico: la caché servía las mismas velas
hasta 60 s mientras la UI prometía «se actualiza cada 1 s» (medido antes:
segunda llamada a 2,5 s devolvía `last t` idéntico, 12 s de retraso creciendo
hasta 60 s; en 30s/okx se midió `last t` congelado con 33 s de lag).

**Caché (`app/marketdata/cache.py`, único fichero backend tocado).**
TTL de velas proporcional al timeframe con la MISMA regla que el refresco
Auto de la UI (¼ de vela, topes 1-60 s): 1s→1 s, 30s→8 s (7,5 redondeado,
igual que `autoRefreshFor`), 1m→15 s, 5m y mayores→60 s (tope). La clave ya
traía el timeframe (`key[2]`), así que `service.py` no se tocó. Fuente única
de duración de vela en Python (`TF_SECONDS`/`tf_seconds` aquí; los adapters
solo mapean nombres a parámetros del exchange, sin duraciones; réplica
comentada del frontend). Ticker 60 s→5 s: la UI lo consulta en cada tick
(hasta 1/s), así que 5 s dejan ~0,2 pet/s al exchange por par/proveedor y el
último precio nunca lleva más de 5 s (60 s lo dejaba visiblemente viejo con
cualquier TF). La protección contra picos sigue: dentro del TTL todo se sirve
de caché (verificado: segunda llamada inmediata no toca al proveedor).
`TTL["candles"]` (60 s) queda como tope/fallback.

**Carrera WS/REST (`app/web/static/estudio.js`).** `ws.onmessage` avanza
`wsTickSeq` al mutar la última vela; `refreshOpenCandle` captura la marca y el
tiempo antes de pedir y, si hubo tick en vuelo, poda con la pura
`pruneRefreshRows` (solo tiempos estrictamente nuevos: añade sin pisar lo que
el tick avanzó). Sin tick en vuelo el comportamiento es idéntico al anterior.
Reusa `generation`/`loading`/`refreshing` existentes, sin sistema nuevo.

**Backfill: NO arreglado, documentado (decisión de riesgo).** `loadCandles`
pide `since=oldest` esperando velas anteriores, pero 9/10 adapters lo
interpretan como «≥ since» (solo OKX/`after` va atrás). Arreglarlo de verdad
exige paginación hacia atrás por adapter (`endTime`/`before`/ventanas por
exchange, 9 implementaciones distintas + tests de red mockeada): riesgo alto
fuera de este encargo de frescura. El frontal ya lo absorbe con honestidad
(`older` vacío → `hasMore=false` + «Inicio del historial», sin spinner eterno
ni duplicados). Límite honesto residual: ese rótulo puede sobrestimar (puede
haber histórico más antiguo inalcanzable con `since`); queda pendiente una
fase de backfill por adapter. No se tocó `adapters/` (respaldo de `cache.py` y
`estudio.js` en `/tmp/opencode/adapters-backup2/`, `diff -u` en el informe).

**Verificación.** `pytest -q`: 287 en verde (278 + 9 nuevos en
`tests/test_frescura_cache.py`). `node --check` limpio. Navegador real
(puerto 56233, BINANCE 1s): última vela avanzando cada ~1 s (4 muestras,
lag 1 s), nota «Vela 1s · se actualiza cada 1 s» coincidente, EMA-20 morada
repintada sobre las velas, consola vacía (cero errores). Screenshots
`/tmp/opencode/fase4b-frescura.png` (BTC/USDT 1s + EMA) y
`/tmp/opencode/fase4b-btceur-centrado.png` (BTC/EUR plano: mercado parado,
pintado honesto en fino). Scripts `/tmp/opencode/frescura_check.py` (después)
y `frescura_check_antes.py` (antes) con salidas en `frescura_*.txt`.

## 12. Fase 5: rendimiento, orden visual, persistencia y refactor (7 de octubre de 2026)

Última fase de código. Solo `app/web/static/estudio.js` + `app/web/static/app.css`
+ `tests/test_estudio_fase5.py` (+ 1 docstring en `test_estudio_ema.py`).
`pytest -q`: **299 en verde** (287 + 12 nuevos). `node --check` limpio.

**5.1 Reutilización de series.** `paintIndicators` hacía removeSeries+addSeries
de TODAS las líneas en cada repintado. Medido en stub Node con todo activo
(SMA20+EMA20/50+BB×3+RSI14+MACD×2 = 9 series): antes 9 add + 9 rm POR
repintado (90 recreaciones en 10 repintados); después 9 add una vez y
0 add + 0 rm + 9 setData por repintado. La serie se crea solo la primera
vez; se actualiza con applyOptions (color/título) + setData (valores); se
elimina solo al apagarse el indicador (barrido de claves no activas) o al
destruir el chart (3 sitios resetean `indSeries`+`indSeriesMeta`). Qué
obligaría a recrear en LWC v5.0.9 (verificado en harness sobre el motor
real): NADA de lo usado —applyOptions cambia color/título/grosor e incluso
priceScaleId, setData actualiza, `series.moveToPane()` mueve de pane en
vivo—; el cambio de pane lleva camino defensivo (moveToPane con fallback a
recrear) aunque ningún pane de clave cambia nunca (resto 0, RSI 2, MACD 3).
Caché `indCacheKey` intacta; leyenda ("EMA <p>") y valores idénticos.
Aserción acoplada actualizada (única): el docstring de
`test_indicadores_con_cache_y_clave_completa` decía "las series siguen
recreándose"; el literal que comprueba sigue existiendo (ahora es el
barrido de claves inactivas, no la recreación sistemática).

**5.2 Orden visual (solo CSS, sin tocar JS).** Tres defectos corregidos: (1)
fila del imán: "Ajustar a velas" partía en dos líneas (54 px vs 43 px) y
la casilla era INVISIBLE (flex la encogía a 0: se veía el icono pero no el
estado) — ahora una línea a 43 px como los 14 botones, casilla visible de
13 px; el rótulo usa la sans de sus botones vecinos (el mono de 86 px no
cabía con casilla+icono en 100 px; medido). (2) Panel de indicadores: la
casilla quedaba huérfana en la primera línea — ahora flex con casilla
pegada a su etiqueta y ritmo de 8 px entre filas (mismo patrón en el panel
de Señales PAPER/DCA). (3) Panel flotante: "Bloquear"/"Ocultar" eran cajas
blancas gigantes (etiqueta en grid estiraba la casilla a min-height 42 px)
— ahora en línea con su etiqueta. NO tocado (medido y uniforme ya): fila
de 6 ajustes (42 px), 6 botones de acción (42 px), layouts/comparar/
alertas/watchlist, panel de Velas (incl. mecha con texto a dos líneas:
flujo normal, casilla pegada), radio 6 px de cards vs 4 px interior
(jerarquía deliberada), leyenda OHLC, banda responsive <760 px. Capturas:
`/tmp/opencode/fase5-antes-{general,toolbar,ohlc,cfgrow,side,inds,inds-on,float,side2}.png`
y `/tmp/opencode/fase5-despues-{general,toolbar,toolbar-390,inds,float,velas,cmp,alerts,marks,30s}.png`
(+ `magnet-zoom*.png` del diagnóstico de la casilla invisible).

**5.3 Persistencia.** Tabla (local + backend; el local manda, el backend
solo rellena si el local está vacío, mismo patrón en todos):
colores de velas SÍ (cfg global + PUT kind=candles, verificado con
recarga); dibujos con width/style/opacity SÍ (por mercado|proveedor|TF +
PUT kind=drawings, round-trip idéntico, sin fugas a ETH ni duplicados
tras cambios y recarga: 6 estables); layouts SÍ (+PUT kind=layouts,
guardar/aplicar verificado); TF/proveedor/mercado/tipo/refresco SÍ
(local); indicadores SÍ en el mismo navegador (por proveedor+TF, local
+ PUT kind=indicators) Y con GET de respaldo (reparación Fase 5c: antes
solo había PUT, en navegador limpio no volvían; ahora loadPersisted
pregunta kind=indicators con la misma guarda de contexto que dibujos
—mercado|proveedor|TF pedido vs actual— y la misma prioridad: el local
manda, el backend solo rellena si el local está vacío; al aplicar no se
hace PUT —sin bucles— y la respuesta se sanea con sanitizeIndCfg antes
de repintar panel + series; verificado en navegador con localStorage
limpio y con backend vacío, más cambio de mercado sin mezclas);
fiblevels SÍ (+PUT); dock SÍ (+PUT
kind=toolsdock); alertas SÍ (local); watchlist SÍ (local + /ux/watchlist).
HUECO ENCONTRADO Y CERRADO: el imán y los marcadores PAPER/DCA nacían
siempre activos (sin save/load) — ahora persisten en local cfg|all|x
(mismo patrón que refresh, sin PUT), con `!== false` para que un valor
corrupto no desactive nada; verificado con recarga. Volumen: no hay
interruptor (sigue a los colores de vela), nada que persistir.
Tests: paridad save/load por caso, claves sin mezcla, round-trips Node con
localStorage falso y PUT/GET por kind con payloads reales + aislamiento.

**5.4 Refactor.** Solo la duplicación clara y barata: las dos reglas de
etiqueta idénticas (#st-inds y #stf-panel-marks) fusionadas en un selector
compuesto (misma especificidad, sin cambio visual), con su test. El resto
(boilerplate de PUTs, helpers de chart) se deja anotado: extraerlo tocaría
muchos call-sites por poco beneficio.

**Verificación navegador** (puerto 56231, Chromium, datos vivos): 14/14
(carga, EMA, zoom/centrar, preset azul/rojo, dibujo, imán off, layout,
recarga con todo restaurado, nota 1s/binance) + 6/6 (dibujo por contexto,
ETH sin fugas, BTC conserva, recarga, 30s/okx cada 8 s sin aviso): cero
errores de consola/página relevantes (un 404 transitorio de primer
arranque, sin reproducirse después; WS /market/stream OK con el python del
sistema, que sí trae websockets — el venv no lo tiene).

**Pendientes honestos acumulados:** backfill por adapter (9/10 adapters
interpretan `since` como ≥; rótulo "Inicio del historial" puede
sobrestimar); tick WS muta la última vela sin rolado por frontera (lo
corrige el refresco); `st-meta` solo en `loadCandles`; `grvt` sin 30m/1w
(y el selector no lo refleja); comentario desactualizado de `mount.py`
(1s/30s); accent-color de casillas en azul por defecto (se deja:
unificarlo tocaría todas las vistas).

## 12bis. Reparaciones Fase 5 (7 de octubre de 2026)

Cierran las cuatro observaciones del juez técnico sin nada nuevo. Solo
`app/web/static/estudio.js` + `tests/test_estudio_fase5.py` (+ este §).
`pytest -q`: **304 en verde** (299 + 5 nuevos). `node --check` limpio.

1. **GET de indicadores** (ver §12 5.3 actualizado): patrón de guarda y
   prioridad idénticos a dibujos; `sanitizeIndCfg` (pura, testeada en
   Node) protege de payloads corruptos; asimetría documentada: el local
   es por proveedor+TF compartido entre mercados (clave `"__ind"`) y el
   backend por mercado real (igual que su PUT).
2. **Test visual corregido**: fija los selectores contextuales reales y
   su contenido (verificado que falla con las reglas eliminadas y pasa
   con ellas; `app.css` intacto, md5 idéntico).
3. **Claves BB/MACD con parámetros** (defensa en profundidad junto a
   `indCacheKey`): cambiar p/m o f/s/sig retira las viejas y crea las
   nuevas con datos recalculados; repintados normales 0 add + 0 rm, sin
   huérfanas (mismo barrido que un periodo EMA). Se eligió implementar
   (sin riesgo medido) en vez de solo testear.
4. **`moveToPane`**: se conserva el comentario defensivo y se añade test
   que ejerce el camino con stub (mueve en vivo: 0 add + 0 rm).

Verificación navegador (puerto 56311, Chromium, kind=indicators
sembrado BTC/EUR|kraken|1h con EMA "9,20"+RSI): localStorage limpio →
EMA/RSI restaurados del backend sin escribir el local; local con SMA-10
+ backend vacío → se conserva el local; ETH/EUR → todo apagado (sin
fugas); desmarcar RSI → PUT con rsi off (ida intacta, sin bucles); cero
errores de consola en toda la sesión. Captura
`/tmp/opencode/fase5c-persistencia-ind.png` (EMA 9/20 + RSI 14 pintados
desde el backend).

## 13. R1: el cambio de mercado borraba los indicadores (7 de octubre de 2026)

Reporte del usuario: «He cambiado de quien veo la gráfica y ha estallado,
no me muestra la gráfica de ninguna». Reproducido el mecanismo en navegador
real (Chromium + uvicorn, datos vivos): con indicadores activos, cambiar a
un mercado sin fila guardada en `/ux/state kind=indicators` dejaba el panel
desmarcado y el gráfico sin líneas (medido: 19 → 11 canvas); al volver, solo
reaparecía lo guardado de ese mercado.

Causa raíz (regresión Fase 5c, nuestra): el backend devuelve `payload: {}`
cuando no hay fila (`ui_state_get`: `row.payload if row else {}`) y ese `{}`
pasaba el guard del GET (truthy, typeof "object") hasta `sanitizeIndCfg`,
que lo convierte en todo-apagado: borraba en memoria la config activa en
cada cambio a un mercado sin fila. Precio seguía; la "gráfica del usuario"
(indicadores) desaparecía en todos los mercados sin fila. Descartadas con
evidencia: series muertas (los 3 `chart.remove()` resetean y el vendor real
no lanza en serie destruida —probado en Node—), `sanitizeIndCfg`,
rama `available:false`, `paintMarkers`, `resetScaleMemory`, caché
`indCacheKey` (incluye mercado+TF).

Arreglo (causa raíz, sin try/catch ni returns preventivos):
`shouldApplyIndPayload` (pura, en `estudio.js` junto a `sanitizeIndCfg`):
vacío (null/no-objeto/sin claves) = "el backend no tiene nada" y no se
aplica —se conserva lo que haya, como ya documentaba `loadPersisted`—.
Una fila guardada siempre trae claves (`persistInd` envía las 5), así que un
"todo apagado" explícito se sigue respetando. Verificado en navegador:
BTC→ETH conserva EMA/BB/RSI/MACD (19 canvas, captura
`/tmp/opencode/fix-mercado-eth-conserva.png`), cero errores; 17 rutas de
cambio probadas (selector, watchlist Abrir/alta, hash entre vistas, doble
cambio, TF 1s/30s, sin datos, proveedores incl. grvt, dibujo a medio
arrastre, layouts). Tests: `tests/test_estudio_mercado.py` (4; 3 fallan
sin el arreglo) + caso D2 en `test_estudio_fase5.py` (ok:true + `{}` no
aplica). `pytest -q`: 316 en verde.

## 14. R2: temporalidad mensual 1M hacia 2020 (7 de octubre de 2026)

Disponibilidad 1M verificada contra las APIs reales (respuestas concretas,
sin simular): binance `interval=1M` ✓, okx `bar=1M` ✓, bybit `interval=M`
✓, bitget `granularity=1M` ✓, mexc `interval=1M` ✓, kucoin `type=1month` ✓
(con ventana startAt/endAt amplia: 82 velas desde 2020-01-01); kraken (máx
1w), coinbase (`Unsupported granularity` a 2592000) y gate (`invalid
interval 1M`; su `30d` son ventanas fijas, no mes) NO lo sirven; grvt sin
OHLC. 1M añadido SOLO a esos 6 mapas + `TF_LIST`/`TF_SECONDS`(2592000 s ≈
30 d)/`TF_PROVIDER_UNSUPPORTED`/`cache.TF_SECONDS`; refresco Auto y TTL caen
al tope de 60 s (coherencia TTL≤UI de 1M verificada a mano el 7-10-2026:
TTL 60 s frente a refresco Auto 60 s; el test de frescura
`test_candle_ttl_never_exceeds_ui_refresh` usaba entonces una lista de TF
hardcodeada sin 1M y no lo cubría —corregido el 8-10-2026 en Tanda B0 para
leer `TF_LIST` real de estudio.js, que ya incluye 1M). Decisión 1y: NO se añade —ninguno lo
sirve nativo (OKX: `Parameter bar error` a `bar=1Y`) y 1M ya llega a 2020
(binance: 111 mensuales desde 2017-08; servicio verificado 2017→2026
ordenado+dedup). Decisión diario/backfill: NO se pagina —`since` es «≥
desde» en los adapters y el tope 1000 es homogéneo con los exchanges;
1d cubre ~2,7 años, 1w llega a 2013 (kraken: 683 semanales, verificado por
red y en navegador con captura `/tmp/opencode/tf-1w-2020.png`). Kucoin:
ventana de petición hecha proporcional al TF (antes fija 200 h: con 1M/1w
no cabía ni una vela). Respaldo previo en `/tmp/opencode/adapters-backup3/`
y `diff -u` limpio (solo líneas de TF + ventana kucoin). Tests:
`tests/test_estudio_tf_mensual.py` (8).

Cierre end-to-end (misma fecha, con autorización expresa restringida a
`mount.py`): añadido `"1M"` a la tupla `allowed` de `market_candles`
(`app/web/mount.py`, ÚNICA línea cambiada —respaldo previo en
`/tmp/opencode/mount.py.bak` con `diff -u` limpio que solo muestra esa
línea—; revisadas las demás validaciones del fichero: `/market/stream` y
`/market/stream-sse` no tienen parámetro timeframe, `/ux/state` no valida
timeframes y el `Literal` de `/market/history` es el endpoint legacy, así
que no había nada más que tocar). Verificado contra backend local con
datos vivos: binance BTC/EUR 1M → `available:true`, 82 mensuales
2020-01-01→2026-10-01 ordenadas+dedup; `1y` (ningún exchange lo sirve) y
`3m` → 400 honesto (`Unknown timeframe '1y'`). Test nuevo
`test_market_candles_monthly_end_to_end` en
`tests/test_market_integration.py` (documenta además el memo de instancias
del registry: hay que vaciar `_instances` para que el mock de
`http_get_json` se capture en construcción fresca). Navegador real
(Chromium, con copia de la DB en /tmp para no tocar `orbis.db`): BTC/EUR
binance 1M pinta desde 2020 con BB/EMA/RSI/MACD + línea horizontal anclada
a precio, zoom ±, cambio BTC→ETH→BTC con indicadores intactos (el arreglo
del §13 sigue funcionando), captura `/tmp/opencode/tf-1m-2020.png`; único
error de consola el 404 preexistente de `/favicon.ico` (petición automática
del navegador, ajeno a esta tanda, anotado sin tocar). Nota de refresco
SIN cambios: «Vela 1M · se actualiza cada 1 min (precio y vela abierta)»
dice exactamente lo que pasa (¼ de 2592000 s con tope 60 s; TTL 60 s) y su
`title` aclara la duración de la vela. `pytest -q`: **317 en verde**
(316 + 1).

Pendientes honestos acumulados: resolución `auto` ignora el TF (resuelve
por par → kraken; con TF que kraken no sirve sale `available:false` aunque
otro provider lo sirva —requiere `service/registry`, otra tanda);
`paintCompare` nunca pinta (compara `c.t` sobre velas mapeadas sin `.t`);
`grvt` sin 30m/1w y el selector no lo refleja; backfill `since` hacia atrás
imposible con la semántica actual.

## 15. M5: disponibilidad de TF derivada del backend, sin tabla hardcodeada (11 de octubre de 2026)

`estudio.js` tenía `TF_PROVIDER_UNSUPPORTED` hardcodeado en cliente
mientras el backend ya respondía `available:false` + `reason` honesto: dos
fuentes de verdad. M5 elimina la del cliente (SOLO frontend, sin backend).
Cada `available:false` observado en la sesión cachea el par proveedor/TF
con su `reason` EXACTO (`TF_OBSERVED_UNAVAILABLE` + `markTfUnavailable` /
`markTfAvailable` / `tfUnavailableReason`, al lado de `TF_LIST`) y el mismo
`updateTfAvailability` deshabilita esa opción con ese texto (`title` +
nota); lo no probado aparece habilitado hasta que falla (el fallo ya era
honesto) y un éxito posterior rehabilita el par. Clave =
`resolved_provider` del backend (reserva: proveedor pedido); con `auto` no
se deshabilita por adelantado (no se sabe qué respaldo resolverá). NO
tocado por diseño: `TF_LIST`, `TF_SECONDS`, `tfSeconds`, `autoRefreshFor`
(los leen `test_frescura_cache.py` por regex), backend, `app.js`/`app.css`.
Tests: `tests/test_estudio_tf_derivada.py` (6: anti-reaparición estático de
la tabla + intocables + cableado + 3 de comportamiento en Node con el
`updateTfAvailability` REAL y DOM mínimo); equivalencias DOCUMENTADAS en
los docstrings para los dos tests que fijaban la tabla
(`test_frontend_table_matches_backend_adapters` en fase4 y
`test_frontend_table_covers_1m_both_ways` en mensual: la verdad sigue en
adapters + servicio honesto, el frontend la deriva). Navegador real
(Chromium + Playwright, DB copia en `/tmp/opencode/orbis-m5.db`):
kraken+1s primero habilitado y tras el fallo honesto deshabilitado con el
motivo exacto del backend; binance+1s pinta (BTC/EUR · BINANCE · precio);
cero errores de consola (los 404 de `/paper/portfolio` sin escenario PAPER
abierto son preexistentes —`Paper scenario not found`— y se creó un
escenario en la copia para la pasada limpia); captura
`/tmp/opencode/m5-tf-honesto.png`. `pytest -q`: **355 en verde**
(349 + 6). Nota: el pendiente de `grvt` sin 30m/1w (§14) queda parcialmente
cubierto —ya no hay tabla que actualizar: al fallar, el motivo observado
deshabilita—; el de `auto` sigue vigente (con `auto` nada se deshabilita
por adelantado, también documentado en el código).

## 16. Tanda C: 7 pulidos frontend (11 de octubre de 2026)

Solo `app/web/static/estudio.js` + `app.js` + `index.html` + `tests/test_tanda_c.py`
(+ 3 aserciones de `test_m3_entry_pnl.py` por equivalencia C6, + este §).
Nada de backend. `pytest -q`: **366 en verde** (356 + 10 nuevos).
`node --check` limpio en estudio.js y app.js.

1. **C1 · Tooltip con umbral (follow-up M1):** `paperOpNear(t, maxDt)` solo
   devuelve operación si |t_op − t_hover| ≤ maxDt; `updateMarkerTip` pasa
   medio ancho de vela del TF actual (`tfSeconds(timeframe)/2`, 0 =
   desconocido = sin umbral). Sin maxDt conserva M1 (equivalencia
   documentada: `test_estudio_paper_marks.py` llama sin umbral y sigue en
   verde). Navegador (compra PAPER en copia): TF 1d, cerca→tarjeta
   («Compra PAPER… 0,001 BTC…»), lejos→oculta; con TF 1h la tarjeta queda
   bien oculta a 72 min de la op (> 1800 s).
2. **C2 · `st-meta` en cada tick:** helper `stMetaHtml` (mismo HTML que
   `loadCandles` construía inline, byte a byte) usado en `loadCandles`,
   `ws.onmessage` y `refreshOpenCandle`; sin bloques copiados. Navegador
   (REST abortado tras cargar, solo el WS puede escribir): 2 mutaciones de
   `#st-meta` en ~7 s, cero errores.
3. **C3 · El tick rola la vela:** `rollCandleIfNeeded(candles, tick,
   tfSecs)` pura —dentro→muta (mismas 3 asignaciones con `String()` del WS),
   frontera exacta→nueva (`o=h=l=c=precio`, `v: 0`), hueco→UNA sola vela en
   el bucket del tick (nunca intermedias, documentado); 1M/1w en la rejilla
   fija de `TF_SECONDS` (aproximación ~30 d/7 d, no calendario).
   Enganchada en `ws.onmessage` antes de `paintIndicators`, sin duplicar
   lógica. Navegador (REST abortado, TF 1s binance): la última vela avanza
   :15:24→:15:25 solo vía WS; el refresco/temporizador Auto intactos.
4. **C4 · `paintCompare` nunca pintaba (bug real, arreglo mínimo):** causa —
   la comparada viaja CRUDA (`{t,...}`) pero las velas del Estudio viajan
   MAPEADAS por `mapCandleRow` a `{time,...}` SIN `.t`; `byT[c.t]` era
   siempre `undefined` → `rows` vacío → `return`. Arreglo de 2 líneas:
   emparejar por `c.time` (épocas en segundos, como el resto); base/c0, %
   normalizada y color intactos. Evidencia navegador: misma zona del
   gráfico, 0 px cambiados sin comparar vs 67.815 px con ETH/EUR comparada.
5. **C5 · Favicon 404:** `<link rel="icon" data:image/svg+xml,…>` inline en
   `index.html` (O menta sobre #08100f, sin ficheros ni backend). El
   navegador ya no pide `/favicon.ico` (0 request failed, 0 respuestas
   ≥ 400 en toda la pasada); `?v=16` intacto (los tests lo fijan).
6. **C6 · Etiqueta del eje en es-ES:** `pnlLabel` formatea con Intl es-ES
   (`-0,00%`, coma) en vez de `toFixed` (`-0.00%`); `paintEntryLines` pasa
   el string ya formateado como `title`. Solo difiere de `U.fmtPct` en el
   cero exacto (`signDisplay: "always"` conserva el `+` de M3: `+0,00%`).
   Equivalencia: `test_m3_entry_pnl.py` actualizado solo en el separador.
7. **C7 · Mini de Operativa en oscuro:** `paintLwcMini` crea el chart con
   fondo `#08100f`, texto `#9caaa4`, rejilla `#172521`, `es-ES` y series
   explícitas (velas `#26a69a/#ef5350`, línea `#4da3ff`); el Estudio
   conserva su único `createChart` intacto. Píxel medido del canvas:
   rgb(8,16,15). Capturas `/tmp/opencode/tandac-{estudio,compare,tip,mini,1s}.png`;
   cero errores de consola en toda la pasada.

Pendientes honestos acumulados: resolución `auto` ignora el TF (requiere
`service/registry`, otra tanda); `grvt` sin 30m/1w y el selector no lo
refleja (parcialmente cubierto por M5); backfill `since` hacia atrás
imposible con la semántica actual. Saldados por esta tanda: `paintCompare`
(§14/§15), `st-meta` solo en `loadCandles` (§12), tick sin rolado (§12),
404 de `/favicon.ico` (§14/§15).

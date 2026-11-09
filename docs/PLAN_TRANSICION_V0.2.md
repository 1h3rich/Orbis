# Orbis — transición del repositorio actual a la visión v0.2

**Base de referencia:** árbol local examinado el 13 de octubre de 2026.  
**Objetivo:** ejecutar la [especificación v0.2](ORBIS_ESPECIFICACION_V0.2.md)
sin perder datos ni confundir resultados históricos, PAPER y reales.  
**Estado de este plan:** conserva la fotografía previa a la implementación.
Para el estado vigente y los límites comprobados, véase
[PROGRESO_V0.2.md](PROGRESO_V0.2.md). Las brechas de la tabla siguiente son
históricas, no una lista actual de funciones ausentes.

## 1. Punto de partida verificado

El repositorio actual usa Python 3.14, FastAPI, SQLAlchemy y SQLite. La UI se
sirve en `/ui` con JavaScript y CSS propios y Lightweight Charts vendorizado.
`./.venv/bin/python -m pytest -q` dio **366 pruebas correctas y 2 avisos de
dependencias**; `./.venv/bin/python scripts/check_schema.py` informó que
`orbis.db` coincide con los modelos. Estas cifras son una fotografía, no una
garantía permanente. Hay cambios locales extensos y módulos aún sin commit;
ese árbol, no sólo `HEAD`, es la implementación de referencia.

| Área v0.2 | Base existente | Brecha verificable |
| --- | --- | --- |
| Mercados y gráficos | [Datos públicos y 10 adaptadores](../app/marketdata/), [rutas de mercado](../app/web/mount.py), [Estudio](../app/web/static/estudio.js), SMA/EMA/BB/RSI/MACD, dibujos y alertas | ATR, volumen relativo, Heikin-Ashi, estructuras configurables; histórico paginado y dataset versionado; calidad y cobertura formalizadas |
| PAPER | [Escenarios, compra/venta, idempotencia](../app/core/paper_trading.py), [Guard](../app/guard/risk.py), [portfolio](../app/core/paper_portfolio.py) y [Ledger](../app/ledger/repository.py) | Cuentas por estrategia, reglas activas, señales, diario de observación, interrupciones y comparación histórica; el precio de orden aún lo envía el cliente |
| Cartera Real | [Modelo `Operation` con `mode`](../app/database/models.py) y [`POST /operations`](../app/main.py) aceptan registros manuales `LIVE` | Falta una cartera real propia, protegida y visible; el endpoint actual admite `LIVE` sin Guard ni autenticación y no modela aportaciones, transferencias ni ajustes con el alcance v0.2 |
| Estrategias | [CRUD y DCA de parámetros](../app/main.py), [modelo simple](../app/database/models.py) | No hay evaluación programada, lenguaje de reglas, versiones inmutables ni vínculo entre decisión y versión |
| Laboratorio | Cálculos de posiciones y datos públicos reutilizables | No existen backtest, eventos, operaciones simuladas, datasets ni estadísticas reproducibles |
| Seguridad y almacenamiento | SQLite local, comprobación de esquema y pruebas aisladas | Sin autenticación administrativa, cifrado de DB, bloqueo, backups verificables ni migraciones controladas |
| Interfaz | Inicio, Estudio, Operativa PAPER, Portfolio PAPER, Estrategias, Ledger y Configuración | Navegación por tres entornos, cartera real, laboratorio y espacios de trabajo generales. Nota posterior: la ruta decorativa `#/live` se eliminó; consultar `PROGRESO_V0.2.md` para estado actual. |

El [registro histórico de estado](ESTADO_Y_FALLOS_PENDIENTES.md) documenta
defectos ya resueltos y límites pendientes. En particular, el parámetro
`since` no permite paginar hacia atrás de forma consistente en la mayoría de
adaptadores. La resolución `auto` puede elegir un proveedor que no soporte la
temporalidad solicitada. Ambos límites importan para el Laboratorio.

## 2. Camino técnico recomendado

Conservar el núcleo Python/FastAPI, SQLite, los adaptadores propios y el
Estudio mientras se construyen cortes funcionales completos. En el Hito 0 se
fijará la frontera que guarda datos y claves para proteger la aplicación
actual, con una interfaz de almacenamiento independiente de la UI. El valor
de una reescritura en React/TypeScript/Tauri deberá demostrarse después con
un prototipo que consuma esa interfaz y reproduzca una tarea real de usuario.
Una decisión de arquitectura documentará entonces distribución,
compatibilidad y coste de mantenimiento de la nueva interfaz.

La v0.2 propone tecnologías, pero no exige cambiar de lenguaje antes de
implementar Cartera Real o Laboratorio. CCXT queda como opción a reevaluar
cuando aporte una capacidad concreta frente a los adaptadores públicos
actuales; su [decisión previa](CCXT_DECISION.md) es específica de la terminal
v1. No se debe inferir que los dos motores de reglas puedan implementarse en
lenguajes distintos sin un contrato común de evaluación y pruebas de paridad.

## 3. Contratos que deben fijarse antes de ampliar el modelo

1. **Identidad de entorno y cuenta.** Toda operación, saldo, evento y métrica
   tendrá entorno explícito (`LAB`, `PAPER`, `REAL`) y cuenta/sesión. Los
   registros de backtest vivirán en entidades propias; las consultas no
   inferirán el entorno a partir de una pantalla o de un valor por defecto.
2. **Procedencia y tiempo.** Datos de mercado: proveedor, par, temporalidad,
   tiempo de apertura/cierre, fecha de obtención, completitud, cobertura y
   huella del dataset. Señales y órdenes: tiempo en que la información llegó
   a ser observable. Las reconstrucciones posteriores tendrán otra marca.
3. **Versiones inmutables.** Estrategia, parámetros, reglas, política de
   costes y versión del motor quedarán fijados por `run` o activación PAPER;
   editar la definición no alterará explicaciones pasadas.
4. **Dinero y unidades.** Importes y cantidades con `Decimal` y reglas de
   precisión/redondeo explícitas; conversión de divisas con fuente y hora.
   Flujos de caja, coste, valor, P/L y comisiones se calculan por separado.
5. **Eventos en vez de sobrescrituras silenciosas.** Compras, ventas,
   aportaciones, retiradas de capital, transferencias propias, ajustes y
   correcciones tendrán tipos distintos y auditoría. Una transferencia
   interna no crea P/L por sí sola.
6. **Semántica compartida de reglas.** Un evaluador determinista consumirá
   observaciones normalizadas. Backtest y PAPER compartirán reglas y
   definiciones de indicadores; diferirán en reloj, disponibilidad de datos
   y política de ejecución simulada, documentadas y probadas.

El esquema concreto se decidirá al implementar el primer corte vertical.
`assets`, `strategy_versions`, `backtest_runs`, `detected_events`,
`simulated_trades`, `paper_accounts` y `portfolio_events` son nombres
conceptuales; no se crearán tablas vacías sólo para cumplir una lista.

## 4. Secuencia de implementación

### Hito 0 — Baseline recuperable y decisiones de almacenamiento

**Trabajo.** Registrar una referencia del árbol local que se vaya a usar
para migrar; inventariar tablas y operaciones `PAPER`/`LIVE` existentes;
elegir mecanismo de migración, backup, restauración y cifrado local mediante
prueba sobre una copia de `orbis.db`. Fijar formato y recuperación de la clave
antes de cifrar datos, y encapsular su acceso detrás de una frontera que
pueda usar la UI actual o una futura. Definir qué protege la contraseña
principal y cuándo se bloquea la aplicación. Mantener la API en loopback
hasta tener protección administrativa efectiva. Tratar las filas `LIVE`
existentes como **legado sin clasificar** hasta que el usuario confirme cuáles
representan patrimonio real; `mode=LIVE` por sí solo no basta.

**Salida verificable.** Una copia se puede restaurar y abrir; la migración
se aplica y revierte de forma ensayada sobre esa copia; un fallo intermedio
no deja datos parcialmente reinterpretados; los registros anteriores siguen
legibles. Los tests siguen usando su SQLite aislado. No modificar datos
reales durante el ensayo.

### Hito 1 — Cartera Real manual como primer corte funcional

**Trabajo.** Completar el cifrado de la base y backups, la contraseña
principal, el bloqueo automático y la protección del acceso administrativo
definidos en el Hito 0 antes de habilitar datos patrimoniales REAL. Crear una
cuenta REAL local y un libro de eventos manuales con
compra, venta, aportación, retirada de capital, transferencia interna,
ajuste y comisión. Definir método de coste para la vista informativa (el
método fiscal queda fuera). Añadir importes, unidades, precio, divisa, fecha,
nota y `request_id` o mecanismo equivalente de detección de duplicados.
Calcular posiciones y P/L desde el historial; construir alta, historial y
vista de cartera en la UI con rótulo REAL inequívoco. Importar sólo los
registros `LIVE` anteriores cuya clasificación se pueda confirmar; los
ambiguos quedarán en una lista de revisión, sin conversión automática.

**Dependencias.** Hito 0 y contrato de entorno/moneda. La valoración pública
puede reutilizar [pricing](../app/pricing/), pero debe señalar fuente y edad;
una cotización ausente no inventará rentabilidad.

**Aceptación.** Sin desbloquear, las rutas administrativas no permiten leer
ni escribir patrimonio; la base y el backup no revelan operaciones en claro,
y ambos se pueden recuperar con el procedimiento documentado. Registrar
compras y ventas de BTC y ETH, reiniciar, recuperar
cantidades, coste medio, comisiones y P/L; aportar efectivo sin que suba la
rentabilidad; transferir unidades propias sin generar compra/venta; consultar
PAPER y REAL sin registros cruzados; no guardar direcciones ni destinos.

### Hito 2 — Histórico reproducible y primer Laboratorio completo

**Trabajo.** Añadir descarga paginada hacia atrás por proveedor soportado,
normalización y detección de huecos/velas incompletas. Conservar las velas
exactas consumidas por cada prueba en un dataset local inmutable, con
proveedor, periodo, temporalidad, cobertura y huella verificable; la huella
sola no permite repetir una prueba si el proveedor cambia o retira datos.
Crear una versión inmutable de estrategia
con un primer subconjunto de reglas long/short (por ejemplo RSI, EMA y
volumen con Y/O), evaluador determinista y políticas explícitas de entrada,
salida, costes y ambigüedad intravela. Guardar `run`, señales, operaciones,
curva de capital y métricas. Construir una vista que permita crear, ejecutar,
inspeccionar y reabrir la prueba.

**Dependencias.** Contratos de §3 y resolución del histórico. El primer
backtest debe ser un corte vertical completo; ATR, estructuras avanzadas y
optimizador se añaden después de demostrarlo.

**Aceptación.** Con estrategia, parámetros, dataset y motor idénticos, dos
ejecuciones producen las mismas señales, operaciones y métricas; ningún
indicador usa el cierre de una vela aún no disponible; lagunas y conflictos
stop/objetivo quedan visibles. Una edición crea nueva versión; el `run`
antiguo conserva la anterior tras reiniciar la aplicación.

### Hito 3 — PAPER conectado a reglas y DCA ficticio

**Trabajo.** Dar a PAPER cuentas y asignaciones por estrategia, reutilizando
las sesiones y controles actuales cuando sea seguro. Hacer que señales y
órdenes consuman observaciones actuales con marca temporal y procedencia.
Registrar intervalos de desconexión; los datos reconstruidos se etiquetan y
no se convierten retroactivamente en ejecuciones observadas. Añadir DCA
periódico y condicionado con presupuesto, exposición, cooldown e
idempotencia al reinicio. Simular esas reglas DCA sobre histórico y comparar
aportaciones, unidades, comisiones y resultado frente a DCA fijo y compra
única bajo los mismos datos y costes. Compartir el evaluador de reglas del
Laboratorio y comparar el seguimiento con backtests vinculados a la misma
versión.

Preparar también PAPER LONG/SHORT para instrumentos de GRVT: primero paridad
de señal con LAB y precios actuales; después margen, financiación y
liquidación con parámetros verificables. El adaptador se ensayará contra el
entorno de pruebas de GRVT antes de considerar órdenes reales. DCA y
LONG/SHORT compartirán trazabilidad de versión, costes y límites, pero no
fingirán tener el mismo modelo de ejecución.

**Aceptación.** Una regla puede probarse históricamente y seguirse en PAPER;
el usuario puede comparar DCA condicionado, fijo y compra única;
dos reintentos no duplican una compra; varias solicitudes no superan saldo
ni límites; una caída de conexión deja un hueco visible; el sistema no compra
por una señal que sólo se conoció al reconstruir el pasado. No existe ruta
de ejecución real automática en este hito.
Una estrategia GRVT no pasa a REAL mientras PAPER no reproduzca restricciones
de margen y liquidación y se haya comprobado la compatibilidad del adaptador.

### Hito 4 — Ampliaciones y decisión de plataforma

Completar ATR, volumen relativo, Heikin-Ashi, grupos de reglas anidados,
condiciones de varias temporalidades y estructuras configurables; añadir
importación/exportación CSV, comparación de estrategias, partición
fuera de muestra, optimización y sensibilidad. Probar un prototipo de
escritorio con una tarea representativa: abrir mercado, cargar histórico,
ejecutar backtest, registrar operación real manual y recuperar estado. La
elección entre UI actual, React/Tauri o una combinación se basará en ese
resultado, no en la lista conceptual de tecnologías.

### Hito 5 — Billeteras y venues, primero observación

Tras consolidar REAL, LAB y PAPER, validar MetaMask y Trust Wallet en el
navegador local y decidir el transporte compatible con un eventual escritorio.
Conectar en modo lectura MEXC Spot y GRVT; importar saldo ya existente,
historial y transferencias sin crear compras ficticias. En wallet, el primer
corte incluye conexión/desconexión, selección de cuenta/red, consulta de
saldos con fuente y bloque, dirección sólo en memoria salvo consentimiento
para guardarla cifrada, y conciliación explícita con la cartera manual. Ni
leer un saldo ni detectar una transferencia crea una compra REAL.

Un corte posterior implementará ejecución automática bajo una política REAL
activada por el usuario: primero MEXC Spot con permisos de trading acotados;
después GRVT sólo para instrumentos con riesgo y firmas modelados; y Uniswap
en una red EVM compatible si la wallet admite delegación segura. Fondos desde
wallet o saldo previo en MEXC/GRVT, órdenes, depósitos y regreso a wallet
deben quedar conciliados. Retiro y delegación de firma tendrán permisos
separados, límites, caducidad, idempotencia y parada. La IA no podrá ampliar
políticas. Compatibilidad por cadena/proveedor, privacidad de RPC/relay y
fallos/reorganizaciones de red tendrán pruebas propias. Detalles y puertas de
seguridad: [EJECUCION_REAL.md](EJECUCION_REAL.md).

**Aceptación del primer corte.** Conectar, consultar, desconectar y reiniciar
no filtra claves ni duplica patrimonio; la vista distingue saldo observado de
operaciones registradas, saldo anterior MEXC/GRVT y coste desconocido. Sin
wallet instalada o sin red, el error es claro y la cartera manual sigue
funcionando. La aceptación de ejecución real requiere además los escenarios
de reintento, parciales, parada, retiro y conciliación de `EJECUCION_REAL.md`.

## 5. Migración de datos sin reinterpretación

| Origen actual | Destino propuesto | Regla de migración |
| --- | --- | --- |
| `paper_scenarios`, `paper_requests`, operaciones `mode=PAPER` | Cuentas/sesiones y eventos PAPER | Conservar IDs, generaciones, fechas, digests y decisiones; no recalcular hechos pasados con reglas nuevas |
| `operations` con `mode=LIVE` | Candidatos a `portfolio_events` REAL | Inventariar y validar tipo, cantidad, importe, divisa y fee caso por caso; registros ambiguos no se convierten automáticamente |
| `strategies` y JSON `config` | Identidad y primera versión de estrategia | Crear versión inicial sólo cuando la configuración sea interpretable; conservar JSON original y procedencia |
| `ui_market_state`, watchlist, preferencias | Preferencias/espacios de trabajo | Preservar claves y contexto; migrar de forma independiente del libro financiero |
| `price_quotes` | Cotizaciones manuales con procedencia | Conservar precio y hora; no transformarlas en operaciones ni velas históricas |

Antes de cualquier cambio de esquema: backup verificado, inventario de filas,
plan de reversión y prueba sobre una copia. Después: comparar recuentos,
saldos por entorno/par/cuenta y resultados conocidos, además de abrir la UI.
Una corrección posterior se hará con ajuste trazable. Los datos legados nunca
se borrarán como efecto colateral de una vista nueva.

## 6. Pruebas y puerta de salida por hito

- **Dominio:** invariantes con `Decimal`, coste medio y método de coste,
  entradas/salidas de capital, ventas parciales, fees, multi-divisa y
  aislamiento LAB/PAPER/REAL.
- **Datos de mercado:** paginación real por adaptador, deduplicación, huecos,
  vela abierta, procedencia, frescura y fallos de red; muestras congeladas
  para reproducción.
- **Reglas/backtest:** casos mínimos calculables a mano, sin datos futuros,
  ambigüedad intravela, estrategias versionadas y repetición byte a byte de
  eventos relevantes.
- **PAPER:** reintentos, concurrencia, límites, desconexión, reconstrucción
  y paridad de interpretación de reglas con Laboratorio.
- **Persistencia y UI:** migrar una copia con datos legados, reiniciar,
  verificar vistas por entorno y recorrer los criterios de aceptación
  visibles; comprobar que la UI no ejecuta controles simulados sin función.
- **Seguridad local:** bloqueo, restauración de backup cifrado, ausencia de
  secretos/direcciones completas en exportaciones y diagnósticos.
- **Billeteras (hito 5):** permisos revocados, cambio de cuenta/red, rechazo
  de firma, token/red equivocados, RPC caído, doble cómputo y no persistencia
  de direcciones sin consentimiento.

Se considera terminado cada hito sólo cuando su flujo completo funciona
desde la interfaz, sus datos sobreviven al reinicio y sus invariantes se
comprueban automáticamente. La suite actual sirve de regresión, pero sus
366 pruebas no sustituyen estas nuevas pruebas de producto.

## 7. Decisiones abiertas que condicionan implementación

| Decisión | Por qué importa | Momento límite |
| --- | --- | --- |
| Frontera de acceso a datos y claves en la aplicación actual | Permite cifrar y bloquear sin acoplar esas funciones a una pantalla concreta | Hito 0, antes de cifrar datos |
| Aplicación web local actual, Tauri/React o híbrido | Define distribución y reemplazo o convivencia de la UI | Hito 4, después del prototipo |
| Cifrado, derivación y recuperación de clave; bloqueo | Evita pérdida de datos y falsa sensación de protección | Hito 0 |
| Método de coste informativo y política multi-divisa | Cambia P/L histórico y qué significa «precio medio» | Antes de Cartera Real |
| Política intravela, slippage y datos mínimos por modo | Cambia resultados y comparabilidad de backtests | Antes del primer `run` guardado |
| Formato/versionado del lenguaje de reglas y del motor | Permite paridad entre Laboratorio y PAPER | Antes de activar estrategia en PAPER |
| Tratamiento de operaciones `LIVE` legadas | Evita llamar «cartera real» a datos de prueba o registros incompletos | Antes de migrar esas filas |

Estas decisiones se cerrarán con un documento de decisión y pruebas sobre
datos de ejemplo. Hasta entonces el plan usa explícitamente supuestos, no
presenta una tecnología o una fórmula fiscal como requisito aprobado.

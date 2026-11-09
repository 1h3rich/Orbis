# Cierre de derivados PAPER tras interrupción

Estado: v106 cargado y verificado. El objetivo global continúa abierto.

## Decisión

Una sesión GAP, HALTED o detenida por reinicio puede conservar una posición
ficticia. No se reanuda el motor `perp-observed-1.0` ni se modifica su cadena.
Un nuevo motor `perp-gap-close-1.0` produce un **escenario estimado de cierre**,
vinculado al prefijo original reproducido y a una confirmación expresa del
intervalo desconocido. No se llama operación observada ni adquiere autoridad REAL.

La ausencia de cobertura impide probar supervivencia, stops o señales entre
puntos. El escenario conserva la posición hasta los puntos de financiación
públicos verificables, aplicando las reglas originales de forma experimental.
En el punto actual usa reglas/especificación recibidas y bid/ask con coste y
slippage originales. Puede terminar en liquidación estimada en un punto conocido;
no afirma que sucediera entonces. Nunca interpola velas, stops ni señales.

## Cálculo e invariantes

- Cuenta reconstruida desde seed/inputs originales; ningún checkpoint cargado
  se convierte en cuenta. Fuente fijada por ID, secuencia, head, seed y origen.
- Sólo posición abierta, cuenta sin observador/cursor operativo. Ningún arranque
  ni orden nueva como efecto lateral. Nuevas lecturas verifican ambos cálculos.
- Funding conserva ancla, orden, cobertura y registros reales; sin ceros por
  defecto. Sus MARK y pagos son reconstrucción conocida ahora, no observación
  pasada. Reglas actuales no se aplican retrospectivamente a puntos antiguos.
- Al final MARK/reglas actuales antes del cierre reduce-only; especificación
  actual, bid/ask recientes, tick adverso, comisión y apalancamiento fijados.
- Libro Decimal compartido, liquidación/seguro/margen de ese libro, sin fórmulas
  nuevas en el navegador. Cantidad completa o error explícito, sin redondear
  una posición y declarar cerrado el resto.
- Contexto de resultados `ESTIMATED_GAP_CLOSE`, autoridad NONE y periodo
  desconocido conservados junto a importes, diario y marcadores nuevos.
- Un cursor vivo conserva una lease compartida de archivo por sesión; el
  cierre exige exclusividad real entre procesos. Se libera al invalidar/detener
  el cursor, y el SO la libera al morir el proceso. Esta lease de propiedad dura
  durante la recogida y evita dos cierres concurrentes; no bloquea otras sesiones,
  el bloqueo de claves ni mantiene una transacción o mutex Python durante HTTP.
- HTTP sin SQL/mutex de sesión retenidos. Bloquear/reautenticar cancela la
  publicación mediante época; cambios del head original impiden publicarla.
- Idempotencia durable: una respuesta perdida recupera el mismo cierre, sin
  otra consulta de mercado. Una sola resolución exitosa por sesión.

## Persistencia y exportación

GRVT PAPER 13→14 añade una tabla de cierres, sin alterar las tablas/filas
publicadas. Guarda recibo público, vínculo original y evidencia calculada/hash.
El acceso sigue siendo cifrado, local y protegido por épocas. Backup/restauración
comprueban las tablas nuevas además de las anteriores.

Una exportación propia enlaza cierre e histórico original completo. El verificador
reproduce los inputs originales, obtiene su estado calculado y aplica el nuevo
motor. Footer obligatorio y digest detectan truncamiento. No cambia el JSONL
observado v1 ni sus resultados; exportar no importa ni activa sesiones.

## Flujo y aceptación

Biblioteca → sesión interrumpida → **Estimar cierre** → revisión/aceptación del
hueco → precio público → resultado estimado guardado. **Ver cierre** muestra
métricas/diario/marca de esa estimación aparte de la inspección original.

Pruebas necesarias: LONG/SHORT, cross/aislado, spread/costes/tick, financiación
una vez, liquidación en puntos conocidos, huecos/futuros/ancla revisada,
rechazo de sesión activa o sin posición, alteraciones, claves/épocas y carreras,
idempotencia después de reapertura, copia/export completo y recorrido navegador
con servidor/trabajador reales y proveedor controlado. No se usan claves/fondos
ni se abren archivos del propietario. El paso a REAL sigue siendo otro tramo.

## Evidencia preliminar

21 casos de cálculo y una tanda integrada de 92 casos pasan sobre fuentes de
ese corte. El navegador en dos procesos verifica compra observada, GAP real del
trabajador, aceptación explícita, respuesta de cierre perdida recuperada por
GET, precio/marca violeta y ausencia de fill observado nuevo, JSONL compuesto,
reapertura idéntica sin red/activación y eliminación de valores al bloquear.
El primer intento del navegador tenía una expectativa de texto decimal sin
los 18 decimales financieros; sólo esa prueba se corrigió y se conservó el
fallo original. No se redujo la precisión de los importes.

Evidencia preliminar en `~/.local/state/orbis/verification/v106-gap-close-dev/`
y `v106-gap-close-browser-r2/`. Las fuentes posteriores añaden comprobaciones
más fuertes de exclusión, renovación de clave/cabecera durante HTTP y consultas
pendientes. Requieren la regresión final; aún no se reinicia el backend.

## Entrega final verificada

4.483 casos / 385 archivos pasan en ocho procesos sobre la aplicación final.
Una reparación posterior sólo cambia el fixture de navegador para comprobar la
pérdida de red, sin depender de un aviso transitorio que la recuperación automática
podía sustituir. Colección exacta y bytes restantes cotejados; no se afirma otra
ejecución completa tras esa modificación no funcional. Once recorridos afectados
pasan por etapas, con el nuevo en dos procesos repetido; otros 42 se conservan.

La prueba propia de carga usa 100.000 velas, una posición/ocho eventos originales
y 721 tasas para treinta días simulados: cierre 8,47 s, replay 4,06 s, con oráculo
financiero independiente. No prueba observación real de treinta días ni largos
linajes de operaciones. Evidencia cargada:
`~/.local/state/orbis/verification/v106-gap-close-r2/verification.json`.
[Uso](PERP_GAP_CLOSE_USAGE.md). REAL, reglas históricas/ejecución avanzada y
las brechas del objetivo completo permanecen pendientes.

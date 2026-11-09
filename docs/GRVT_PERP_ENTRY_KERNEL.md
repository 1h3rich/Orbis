# Núcleo de entrada perpetua REAL

Estado: implementación interna v108 verificada y cargada. La interfaz
sigue indicando que la activación del modelo nuevo está pendiente. Este núcleo
**no crea un bot**, no verifica por sí solo una señal de entrada y no arranca
un coordinador de salidas. No se añade una ruta HTTP que lo habilite.

## Contratos y cálculos

- `PerpRealBudget` fija subcuenta USDT dedicada, límite de colateral completo,
  efectivo por entrada, notional de entrada/salida, techo de comisión, plazos,
  spread y deslizamiento. El límite de colateral reconoce toda la subcuenta;
  limitar el efectivo de una entrada no aísla el resto del saldo CROSS.
- `PerpEntryTerms` fija instrumento, temporalidad, versión/perfil y definición,
  además del presupuesto **REAL independiente**. Conserva el capital inicial
  de la definición para su identidad, pero no lo utiliza para dimensionar.
- `PerpPlanInput` conserva equity real observado, cotización bid/ask/MARK,
  especificación, tramos de margen y sus horas. `PerpEntryPlan` guarda los inputs,
  resultado y hash; reproducirlo vuelve a calcular, nunca carga una cuenta
  desde un checkpoint. Su autoridad es NONE.
- El presupuesto efectivo es el menor entre equity real × asignación de la
  estrategia y el tope REAL por entrada. El libro compartido incluye margen
  inicial, pérdida adversa frente a MARK, reserva de comisión y dos quanta de
  liquidación contable. No anticipa un beneficio favorable frente a MARK.
- Los límites de notional y de las dos salidas se aplican **antes** de recorrer
  los tramos. Recortar después una cantidad válida podría introducirla en un
  tramo donde el apalancamiento del perfil no está permitido.
- Se verifican lotes/ticks, mínimos, precisión de firma, límites, geometría
  long/short y solvencia del libro recién calculado. No se adapta silenciosamente
  el apalancamiento para que una orden resulte ejecutable.

## Correspondencia de ejecución y límites actuales

La correspondencia explícita es **FOK limitado + protecciones nativas LAST**.
No equivale al fill supuesto de mercado del histórico. Las protecciones
iniciales se calculan desde el precio límite de entrada, antes de conocer el
precio medio de los fills; una ejecución favorable puede cambiar su distancia
porcentual respecto al fill real. Las salidas dinámicas, trailing, límites de
permanencia y evaluación de la señal pertenecen al coordinador pendiente.

La comisión usada al reservar es un **techo elegido por el usuario**, no una
tarifa de cuenta verificada. El plan lo declara expresamente. Las comisiones
y financiación conciliadas se obtienen después de ejecuciones/historial reales.
Falta resolver la verificación de tarifa y la respuesta ante una discrepancia
antes de habilitar el bot del modelo nuevo. Ni esta reserva ni una doble
lectura de configuración garantizan el resultado futuro del exchange.

## Preflight específico

`grvt_perp_preflight` toma dos muestras de configuración privada por instrumento,
cuenta/órdenes, catálogo público y reglas de margen. Exige cuenta vacía de otras
posiciones y órdenes, colateral USDT, modo de cuenta compatible y equity dentro
del límite aprobado. Configuración de margen/apalancamiento, saldos y reglas
deben coincidir entre muestras. Una cotización actual aporta MARK y liquidez.

El horizonte completo de observación es de cinco segundos, también comprobado
antes de transmitir. Se conserva el origen de cada dato y su cronología local.
No se cambia ninguna configuración GRVT. Las llamadas HTTP se realizan fuera
de transacciones y comprueban la revocación entre solicitudes.

## Firma, registro y envío

`submit_perp_entry_once` exige confirmación específica y un permiso RAM
revocable del llamador, bóveda cifrada abierta y firmante dedicado. Recoge su
propio preflight, deriva el contrato y vuelve a comprobar el instrumento antes
de firmar. Un plan recibido o una referencia guardada no autorizan ese envío.

El contrato almacenado usa `orbis.grvt.mainnet.bracket.claim.v2` y origen
`PERP_PROFILE_NATIVE_PROTECTED_ENTRY`; conserva el preflight específico completo.
Los registros v1 mantienen su lector y semántica. Relabelar un v2 como v1 con
hashes recalculados se rechaza. No cambian tablas, columnas ni esquemas SQLite.
El lector anterior no comprende el formato nuevo; no se considera un archivo
v1 ni se promete compatibilidad con una versión anterior de ORBIS.

El límite de transmisión es compartido con las entradas existentes: reserva
durable, firma de las tres órdenes, comprobación de permiso/STOP antes del POST
y una sola emisión. Acuse, fill y conciliación siguen siendo hechos distintos.
Una respuesta perdida deja un intento consultable; repetir el ID no envía otro.
Se utilizan las verificaciones existentes de fills, financiación, costes,
recuperación reduce-only, dos auditorías y copias cifradas.

## Verificación y trabajo que permanece

Los tests nuevos usan bóvedas propias, claves de prueba conocidas y transporte
controlado en la frontera HTTP. Recorren el cliente, firma, registro, envío,
lectura, cierre, conciliación y reapertura reales de la aplicación. Incluyen
LONG/SHORT, CROSS/ISOLATED, presupuesto diferente del PAPER, cambios de estado,
funding con ambos signos, respuesta perdida, revocación tras claim y recuperación
del residuo tras salida parcial. No utilizan claves ni fondos del propietario.

Siguen pendientes para la activación: origen LAB/PAPER reproducido ligado a una
concesión REAL versionada, señal actual independiente del saldo/posición
ficticios, presupuesto aprobado, observación del margen/apalancamiento de la
posición real después de los fills, gestión de salidas y tarifa efectiva,
coordinador RAM, GUI completa y pruebas de extremo a extremo del nuevo bot.
El piloto humano mainnet y la aceptación del objetivo completo siguen abiertos.

Tanda final: 4.581 casos / 391 archivos, identidades cotejadas y ocho salidas 0;
ocho recorridos de navegador MAINNET repetidos sobre fuentes finales. Dos casos
leen registros en otro intérprete: un cierre y un intento con respuesta perdida.
La consulta pública real y su plan de capital sintético se conservan aparte de
la evidencia de cuenta/ejecución controlada. Informe de la entrega:
`~/.local/state/orbis/verification/v108-perp-entry-r1/verification.json`.

Véase [plan del flujo completo](PERP_REAL_EXECUTION_PLAN.md).
Referencia de campos: [cuenta y posiciones de GRVT](https://api-docs.grvt.io/schemas/sub_account/).

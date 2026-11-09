# Validación perpetua — v98

Estado: v98 verificada y cargada, con evidencia en
`v98-perp-validation-r1/verification.json`. El objetivo completo sigue abierto.

## Requisitos y decisiones

Este tramo permite evaluar una versión fija sin reutilizar el patrimonio de
desarrollo. Conserva los contratos `perp-ohlc-1.0`, `perp-account-0.1` y
`lab-1.16`; no modifica sus fórmulas, cronología ni resultados publicados.

`perp_validation` publica `perp-validation-1.0` y un plan estricto:

| Método | Resultado | Límites explícitos |
| --- | --- | --- |
| Reserva cronológica | Desarrollo y reserva final con el mismo perfil | Fracción 0,5–0,8; al menos 30 velas en cada tramo |
| Ventanas sucesivas | 2–5 reservas disjuntas con desarrollo creciente | Al menos 30 velas por tramo; no reoptimiza ni elige parámetros |
| Sensibilidad de recorridos | Cuatro pares de trayectorias TRADE/MARK | No es fuera de muestra ni un intervalo que contenga toda ejecución real |

Cada ventana reinicia capital, posiciones, garantías, órdenes, financiación
consumida y estado de liquidación. Los indicadores usan únicamente el prefijo
causal, incluyendo la última vela cerrada anterior al inicio. El funding de esa
apertura precede a la entrada; no se cobra a una posición que todavía no existía.
Una liquidación en desarrollo no impide iniciar la cuenta nueva de la reserva.
Los cierres de fin de ventana son artificiales y se identifican como tales.

No se suman curvas, rendimientos ni beneficios de estas cuentas independientes
para presentarlos como una cartera continua. Se conservan métricas y huella del
resultado completo de cada hijo, no sólo una cifra final. La repetición calcula
de nuevo todas las operaciones/eventos y coteja esas huellas. Ver los resultados
antes de diseñar la estrategia puede contaminar la reserva; el registro no
demuestra independencia estadística. Las muestras pequeñas llevan aviso propio.

## Persistencia y concurrencia

LAB 11→12 añade `perp_validations`, con referencias a dataset y perfil, JSON
inmutable y huella única. No cambia el formato de `perp_runs`, datasets ni
estrategias. Los lectores adjuntos de tablas antiguas aceptan explícitamente 12;
las versiones desconocidas se rechazan. Los montajes antiguos eliminan la tabla
nueva antes de representar una versión anterior; no se relaja DDL para tolerar
fixtures que contengan tablas futuras.

`PerpValidations` requiere LAB cifrado. Calcula fuera del bloqueo SQL, conserva
identidad de archivo/clave/época y relee originales bajo el commit. Un bloqueo o
reauthenticación con la misma contraseña invalida la operación. El listado dice
`HASH_AND_ORIGINALS_ONLY`; detalle y exportación recalculan y sólo entonces dicen
`REPLAY_VERIFIED`. Las copias cifradas verifican todas las validaciones y sus hijos.

**Ver** materializa explícitamente una prueba hija deduplicada en el archivo
común y la abre en su gráfico. No hace escrituras en un GET ni crea otro tipo de
simulación que parezca una prueba archivada. Perfil, run y reproducción propagan
la misma época de acceso; se impide continuar una inspección bajo otra sesión.
Una prueba hija guardada antes de un fallo posterior sigue siendo un experimento
válido, pero no se presenta como la ventana seleccionada sin la comprobación final.

## Contratos y autoridad

- `orbis.lab.perp.validation.result.v1`: plan, motores, originales, ventanas,
  métricas, huellas completas y advertencias deterministas.
- `orbis.lab.perp.validation.v1`: ejecución persistida, original y resultado.
- `orbis.lab.perp.validation.bundle.v1`: exportación con datos, definición,
  perfil y resultado. Los diarios individuales se inspeccionan/exportan como
  pruebas hijas mediante el archivo común.
- `orbis.research.perp.reference.v1`: referencia tipada a una reserva fija o
  ventanas sucesivas, con perfil, motores, hashes, periodos, muestra y autoridad
  `NONE`. Se reproduce contra originales antes de emitirla. Sensibilidad no
  puede exportarse como una reserva fuera de muestra.

La referencia de investigación de precios v1 permanece intacta. Las puertas
REAL existentes no consumen esta referencia nueva; tampoco se cambia PAPER ni
se activa ningún trabajador al crear, inspeccionar o exportar una validación.
La compatibilidad PAPER y la revisión REAL del nuevo motor requieren otro tramo.

## Interfaz y aceptación en desarrollo

`Laboratorio → Validación de derivados` reutiliza el perfil guardado en
**Pruebas de derivados**. El usuario elige datos/método y revisa cada tramo;
**Ver** abre sus operaciones, marcas, curva y diario en el componente existente.
No hay motores financieros en JavaScript: los importes exactos permanecen como
texto; sólo la presentación redondea. Cambiar configuración, bloquear LAB o
recibir una respuesta atrasada oculta el resultado anterior. La biblioteca pagina.

- 32 casos nuevos: capital fresco y señal anterior al corte, reservas disjuntas,
  cifras manuales de funding en frontera, liquidación previa sin estado heredado,
  invariancia ante sufijo futuro y contexto Decimal externo, cuatro escenarios,
  parámetros ambiguos rechazados, cifrado/copia/reapertura, duplicados,
  reautenticación y manipulación con hash nuevo.
- Recorrido real de UI en dos procesos: LAB vacío, descarga por adaptador GRVT
  real con datos públicos controlados, estrategia, tres métodos, referencias,
  inspección de tramos en gráficos, edición posterior de versión y copia cifrada.
  Reapertura exacta, respuesta atrasada, corrupción, bloqueo y escritorio/móvil.
- El primer recorrido crea correctamente los tres informes y falla al reabrir
  porque el test esperaba un único perfil. Inspeccionar otro recorrido crea
  legítimamente otro perfil; se corrige sólo la expectativa de reapertura.
  El segundo recorrido pasa completo; los intentos se conservan como evidencia.
- Una tanda previa de módulos afectados pasa 212 casos; después se añaden dos
  oráculos de frontera/liquidación y vuelven a pasar los 32 casos nuevos. La
  regresión final sobre fuentes fijadas pasa 4.262 casos / 370 archivos, sin
  fallos/errores/omitidos. Nueve recorridos actuales pasan; se conservan otros
  42 de funciones sin cambios (51 total), sin afirmar otra tanda completa de UI.
  Backend y módulos/activos se comprueban después del reinicio autorizado.

## Rendimiento medido

Medida sobre 5.000 velas propias, después de terminar la regresión: reserva
4,431 s; cinco reservas sucesivas (10 cuentas, 16.662 velas modeladas) 14,846 s;
cuatro escenarios (20.000 velas modeladas) 17,083 s. Los informes ocupan
4–18 KB sin duplicar diarios. Es cálculo puro, sin acreditar latencia HTTP,
persistencia o UI. Evidencia: `validation-benchmark.json` en la entrega v98.

## Pendientes del objetivo completo

Semántica PAPER compatible con este motor; nuevos controles de procedencia en
revisión REAL; reglas históricas acreditadas para modo realista; órdenes/posiciones
avanzadas; señales omitidas visibles en la UI; bibliotecas grandes/rendimiento;
cifrado gráfico de PAPER manual; conciliación DEX restante; auditoría global y
piloto humano mainnet. No se usa ninguna wallet, clave ni fondo del propietario.


## Continuidad PAPER: comprobación del código actual

El PAPER GRVT existente ya ejecuta señales, registra funding público y controla
riesgo observado: `SignalMonitor`, `AutoFunding`, `PaperPerpStore` y
`SignalRiskWorker`. No debe presentarse como un módulo sin pagos ni ejecución.
Conserva su libro aislado y sus estados `GAP`/`STOPPED`/riesgo no determinado.
La investigación de precios v1 enlaza ese flujo y debe seguir siendo reproducible.

El siguiente tramo debe añadir sesiones con otro contrato explícito, preservando
las anteriores. Compartirá `perp-account-0.1`, el evaluador fijado, sizing, fees,
reglas de cierre y políticas de margen con la investigación; la recepción de
observaciones actuales sustituirá la trayectoria OHLC hipotética. Ticker TRADE,
bid/ask, MARK, financiación, tiempos y reglas deben archivarse por separado con
orden de llegada y secuencia. Una interrupción no permite inventar ticks ni fills
retroactivos: el intervalo desconocido se registra y la sesión queda pendiente de
resolución o parada según su exposición. Reapertura nunca reactiva por sí sola.

Antes de ofrecer el handoff se necesita prueba de conformidad: la misma secuencia
explícita de marcas/fills/funding sobre ambos consumidores produce el mismo libro,
además de oráculos independientes. No se exige que un backtest OHLC y un mercado
observado den la misma rentabilidad. Las diferencias de liquidez, precio de fill,
reglas actuales y trayectoria deben poder explicarse. La referencia OOS nueva se
verifica contra originales antes de crear una sesión ficticia, sin heredar su
capital final y sin ampliar ninguna autoridad REAL.

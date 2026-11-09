# Validación fuera de muestra y procedencia de candidatos

Estado: reserva DCA implementada en v88. En v89 se comparte el verificador LAB y
se incorpora la referencia de investigación tipada y exportable. El paso 2 está
terminado. En v90 MEXC enlaza la referencia a sus activaciones y rearmes nuevos.
GRVT se incorpora en v91, verificada y cargada. DEX se incorpora en v92, verificada y cargada. La versión
activa se comprueba en el índice de evidencia.
La entrega cargada vigente se consulta en `~/.local/state/orbis/verification/latest.json`.
Alcance de producto: §7.4, investigación profesional de DCA
y LONG/SHORT, y decisión A-13. El objetivo completo se conserva en la aceptación
funcional; esta entrega no se limita a emitir una marca de verificación.

## Secuencia

1. DCA: reservar un tramo final con las mismas reglas y costes; presupuesto,
   posiciones, activaciones, calendario y consumos independientes. Se conservan
   sólo referencias de precio ya conocidas antes del corte. Comparar condicionado,
   fijo y compra inicial en desarrollo y reserva, guardar, repetir y recuperar.
2. Compartir la verificación de experimentos LAB/DCA mediante servicios de dominio
   con versiones explícitas. La API actual de LONG/SHORT ya guarda sus huellas;
   su semántica publicada se preserva al hacerla reutilizable.
3. Definir una referencia de investigación para nuevas vinculaciones a bots en
   MEXC, GRVT y DEX, con dataset, versión de reglas, motor, método, corte y hashes.
   La evidencia no concede permisos ni activa órdenes. Integrarla con versiones
   nuevas de vinculaciones, conservando la interpretación de registros anteriores.

## Primer corte funcional DCA

- Módulos: `automation.dca` (ejecución compartida por ventana),
  `automation.validation` (corte y semántica), `automation.verification`
  (comprobación reproducible), almacenamiento/API y sección compacta en DCA.
- Método nuevo `DCA_CHRONOLOGICAL_HOLDOUT`, motor `dca-holdout-1.0`, simulador
  subyacente fijado `dca-1.6`. El motor normal y sus resultados publicados
  conservan exactamente la salida anterior cuando no se solicita ventana.
  El método de reserva fija contexto decimal: 300 dígitos, HALF_EVEN, exponentes
  ±999999 y traps estándar; no hereda precisión, redondeo, límites ni traps del
  llamador. Las tres variantes se calculan en ese contexto independiente.
- Corte 50–80 % de desarrollo, mínimo 30 velas por tramo. En reserva, la vela
  anterior es contexto de señal, no una operación del tramo de desarrollo.
  Presupuesto completo, cero unidades/consumos/activaciones al comenzar;
  referencia inicial original o máximo del prefijo conocido, según el plan.
  La compra inicial de comparación se intenta en la primera apertura reservada.
- Datos nuevos: tabla `dca_validation_runs` en automatizaciones, esquema 6
  mediante migración aditiva desde 5, con versión de regla/dataset, hashes,
  método, corte, versión del simulador y resultado. Los runs normales no se
  convierten en validaciones. Cifrado, bloqueo, copias y restauración incluyen
  la entidad nueva. REAL permanece en su esquema actual.
- El guardado revalida la versión y el dataset adjunto en la misma transacción
  SQLCipher que inserta el resultado. Una lectura verificada repite el cálculo;
  una divergencia impide presentar la reserva como comprobada.
- UI: controles «Fuera de muestra», corte, «Validar», resultados guardados y
  tabla desarrollo/reserva con cifras exactas accesibles. Sin activaciones de
  PAPER/REAL ni enlaces que aparenten una vinculación todavía no implementada.
- Pruebas: resultados normales idénticos a v87; capital independiente; compras
  sólo desde el corte; referencia sin futuro; costes/límites; tres variantes;
  hashes, edición concurrente, cifrado/migración/copia/reapertura y flujo de UI.

## Interpretación

Una repetición coincidente prueba reproducibilidad. La reserva sólo es
independiente si no se usó para elegir la estrategia o sus parámetros. Cambiar
el corte tras mirar resultados también contamina el estudio. Ni el beneficio
positivo ni una muestra pequeña autorizan una afirmación de robustez. El
seguimiento PAPER observado sigue siendo una evidencia distinta.

## Referencia común — v89

`research.references` publica `orbis.research.reference.v1`, con familias
STRATEGY (LONG/SHORT) y DCA. Admite reserva cronológica y ventanas sucesivas de
versión fija. No emite referencias de ganadores del optimizador: sus parámetros
pueden diferir de la definición base guardada.

La referencia contiene UUID de artefacto, definición, versión y dataset;
huellas del artefacto completo, definición y datos; procedencia, temporalidad,
periodo, motores fijados, método, ventanas reservadas, tamaño y advertencias.
Su SHA-256 cubre el JSON canónico salvo el propio campo `sha256`. Es una huella
de integridad, no una firma ni una autorización; `execution_authority` es NONE.
No incluye claves, saldos, operaciones ni curvas de resultados. El consumidor
debe reconstruirla desde sus originales locales mediante `revalidate_reference`:
un archivo con hash recalculado no acredita que sus datos sean auténticos.

La construcción repite la validación con el servicio de dominio, comprueba
huellas y relee entradas. El verificador LAB extraído conserva el cuerpo anterior
idéntico por AST y las clasificaciones de registros legados/motores no disponibles.
No cambian simuladores, fórmulas, esquemas ni formatos de ejecución existentes.

LAB y DCA ofrecen «Referencia». LAB requiere primero «Verificar»; la descarga
vuelve a comprobar el original. Ambos descartan respuestas tardías al cambiar
de prueba o pantalla. Una alteración del original bloquea la descarga y retira
el estado comprobado. La aplicación completa recupera referencias idénticas
tras cerrar el proceso, desbloquear sus bases cifradas y volver a abrirlas.

## Vínculo de MEXC — v90

La API de activación y rearme exige `research_validation_id` para una autorización
nueva. Omitirlo sólo permite repetir de forma inerte un pedido antiguo ya
registrado; no crea trabajadores ni prolonga permisos. Las primitivas internas
conservan el soporte de formatos antiguos, sin crear una puerta HTTP alternativa.

`research.mexc_binding` publica `orbis.mexc.bot.research.v1`: referencia de
investigación, identidad/huella de origen PAPER, dataset inicial y tramo observado.
Se comprueban versión, definición, motor, mercado, temporalidad, datos públicos
MEXC y corte anterior o igual al fin del histórico inicial PAPER. Se admite un
histórico de investigación más antiguo y distinto; si se solapan sus velas con
el histórico inicial PAPER, deben coincidir. Las reservas tienen al menos 30
velas por ventana. No se exige beneficio histórico ni se promete independencia.

Antes de consultar la apertura se repite la validación original. Tras la consulta
se releen artefacto, versión, dataset y semilla; una modificación impide guardar
la autorización. El registro cifrado conserva una instantánea tipada con huella
junto a cada autorización. Leerla comprueba identidad y coherencia con ese
origen, sin exigir LAB desbloqueado ni repetir el histórico en cada ciclo.
Reanudar concede otro permiso sólo después de comprobar de nuevo los originales.

DCA archiva expresamente `RESET_AT_REAL_ACTIVATION`, tiempo y precio de su
referencia REAL. La reserva histórica conserva su contexto original; no se
equiparan sus precios iniciales ni se copia capital ficticio. El rearme y la
renovación continua conservan la referencia REAL inicial y la investigación de
su autorización. Un rearme puede elegir otra validación compatible, archivada
como evidencia nueva; las anteriores permanecen intactas.

La UI ofrece «Reserva», acceso «Validar» y la referencia vinculada en la tarjeta.
El descubrimiento de opciones sólo comprueba metadatos: se etiqueta
`ON_ACTIVATION`, sin presentarlo como una nueva repetición. La confirmación del
backend es obligatoria. Un bot antiguo aparece como «Legado · sin reserva»;
leerlo o repetir su pedido no lo convierte en una autorización investigada.

Pasan las pruebas de rechazo por versión, mercado, falta de procedencia, datos
posteriores a PAPER, motor ausente, velas comunes contradictorias, resultado
alterado y cambio durante la apertura. Se comprueban snapshot/huellas, legado,
rearme, DCA y conservación a través de renovación continua. Los recorridos de
navegador prueban compra/venta y wallet↔MEXC con transportes propios; el piloto
humano mainnet sigue siendo una comprobación distinta.

## Vínculo GRVT — v91 cargada

Las revisiones MAINNET nuevas exigen una reserva de versión fija y un origen
AUTO PAPER activo con backtest fijado y al menos una observación reproducible.
Se comprueban motor, dirección LONG/SHORT, versión/definición, mercado,
temporalidad y datos públicos GRVT; la investigación termina antes del tramo
observado en PAPER. Otro histórico anterior es válido si sus velas comunes
coinciden. Las señales se repiten desde el archivo encadenado, con el replay
completo disponible para reglas multitemporales.

`orbis.grvt.bot.research.v1` archiva la referencia, identidad/digest de origen,
backtest y su huella, dataset inicial y un prefijo observado (cantidad, última
vela, huella de observaciones, cabeza de cadena y tiempo de observación).
PAPER puede añadir velas entre revisión y activación: el prefijo revisado debe
seguir intacto. Revisión y activación repiten los originales, y antes del commit
se releen artefacto, versión, datos y prefijo. Una contradicción consume/rechaza
la revisión sin iniciar órdenes; un nuevo intento necesita otra revisión.

La revisión sellada usa `orbis.grvt.mainnet.bot.review.v2` y las concesiones
nuevas `orbis.grvt.mainnet.bot.grant.v3`. Los formatos de concesión v1/v2 siguen
con su interpretación original. La evidencia no concede autoridad: firma,
límites, permisos RAM y STOP mantienen sus controles anteriores. Los tiempos
en nanosegundos de la referencia se serializan como texto antes de calcular
hash y MAC; no se dejan convertir en números imprecisos de JavaScript.

El modelo histórico se declara expresamente
`PRICE_RULES_WITHOUT_DERIVATIVES_MARGIN_OR_FUNDING`. El laboratorio repite reglas
de precio; este vínculo no añade un motor histórico de margen, financiación o
liquidación. La UI lo identifica como «Histórico · Sin margen/funding». PAPER y
la operativa MAINNET conservan sus propias cantidades, costes y verificaciones.

La UI permite seleccionar la reserva, revisarla con los límites y consultar
su referencia en el registro. La lista de opciones sólo descubre metadatos
(`ON_PREVIEW_AND_START`); no equivale al replay de la revisión. Una sola opción
se elige por defecto, y una selección distinta invalida la revisión anterior.

El siguiente tramo debe enlazar esta evidencia a formatos nuevos de DEX.
Los registros anteriores conservan su interpretación. La referencia por sí sola
no demuestra independencia estadística ni habilita una orden.

La puerta DEX aún no consume esta referencia OOS; tampoco se atribuye
retroactivamente esa comprobación a autorizaciones antiguas de MEXC o GRVT.


## Vínculo DEX — v92 cargada

`research.dex_binding` repite una reserva LAB/DCA de la misma versión,
definición, motor, BNB/USDT y temporalidad que el origen PAPER. La investigación
termina antes o al final de la semilla PAPER y las velas comunes coinciden.
Cada ventana reservada tiene al menos 30 velas. La preparación HTTP v2 identifica
por separado las señales públicas MEXC y la ejecución USDT/WBNB en Uniswap v3,
BNB Chain. El modelo declara `SIGNAL_MARKET_WITHOUT_DEX_EXECUTION_COSTS`:
no acredita precios, liquidez, gas o ejecuciones históricas DEX.

La evidencia se almacena en dos tablas nuevas del esquema REAL 72:
`real_dex_research_bindings` y `real_dex_research_authorizations`. Sus formatos
`orbis.dex.research.binding.v1` y `orbis.dex.research.authorization.v1` apuntan
al digest exacto del encabezado financiero. Vinculación y activación escriben
su evidencia dentro de la misma transacción; nunca conceden un permiso sólo
por encontrar una prueba en disco. No se cambian los formatos de origen v1,
vínculo financiero v1/v2, LONG v1–v5 ni DCA v1/v2.

Cada activación o rearme HTTP nuevo repite la reserva original antes de conectar
la firma y vuelve a leer sus metadatos antes de confirmar. Una reserva nueva
compatible puede elegirse al rearmar, conservando pruebas anteriores, posición,
presupuestos, cursor y operaciones. El contexto de activación guarda la vela y
el cierre efectivos. Para DCA incluye `initial_reference` y `skip_through_t`
exactos del encabezado: la primera activación reinicia su referencia en el último
cierre conocido; el rearme conserva la referencia y los consumos anteriores.
No copia capital ni la referencia contable de una simulación histórica.

Los registros se verifican sin abrir LAB/PAPER en lecturas y copias cifradas.
Rearmar sí requiere acceso a los originales LAB/DCA; no requiere reactivar el
observador Spot PAPER archivado. El trabajador fija en RAM el digest de su
calificación y comprueba su integridad en el arranque y antes de nuevas órdenes.
Falta o alteración bloquea la emisión. STOP y la observación de recibos pendientes
no dependen de que esa evidencia pueda leerse. La comprobación no introduce
consultas SQL en el guard de permiso usado por estado/STOP.

Los registros anteriores se leen como legado y las repeticiones de solicitudes
son inertes. No se inventan reservas en la migración. La ausencia de un registro
complementario tras reabrir indica «sin reserva archivada»; por sí sola no permite
distinguir un legado de la eliminación externa de ese registro. No concede
nueva autoridad: toda activación nueva vuelve a los originales; un trabajador
existente conserva la huella esperada y rechaza su eliminación.

La migración 71→72 se prueba exclusivamente con bóvedas propias: copia cifrada
previa, conservación de encabezados/estados financieros, lectura y copia posterior.
No se abre ni migra la bóveda del usuario para verificar esta entrega.


La regresión completa de v92 detectó el rearme HTTP de una política sucesora:
el vínculo renovado no tenía una calificación propia y, sin un ID nuevo en el
cuerpo, se perdía la selección del permiso anterior. La corrección recupera
primero la reserva de ese permiso verificado cuando el cliente no elige otra;
siempre la repite desde los originales antes de conceder. La interfaz usa la
misma prioridad después de refrescar o reabrir. No copia autoridad, posiciones
ni resultados de investigación. Un legado sin prueba anterior sigue necesitando
selección explícita. Prueba HTTP de renovación y prueba de navegador con dos
reservas, selección alternativa, parada/rearme y reapertura.


Evidencia cargada: `~/.local/state/orbis/verification/v92-dex-research-r3/verification.json`.
El informe distingue la tanda completa fallida, las 75 comprobaciones finales de
los archivos afectados, 3.975 casos sin cambios y 17 recorridos finales de
navegador con 28 anteriores sin cambios. Se cotejan 4.050 identidades actuales;
no se atribuye una repetición completa final ni una operación mainnet realizada.

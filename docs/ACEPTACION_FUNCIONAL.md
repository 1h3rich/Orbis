# Aceptación de la versión funcional de ORBIS

Revisión: 7 de noviembre de 2026. La referencia es
[ORBIS_ESPECIFICACION_V0.2.md](ORBIS_ESPECIFICACION_V0.2.md), junto con las
ampliaciones del propietario: wallet, MEXC, GRVT, un DEX, ejecución automática
con límites y parada, y probar un bot antes de activarlo con capital real.
La especificación completa sigue siendo el objetivo; una entrega no equivale
a completarla. El índice de evidencia cargada es
`~/.local/state/orbis/verification/latest.json`.

## v115 · recuperación de recibos externos

v115 carga la revisión de recibos externos. Se comprobaron los 52 recursos del navegador, 15 rutas privadas y la reapertura de la interfaz con la bóveda REAL cerrada. La tanda general de 5.427 casos pasa 5.426 y detecta una expectativa antigua en la lista de capacidades. Se corrige sólo esa expectativa y pasan los 14 casos del módulo. La cobertura final coteja las 5.427 identidades en 454 archivos. Pasan 13 recorridos de navegador. Evidencia en `~/.local/state/orbis/verification/v115-external-recovery-r1/verification.json`.

## Verificaciones previas de REAL 79

La [interfaz de recuperación](EXTERNAL_RECOVERY_UI.md) ya supera en desarrollo
los recorridos de captura desde historial, dos wallets, respuesta perdida,
reapertura sin wallet y cambio de contexto. En r19 pasan 13 recorridos afectados
y cinco casos focales sobre 1.319 fuentes, incluida aceptación mixta firmada
LONG/DCA. Esta etapa precede a la publicación documentada arriba.

La API conjunta posterior pasa 249 identidades/21 módulos, con 15 casos HTTP
nuevos además de los 21 de almacenamiento incluidos, cuatro salidas 0 y 1.312
fuentes iguales. Hay preview/confirmación privada con todas las pruebas EOA,
recuperación fría por grupo sin RPC y rechazo de cierre de bóveda/cambios de
fuentes durante el cálculo. No hay interfaz conjunta ni publicación. Faltan
fuentes mixtas firmadas, navegador, rendimiento ampliado y aceptación global.
[Contrato y evidencia](EXTERNAL_BATCH_ACCEPTANCE_PLAN.md).

La aceptación conjunta persistente posterior pasa 220 identidades/18 módulos
con 21 casos nuevos y cuatro salidas 0 sobre 1.310 fuentes iguales. Guarda todos
los bookings y originales con un único criterio, revierte fallos en cuatro
fronteras y recupera por grupo o fuente desde una copia cifrada. La migración
conserva criterios individuales v7; otra revisión posterior conserva el origen
y el gas de cada recibo. Los contextos/alias de toda custodia comprobada siguen
bajo el bloqueo de pendientes. Sin publicar: faltan API conjunta, interfaz,
rendimiento ampliado y la aceptación global. [Decisiones y evidencia](EXTERNAL_BATCH_ACCEPTANCE_PLAN.md).

La propuesta conjunta de varios recibos se verifica después en 164 casos/14
módulos sobre 1.307 fuentes, incluidos 18 nuevos y cuatro salidas 0. Calcula el
coste de traslados dependientes y ventas posteriores, conservando gas y unidades.
Ese corte aún no aceptaba ni recuperaba grupos; el tramo 79 anterior añade la
persistencia, migración y recuperación. Faltan API e interfaz conjuntas.
[Plan y evidencia](EXTERNAL_BATCH_ACCEPTANCE_PLAN.md).

La validación externa dentro de una lectura protegida se verifica después en
254 identidades/32 módulos, cuatro salidas 0 sobre 1.304 fuentes. Incluye siete
casos nuevos de invalidación, corte histórico y reinicio SQL DELETE/WAL.
El recorrido DCA propio completo pasa de 162,689 s a 26,053 s; no acredita
rendimiento global o linajes extensos. [Decisión y evidencia](EXTERNAL_OUTCOME_READ_SCOPE.md).

El resultado efectivo y la continuación explícita LONG/DCA en la misma asignación
se verifican después en 182 casos/22 módulos, cuatro salidas 0 e identidades
cotejadas sobre 1.302 fuentes. Incluye compra DCA posterior y reapertura cifrada,
sin fills inferidos. [Evidencia y límites](EXTERNAL_EFFECTIVE_OUTCOMES.md).
La renovación entre políticas se verifica después en 247 identidades/31 módulos
de dos tandas sin solapamiento sobre 1.303 fuentes, con dos casos nuevos.
Conserva consumo raíz y permite nueva operativa y segunda preparación;
[evidencia](EXTERNAL_POLICY_RENEWAL.md). Faltan varios pendientes conjuntos,
interfaz, rendimiento y verificación antes de publicar. La evidencia anterior se conserva
como el corte histórico que se detalla a continuación.

Sin publicar. La captura de wallets y revisión del esquema 77 se amplía con
originales, booking, criterio y enlace en un solo commit. Hay confirmación por
API, recuperación privada sin reconectar wallet, reproducción fría, migración
cifrada y conservación del origen manual. El criterio 3 hereda fuentes y permite
revisiones y recibos conocidos posteriores, sin etiquetar una estrategia como
ejecutada ni reiniciarla. [Contrato](EXTERNAL_FINANCIAL_ACCEPTANCE.md).

Pasan diez casos iniciales, después 59 del alcance ampliado y cuatro de compra
posterior, normalización y operador. Son tandas de desarrollo, no se suman como
cobertura independiente. Batería integrada: 691 correctos y un fallo de montaje
en la prueba MEXC del nuevo campo de alcance. Corrección limitada al constructor
del test y repetición de sus siete casos: todos pasan. Aplicación idéntica, resto
del módulo igual por AST. Cobertura de **692 identidades/53 módulos**, sin afirmar
una segunda tanda completa limpia. Incluye 16 casos nuevos de dos módulos.
Faltan resultado efectivo de bots, varias observaciones pendientes
conjuntas, UI y verificación de publicación. La instancia original y `latest.json`
siguen en v114; este tramo no completa el objetivo total.

## v233 cargado · Continuidad del observador Spot PAPER

Spot PAPER agenda el próximo cierre o control de riesgo con el estado resumido de las sesiones activas. Al llegar el momento de observar, sigue comprobando el archivo completo antes de tomar una decisión. Bloquear la bóveda invalida las observaciones y cierres manuales aún sin guardar, incluso si se vuelve a desbloquear.

Pasaron 177 pruebas en 8 módulos y 3 recorridos Chromium. Se comprobaron compras, salidas de riesgo, cierre manual, reversión de cambios, reapertura cifrada y detección de huecos tras reiniciar. El límite operativo sigue en 5.000 velas por sesión. [Alcance](PAPER_CONTINUITY.md#planificación-del-observador-spot).

## v232 cargado · Comparar margen y apalancamiento de derivados

**Pruebas de derivados → Margen y apalancamiento** repite la prueba guardada con margen aislado, cruzado o ambos. Cada escenario indica capital, drawdown, entradas bloqueadas y, si se produce una liquidación, su instante y precio MARK del modelo. Se conserva la configuración original y los escenarios son experimentales.

Pasaron 166 pruebas en 10 módulos y 5 recorridos Chromium con reapertura. Se comprobaron liquidaciones LONG y SHORT, entradas bloqueadas, perfiles LIMIT y parciales, y la reproducción de la misma exportación.

## v231 cargado · Abrir el ingreso o la retirada pendiente en MEXC

**Wallet ↔ MEXC** lleva directamente a la ficha del ingreso o retirada pendiente de la cuenta seleccionada. La ficha se resalta al cargar el historial, incluso si está fuera de las cien entradas recientes. La guía conserva sus consultas locales tras desbloquear la bóveda.

Pasaron 166 pruebas en 10 módulos y 5 recorridos Chromium. Se comprobó el foco exacto de ambos movimientos, el aislamiento entre cuentas y el recorrido de compra, venta y retirada con proveedores de prueba.

## v230 cargado · Exportar el diario de Spot PAPER

**Spot PAPER → Exportar JSONL** guarda el contexto inicial, las velas observadas, las señales, las ejecuciones ficticias y los huecos de riesgo. Los resultados quedan separados de los datos de decisión y los cierres enlazan con su entrada por episodio. La descarga local requiere confirmar la revisión vigente del diario y verifica la huella del archivo.

Pasaron 184 pruebas en 9 módulos y 4 recorridos Chromium. Se comprobó la descarga y la cancelación, la misma revisión tras recargar, la revocación de acceso y el rechazo de un diario alterado. Las exportaciones REAL de MEXC y Uniswap se comprobaron con proveedores de prueba.

## v229 cargado · Comparar Spot PAPER con reservas LAB

**Spot PAPER → Comparar reserva LAB** reproduce una reserva cronológica o varias ventanas sucesivas de la misma estrategia, mercado y motor. Coteja la semilla, las señales, el libro y las comisiones del diario PAPER. La tabla conserva periodos separados y avisa si la reserva se guardó tras iniciar PAPER. La posición ficticia abierta conserva su P/L realizado y sus costes.

Pasaron 180 pruebas en 9 módulos y 3 recorridos Chromium. Se verificaron ambos métodos, el rechazo de señales y costes incompatibles, la reproducción tras recargar y el recorrido de un bot MEXC con proveedores de prueba.

## v228 cargado · Reabrir un plan de retirada MEXC

En **Retirar a wallet → Consultar retiradas**, un plan vigente permite **Revisar plan** tras recargar ORBIS. La revisión coteja la misma cuenta y wallet, el token, la ruta, la comisión, el saldo conciliado y STOP. Se conservan el ID y el vencimiento originales. El envío requiere la credencial de retirada y su confirmación independiente.

Pasaron 147 pruebas en 10 módulos y 4 recorridos Chromium. Se comprobó la reapertura desde el navegador, la ausencia de solicitudes durante la revisión, un solo envío confirmado y la conciliación en BNB. El piloto con fondos reales sigue pendiente.

## v227 cargado · Sensibilidad de costes en derivados

**Pruebas de derivados → Sensibilidad de costes** reproduce una prueba guardada y simula otras comisiones y otro deslizamiento con los mismos datos, reglas y financiación. La tabla compara capital, rentabilidad, comisiones y drawdown. Si una ejecución queda incompleta, se muestran ambos estados y se ocultan las diferencias. El escenario es local y experimental.

Pasaron 163 pruebas en 10 módulos y 6 recorridos Chromium con reapertura. Se comprobó el cálculo exacto para LONG y SHORT con MARKET y LIMIT con parciales, el rechazo de un resultado alterado y la reproducción del mismo JSON tras reiniciar.

## v226 cargado · Histórico de patrimonio de varios años

**Evolución patrimonial** ofrece cinco y diez años. Los cierres se paginan según el proveedor y los días sin precio conservan su hueco. El selector común encuadra fechas dentro de la consulta cargada. La tabla diaria muestra cien filas por página. El gráfico de fills de GRVT TESTNET usa el mismo selector.

Pasaron 92 pruebas en 10 módulos y 6 recorridos Chromium. Se comprobaron diez años de cartera con datos controlados, las fechas visibles exactas, la paginación local y la ocultación de importes. Bitstamp devolvió 1.826 cierres públicos de BTC/EUR entre el 1 de octubre de 2016 y el 30 de septiembre de 2021. [API OHLC de Bitstamp](https://www.bitstamp.net/api/#tag/Market-info).

## v225 cargado · Comparar PAPER DCA con una reserva LAB

**Comparar reserva LAB** reproduce la reserva y el diario observado de la misma versión, histórico y motor. Muestra por separado fechas, compras, costes, caja, patrimonio, rentabilidad y drawdown. LAB valora al cierre y PAPER al bid registrado en cada observación. La comparación deja visibles ambos periodos y sus precios de marca.

Pasaron 225 pruebas en 10 módulos y 6 recorridos Chromium. Se verificaron la valoración exacta, la ausencia de consultas de mercado durante el cálculo, la reapertura y el bloqueo de LAB. El piloto con fondos reales sigue pendiente. [Uso](../README.md#comparar-paper-dca-con-la-reserva-lab).

## v224 cargado · Recuperación por historial de GRVT

Si GRVT devuelve «orden no encontrada», **Consultar** busca las órdenes originales del grupo y los cierres IOC en las ventanas de los contratos archivados. El historial debe acreditar un estado terminal y una identidad única. ORBIS vuelve a verificar la firma antes de incorporar el estado al diario. La conciliación mantiene sus dos comprobaciones financieras.

Pasaron 144 pruebas en 11 módulos y 6 recorridos Chromium. Se comprobó la recuperación completa desde el historial y el rechazo de duplicados, órdenes abiertas, firmas y disparadores modificados, ventanas incorrectas y fallos de autorización. Se usó un proveedor controlado. El piloto con fondos reales sigue pendiente. [Uso](GRVT_BOT_MAINNET.md#consultar-una-operación-con-view).

## v223 cargado · Recuperación de un cierre GRVT con View

**Consultar** recupera por su identificador la orden de cierre IOC ya registrada si se perdió el acuse. La vista distingue la cantidad pendiente según las órdenes de un cierre conciliado. El recibo se archiva sólo cuando dos auditorías confirman fills, saldo y posición cerrada. La conexión View conserva su alcance de lectura en el exchange.

Pasaron 117 pruebas en 11 módulos y 5 recorridos Chromium. Se verificaron LONG, SHORT, cierre parcial, diferencia de saldo, autoridad anterior vencida y reapertura del recibo. Se usó un proveedor controlado. El piloto con fondos reales sigue pendiente. [Uso](GRVT_BOT_MAINNET.md#consultar-una-operación-con-view).

## v222 cargado · DCA por volumen relativo en Uniswap

**Preparar DEX** vincula una sesión PAPER DCA de BNB/USDT con una reserva histórica verificada. El volumen de velas cerradas de MEXC determina la señal. Uniswap v3 cotiza y ejecuta la compra de WBNB en BNB Chain. El bot espera si el cociente es insuficiente y se detiene si cambia una vela de referencia. El diario y el JSONL conservan el periodo, el mínimo y el cociente exacto.

Pasaron 232 pruebas en 14 módulos y 5 recorridos Chromium. Se comprobaron dos compras en el umbral, STOP, renovación de política, rearme explícito y reapertura sin arrancar de nuevo. Las órdenes se probaron en una cadena EVM local. El piloto con fondos reales sigue pendiente. [Uso](DEX_DCA_PLAN.md#volumen-relativo).

## v221 cargado · DCA por volumen relativo en MEXC

**Preparar MEXC REAL** abre la revisión del plan PAPER con volumen relativo. El bot contrasta toda la ventana de volumen con las velas archivadas antes de operar. El diario conserva el umbral y el valor exacto. El rearme conserva las velas y los límites anteriores.

Pasaron 303 pruebas en 16 módulos y 5 recorridos Chromium. Se comprobaron la compra BASE y DIP en el umbral, la espera por volumen insuficiente, la pausa si el proveedor revisa el primer dato de la ventana, la reproducción del diario y la reapertura. La ejecución se comprobó con un exchange de prueba. El piloto con fondos reales sigue pendiente. [Uso](BOT_MEXC_SPOT.md#volumen-relativo).

## v220 cargado · DCA por volumen relativo

La condición DCA de volumen compara la última vela cerrada con la media de las anteriores. Se aplica a la compra periódica y a los extras por caída de la variante condicionada. LAB conserva las comparaciones con DCA fijo y compra inicial. PAPER registra la ventana de velas, la decisión y el saldo ficticio.

Pasaron 276 pruebas en 17 módulos y 6 recorridos Chromium. Se comprobó la igualdad con el umbral, la reserva, las ventanas sin volumen, la reapertura, los días perdidos y el rechazo en REAL. Los 36 resultados completos comparados de los motores anteriores coinciden con v219. [Uso](../README.md#volumen-relativo-en-dca).

## v219 cargado · Mapas de calor en el Laboratorio

Los optimizadores muestran el neto de desarrollo por stop y objetivo. El mapa distingue pérdida, cero, ganancia y combinaciones descartadas, y señala la selección archivada. Las ventanas sucesivas y el desarrollo final muestran sus propias cuadrículas. La reserva permanece en su informe.

Pasaron 91 pruebas en 13 módulos y 5 recorridos Chromium. Se comprobó la igualdad del valor exacto con los originales, la escala simétrica, la reapertura, el uso en móvil y la continuidad hacia PAPER. [Uso y escala](PERP_EXPERIMENTS_PLAN.md#mapa-de-calor).

## v218 cargado · Siguiente paso del recorrido MEXC

**Ver recorrido** e **Ingresos directos MEXC** enlazan con la operativa Spot o la retirada del libro seleccionado, según los movimientos comprobados del tramo vigente. Las acciones abren el panel correspondiente. Los tramos desplazados por otro ingreso se conservan en el historial.

Pasaron 205 pruebas en 15 módulos y 4 recorridos Chromium. Se comprobaron compra y venta, retirada, cierre de un tramo anterior y navegación sin órdenes, firmas ni retiradas adicionales. El piloto con fondos reales sigue pendiente. [Uso y alcance](WALLET_FUNDING_BNB.md#continuar-el-recorrido).

## v217 cargado · Revisar saldo antes de activar un bot Uniswap

**Bot · Activar → Revisar saldo** muestra gas disponible, coste de una orden, reserva STOP y unidades utilizables según los saldos y permisos del ejecutor. Activar o rearmar exige que la operadora cubra una orden y la reserva, con el precio de gas dentro de la política.

Pasaron 90 pruebas en 11 módulos y 4 recorridos Chromium. Se comprobó el rechazo con saldo justo para STOP, la activación al completar el gas y los recorridos LONG y DCA con parada. El piloto con fondos reales sigue pendiente. [Uso y alcance](USO_BOT_DEX.md#revisar-saldo-y-permisos).

## v216 cargado · Pool y recibo en el intercambio Uniswap

**Intercambiar · BNB** consulta la fábrica de Uniswap y comprueba el código del pool en el bloque de la cotización. Antes de solicitar la firma comprueba de nuevo la ruta. El plan muestra la salida simulada y el historial separa el mínimo de la cantidad confirmada por el recibo.

Pasaron 144 pruebas en 14 módulos y 4 recorridos Chromium. Se comprobó el bloqueo ante un pool distinto, la reapertura del historial sin wallet, la privacidad y el registro de compra, venta y gas. Una consulta pública verificó los cuatro pools en BNB Chain. El piloto con fondos reales sigue pendiente. [Uso y alcance](WALLET_SWAP_BNB.md#revisar-pool-y-recibo).

## v215 cargado · Seguimiento de órdenes parciales MEXC Spot

**Orden Spot → Revisar orden pendiente** abre la orden del diario cuando existe un único pendiente. La vista muestra las unidades solicitadas, ejecutadas y aún abiertas o canceladas a partir del pedido y los fills archivados. Un cierre sin fills aparece como cancelación sin ejecución.

Pasaron 204 pruebas en 13 módulos y 4 recorridos Chromium. El caso parcial conservó el fill al cancelar el resto y completó la retirada BNB con proveedores controlados. Se comprobó además el cierre sin fills y el rechazo de cantidades alteradas. El piloto con fondos reales sigue pendiente. [Uso y alcance](MEXC_SPOT_MANUAL.md#ejecuciones-parciales).

## v214 cargado · Origen LAB y PAPER antes de autorizar un bot MEXC

**Bots · REAL → Verificar origen** reproduce la reserva y la prueba de origen y presenta su versión, periodo y última vela PAPER. El formulario incluye la huella en la confirmación de LONG y DCA. Si cambia la evidencia, la autorización exige otra revisión.

Pasaron 146 pruebas en 11 módulos y 4 recorridos Chromium. Se comprobó el rechazo de una reserva modificada, la persistencia de la misma huella, el ciclo LONG, el rearme y DCA con recuperación de respuesta. El piloto con fondos reales sigue pendiente. [Uso y alcance](BOT_MEXC_SPOT.md#verificar-origen).

## v213 cargado · Revisar saldo antes de habilitar MEXC Spot

**Orden Spot → Revisar saldo** muestra las cantidades del libro y de MEXC si difieren. Si sólo sobra USDT en MEXC, **Buscar ingreso MEXC** lleva a la consulta de transferencias. Una revisión vigente sin diferencias permite confirmar la apertura o reapertura por separado.

Pasaron 107 pruebas en 12 módulos y 5 recorridos Chromium. Se comprobó la revisión antes y después de registrar el ingreso, la invalidación al cambiar límites, la orden manual y la reapertura. El piloto con fondos reales sigue pendiente. [Uso y límites](MEXC_SPOT_MANUAL.md#revisar-saldo-antes-de-habilitar).

## v212 cargado · Recorrido de ingresos directos MEXC

**REAL → Wallet ↔ MEXC → Ingresos directos** consulta los abonos USDT registrados con MEXC View. Agrupa los abonos contabilizados juntos y presenta la primera secuencia verificada de compra y venta Spot del mismo par, así como la retirada USDT por BNB registrada desde ORBIS. El origen de la wallet figura como desconocido.

Pasaron 220 pruebas en 15 módulos y 4 recorridos Chromium. Se comprobaron dos abonos agrupados, la compra y venta, la recepción y el asiento, un nuevo ingreso, la reapertura sin View y la copia cifrada. El piloto con fondos reales sigue pendiente. [Uso y límites](WALLET_FUNDING_BNB.md#ingresos-directos-mexc).

## v211 cargado · Recorrido MEXC desde el archivo local

**Enviar a MEXC · BNB → Ver recorrido** abre la comprobación del ingreso, las órdenes y la retirada desde la bóveda REAL desbloqueada, con wallet y View desconectadas. La nueva retirada vincula su destino mediante HMAC. Una retirada antigua sin huella permanece como destino por verificar hasta comprobar la wallet de origen.

Pasaron 213 pruebas en 14 módulos y 4 recorridos Chromium. Se comprobaron la reapertura, la ocultación de importes, otro destino, un plan antiguo y el rechazo de una huella copiada entre retiradas. [Uso y límites](WALLET_FUNDING_BNB.md#ver-recorrido).

## v210 cargado · Origen LAB y PAPER en la revisión GRVT

Las revisiones de los bots perpetuos MARKET y LIMIT muestran **Origen LAB y PAPER** antes de autorizar. Se identifican la versión, el tramo y las operaciones observadas, la validación histórica y, si procede, la optimización original. La procedencia pertenece a la misma revisión sellada que los límites REAL.

Pasaron 234 pruebas en 21 módulos y 4 recorridos Chromium. Se verificaron el rechazo de una revisión alterada y el flujo de activación de MARKET y LIMIT con proveedor controlado. La revisión de la variante optimizada quedó a la espera de autorización. [Diseño y uso](PERP_EXPERIMENTS_PLAN.md#revisar-el-origen-antes-de-autorizar-en-grvt).

## v209 cargado · Preparar PAPER desde una variante

**Laboratorio → Optimizar derivados → Preparar PAPER** abre una revisión de la misma versión, el mismo perfil y la misma reserva de la variante. La preparación muestra el tramo, el capital ficticio y los parámetros de salida. **Activar ficticio** crea la sesión mediante confirmación separada. Una reserva liquidada, interrumpida o con exposición queda excluida.

Pasaron 578 pruebas en 40 módulos y 4 recorridos Chromium en dos procesos por recorrido. Se verificó el vínculo con los originales, la operación ficticia con datos controlados, la comparación con la reserva y la reapertura sin arrancar otro observador. [Diseño y límites](PERP_EXPERIMENTS_PLAN.md#preparar-paper-desde-la-reserva-de-una-variante).

## v208 cargado · Guardar variante de derivados

**Laboratorio → Optimizar derivados → Guardar variante** archiva la combinación elegida como una versión, con su perfil y las pruebas exactas de desarrollo y reserva. **Abrir versión** recupera la versión aunque haya una posterior. Cada prueba abre su diario y las marcas del gráfico. Los reintentos recuperan los mismos registros.

Pasaron 647 pruebas en 37 módulos y 4 recorridos Chromium en dos procesos por recorrido. Se comprobó la migración LAB 14 → 15 en un archivo de prueba, el rollback ante un fallo parcial, la reapertura, los marcadores y la vinculación con las huellas de los resultados originales. [Diseño y límites](PERP_EXPERIMENTS_PLAN.md#guardar-y-abrir-la-variante-elegida).

## v207 cargado · Optimizar salidas de derivados

**Laboratorio → Optimizar derivados** compara stops y objetivos con el perfil fijo. Elige por capital neto entre combinaciones que completan el desarrollo sin liquidación y con al menos diez operaciones cerradas. Sólo evalúa la elegida en la reserva posterior. LAB 14 conserva el informe cifrado y sus originales. La reapertura y la copia reproducen el cálculo. El informe es experimental y no concede autoridad de ejecución.

Pasaron 643 pruebas en 36 módulos y 4 recorridos Chromium en dos procesos por recorrido. Se verificó la migración LAB 13 → 14 con datos de prueba, la reapertura, la exportación y la separación del tramo reservado. [Diseño y límites](PERP_EXPERIMENTS_PLAN.md#optimización-cronológica-de-salidas).

## v206 cargado · Comparar dos backtests de derivados

En **Pruebas de derivados → Comparar pruebas guardadas**, ORBIS reproduce dos ejecuciones y muestra sus métricas juntas. Cuando coinciden histórico, periodo, motor, capital, costes y supuestos de ejecución, calcula la diferencia y superpone sus curvas de capital. Los cambios de estrategia, entrada LIMIT y parciales se pueden comparar como variantes. El informe local conserva la huella de ambos resultados y sólo se descarga tras confirmación.

Pasaron 196 pruebas en 11 módulos y 4 recorridos Chromium en dos procesos por recorrido. Se comprobó la comparación tras reabrir, las curvas alineadas, las diferencias omitidas entre supuestos distintos y el rechazo de una ejecución alterada.

## v205 cargado · Archivo de muestras GRVT TESTNET

**Muestra de parciales → Capturar** guarda las observaciones en la bóveda cifrada y las vincula al grupo enviado. Tras reabrir, se pueden consultar, descargar y comparar dos o tres muestras del mismo grupo con View desconectada. El archivo conserva las cantidades y tiempos exactos, omite credenciales y recupera una captura guardada aunque se pierda la respuesta.

Pasaron 240 pruebas en 12 módulos y 4 recorridos Chromium. Se verificó la migración de 81 a 82 en una bóveda de ensayo, la copia cifrada y el rechazo de un archivo alterado. Falta verificar la secuencia IOC y stop con una subcuenta TESTNET autenticada del proveedor para habilitar parciales MARKET en REAL.

## v204 cargado · Máximo vendible en MEXC Spot

En **Orden Spot → Vender → Máximo vendible**, ORBIS propone la cantidad máxima de una orden LIMIT al precio de compra observado en MEXC, ajustado a la precisión del par. El cálculo usa el saldo conciliado, reserva margen para una posible comisión en el activo vendido y aplica los límites del mercado y de la sesión. La vista muestra las unidades fuera de la orden. Enviar requiere confirmación y una comprobación nueva.

Pasaron 162 pruebas en 12 módulos y 5 recorridos Chromium. Se comprobaron el redondeo de lotes, el mínimo del mercado, el límite por orden, políticas anteriores, la ausencia de reservas al calcular y la venta controlada con los términos propuestos. El piloto con fondos reales sigue pendiente.

## v203 cargado · Estimación de gas para ingresar a MEXC

En **Enviar a MEXC · BNB**, **Estimar gas** consulta el destino de la cuenta vinculada y simula la transferencia exacta de USDT. Propone el límite editable de BNB usando el precio observado y un margen del 20 % en unidades de gas, redondeado al entero superior. Al preparar el ingreso, ORBIS recalcula y comprueba que el límite sigue cubriendo el plan. El mínimo de ingreso procede de la pantalla de MEXC.

Pasaron 103 pruebas en 9 módulos y 5 recorridos Chromium. Se verificaron el redondeo exacto, un aumento del precio del gas, el cambio de importe y la separación entre estimar, preparar y firmar. Los recorridos controlados completaron ingreso, compra, venta y retirada. El piloto con fondos reales sigue pendiente.

## v202 cargado · Exportación de swaps Uniswap REAL

En **Gráfico → Exportar JSONL**, los bots LONG y DCA descargan sus swaps confirmados. Cada fila vincula la decisión de MEXC y los datos LAB/PAPER al permiso que produjo el swap. Conserva el gas exacto en BNB y distingue los swaps que ya se contabilizaron. El archivo local en claro omite direcciones, hashes de transacción, claves e identificadores operativos.

Pasaron 99 pruebas en 13 módulos y 5 recorridos Chromium. Se verificaron compra y venta, contabilidad, DCA BASE/DIP tras un rearme con otra reserva, rechazo de archivos alterados, ocultación de importes y continuidad de la descarga MEXC. El piloto con fondos reales sigue pendiente.

## v201 cargado · Exportación de fills GRVT MAINNET

Exportar JSONL descarga los fills de una operación conciliada en la revisión verificada del gráfico. Funciona para grupos protegidos y para LIMIT con o sin parciales. Conserva cantidades exactas, la función de cada fill, los parciales y el origen LAB/PAPER cuando existe. El archivo distingue MAINNET de TESTNET y omite subcuentas, órdenes y credenciales.

Pasaron 132 pruebas en 13 módulos y 5 recorridos Chromium. Se comprobó la exportación de un bot perpetuo protegido y de los perfiles LIMIT con y sin parciales, la protección de subcuenta y la continuidad de la exportación MEXC. El piloto con fondos reales sigue pendiente.

## v200 cargado · Exportación de fills MEXC REAL

Gráfico → Exportar JSONL guarda los fills finales, conciliados y asentados de un bot LONG o DCA. Cada fila conserva la autorización correspondiente, su origen PAPER y su reserva LAB cuando existe. Los importes exactos quedan en el archivo local en claro después de confirmar. La exportación omite claves, direcciones, identificadores de cuenta y números de orden.

Pasaron 133 pruebas en 13 módulos y 4 recorridos Chromium. Se comprobó la exportación antes y después de reabrir, el rearme, DCA, las ventas manuales atribuidas, la huella incorrecta y la revisión de gráfico desactualizada. El piloto con fondos reales sigue pendiente.

## v199 cargado · Saldo máximo para intercambiar en Uniswap

Máximo carga la cantidad exacta de USDT o WBNB observada en la wallet y muestra el BNB para gas por separado. La lectura y la cotización comprueban el bloque antes de mostrar el resultado. El saldo se descarta al cambiar de wallet o de cuenta y el importe precargado se retira al ocultar valores.

Pasaron 152 pruebas en 15 módulos y 4 recorridos Chromium. Se comprobaron un saldo de 18 decimales, saldo cero, ausencia de gas, privacidad, cambio de bloque, compra, venta por el saldo completo, comprobación de recibos y registro. El piloto con fondos reales sigue pendiente.

## v198 cargado · Recuperación de activaciones LIMIT en GRVT

Las revisiones LIMIT muestran la subcuenta. Un rechazo confirmado permite revisar de nuevo. Si se pierde la respuesta, ORBIS comprueba el identificador de la revisión sellada y el del intento. El intento anterior de una reanudación comparte ID de orden, pero no confirma la autorización nueva.

Pasaron 130 pruebas en 14 módulos y 4 recorridos Chromium. Los escenarios LIMIT con y sin parciales comprobaron rechazo, pérdida de respuesta en la activación y en la reanudación, historial atrasado, un único envío de entrada y conciliación. El piloto con fondos reales sigue pendiente.

## v197 cargado · Cuenta MEXC al pasar de PAPER a REAL

Bots REAL verifica el libro seleccionado y muestra cómo continuar cuando falta View, vínculo o Trade. Si hay STOP, otro bot activo, órdenes pendientes o una retirada en curso, el formulario muestra el bloqueo y los controles de revisión disponibles. El cambio de libro conserva la sesión PAPER elegida y vuelve a leer la cuenta y sus límites. Si se pierde la respuesta a una activación nueva, la lista de bots recupera el resultado por el identificador original.

Pasaron 137 pruebas en 15 módulos, 5 recorridos Chromium y las pruebas de estados del formulario. El recorrido completo cambió a un libro ajeno con Trade verificada, volvió al libro correcto y completó el ciclo con wallet y exchange de ensayo. El piloto con fondos reales sigue pendiente.

## v196 cargado · Historial de margen en GRVT PAPER

Margen GRVT muestra las reglas iniciales y los 20 cambios de valores más recientes de una sesión. Separa la hora de captura de la hora en que PAPER aplicó las reglas. Una nueva consulta con los mismos valores sólo actualiza la última captura. La vista usa el prefijo verificado de la sesión seleccionada.

Pasaron 223 pruebas en 15 módulos y 3 recorridos Chromium, cada uno con reapertura. Se comprobó la distinción entre captura y aplicación, la selección de un snapshot anterior, la reapertura y un cargo de financiación anterior a una liquidación causada por el endurecimiento de margen en la siguiente cotización de la prueba.

## v195 cargado · Capturas GRVT en el periodo de una prueba

Comparar reglas GRVT muestra la fecha de las reglas usadas, los instantes archivados dentro del periodo y las capturas más cercanas antes y después. Clasifica coincidencias, diferencias y valores contradictorios. La captura distinta más reciente del periodo aparece destacada para una comparación experimental. La vigencia entre capturas sigue sin acreditarse. [Consulta de margen por instrumento](https://api-docs.grvt.io/schemas/api_get_margin_rules_request/).

Pasaron 252 pruebas en 15 módulos y 4 recorridos Chromium, cada uno con reapertura. Se comprobó el periodo exacto, los límites de fecha, las coincidencias semánticas, los conflictos, la reproducción y el rechazo de capturas alteradas. Se consultaron reglas públicas de BTC, ETH y SOL para comprobar la captura actual.

## v194 cargado · Comparar PAPER de derivados con LAB

Comparar reserva LAB reproduce la instantánea de PAPER seleccionada y sus reservas históricas originales. Muestra por separado fechas, capital, resultado, rentabilidad, cierres, comisiones y financiación. Funciona con perfiles de mercado, LIMIT y parciales. Si LAB se bloquea, la comparación se retira y la inspección de PAPER conserva sus controles.

Pasaron 220 pruebas en 17 módulos y 4 recorridos Chromium, cada uno con reapertura. Las pruebas cubren varias reservas, una instantánea anterior, cambios de identidad en LAB y bloqueo. Los resultados de PAPER y LAB conservan sus propios periodos y supuestos.

## v193 cargado · Calcular el máximo de retirada MEXC

Calcular máximo propone un importe USDT para BNB Chain con el saldo conciliado, el filtro de comisión y el incremento y los límites publicados. Si el filtro está vacío se usa la comisión observada. La consulta exige View, destino verificado, STOP y que no haya operaciones pendientes. Preparar y confirmar son pasos separados que vuelven a comprobar la retirada. [Catálogo de monedas y redes de MEXC](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/query-the-currency-information).

Pasaron 222 pruebas en 17 módulos y 5 recorridos Chromium. Las pruebas cubren el redondeo exacto, saldo insuficiente, el filtro de comisión, la ausencia de planes al calcular y una retirada confirmada con el importe calculado en el proveedor de ensayo. El piloto con fondos reales sigue pendiente.

## v192 cargado · Importes del recorrido Wallet ↔ MEXC

Ver recorrido muestra USDT acreditado y gas, cambios netos de cada orden Spot y sus comisiones, y el débito, la comisión y el neto confirmado de la retirada. Los importes proceden del archivo local verificado. El informe funciona sin View y respeta Ocultar importes. El siguiente ingreso USDT contabilizado desde MEXC View también corta el tramo.

Pasaron 236 pruebas en 18 módulos y 6 recorridos Chromium. Se comprobaron los importes exactos, el regreso a la wallet original, el enmascaramiento y la reapertura con View desconectada. El piloto con fondos reales sigue pendiente.

## v191 cargado · Comprobar recibos Uniswap en BNB Chain

Comprobar recibo consulta el RPC de ORBIS con el hash comunicado por la wallet. Se verifica la firma, la inclusión en un bloque finalizado, la llamada original, el gas y los efectos esperados. Si la tarifa o la llamada difieren, se mantiene la revisión explícita. El historial prioriza acciones aún pendientes y permite registrar la operación y el gas por separado. [Finalización en BNB Chain](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/bsc-api-list/).

Pasaron 261 pruebas en 20 módulos y 11 recorridos Chromium. Se verificaron una compra, una venta, permisos, la recuperación sin reenvío, el registro contable y el reintento idempotente. Se cotejó una transacción pública legacy y su bloque en BNB Chain. El piloto con fondos del usuario sigue pendiente.

## v190 cargado · Calcular órdenes MEXC por importe

La operativa manual puede calcular cantidad y precio límite a partir del importe en la divisa del par. La compra incluye la comisión máxima estimada y conserva el presupuesto en el pedido. La venta parte del importe bruto al precio límite y muestra un neto estimado. El cálculo usa la cotización disponible del lado correspondiente y baja la cantidad al incremento admitido. [Reglas de instrumentos MEXC](https://www.mexc.com/api-docs/spot-v3/market-data-endpoints/exchange-information).

Se contrastó el cálculo con precios públicos de BTC, ETH, BNB y SOL y saldos sintéticos. Una comprobación racional independiente cubrió 10.000 límites de redondeo. El navegador calculó y envió una compra y una venta con un proveedor controlado. Se comprobó que un aumento de comisión bloquee la compra al superar el presupuesto y que el reintento recupere la misma orden.

Pasaron 363 pruebas en 22 módulos y 6 recorridos Chromium. El cálculo carece de autoridad de envío. El piloto con fondos reales sigue pendiente.

## v189 cargado · Registrar ingresos externos de MEXC

MEXC View permite elegir ingresos USDT acreditados que aún faltan en la cartera. ORBIS vuelve a consultarlos al confirmar y exige que su suma cubra exactamente la diferencia con el saldo Spot. Guarda el aporte y su vínculo con la captura cifrada en una sola transacción. Los fondos ya registrados en otra cuenta usan el recorrido de traslado. [Estados de ingreso de MEXC](https://www.mexc.com/api-docs/spot-v3/public-api-definitions#deposit-status).

Una prueba controlada recuperó dos ingresos, reabrió sus vínculos desde una copia cifrada y compró BTC con un importe superior al saldo anterior. Se comprobó el rechazo de saldos cambiados, estados regresivos y posibles duplicados, y la reversión de ambas entradas si falla la segunda. El navegador recuperó una respuesta perdida y conservó el mismo asiento al reabrir.

Pasaron 316 pruebas en 20 módulos y 6 recorridos Chromium con proveedores controlados. La aportación requiere una confirmación propia y las órdenes requieren otra apertura y sus permisos de siempre. Sigue pendiente el piloto con fondos reales.

## v188 cargado · Operar un par con varios activos en MEXC

Las órdenes manuales y los bots MEXC Spot admiten otros activos ya registrados. ORBIS compara el saldo completo con el libro y usa el efectivo y la posición del par para calcular fondos y exposición. Las diferencias, los fondos bloqueados, las órdenes abiertas y los movimientos sin conciliar impiden enviar. Si hay MX fuera del par, la lectura View debe confirmar que el pago de comisiones con MX está desactivado. [Consulta de MEXC](https://www.mexc.com/api-docs/spot-v3/spot-account-trade/query-mx-deduct-status).

El recorrido de API compró y vendió BTC y retiró USDT a la wallet registrada. ETH y BNB conservaron cantidades y coste. El navegador comprobó la importación de ETH, la compra y venta de BTC y la reapertura. El recorrido del bot comprobó que su asignación no usó el saldo ETH. El cambio de comisiones durante la orden de prueba impidió el POST negociable.

Pasaron 382 pruebas en 26 módulos y 6 recorridos Chromium con proveedores controlados. Sigue pendiente el piloto con fondos reales.

## v187 cargado · Velas cerradas confirmadas por GRVT

Los lectores históricos TRADE y MARK verifican `close_time` en nanosegundos antes de entregar una vela. El histórico incompleto se rechaza. Cuando sólo falta confirmar el último intervalo, PAPER observado y el lector de señales REAL esperan una nueva respuesta mientras siguen vigentes los límites de señal y cobertura. La espera no registra una observación ni publica una señal de entrada. [Campos de la API de GRVT](https://api-docs.grvt.io/schemas/candlestick/).

Se comprobaron 46 consultas públicas de intervalos cerrados en mainnet y testnet y se repitieron 40 con el adaptador de esta versión. Las pruebas controladas incluyen una vela parcial un nanosegundo antes del cierre, el rechazo del histórico en LAB, un hueco anterior y el vencimiento de la espera. El observador automático también completó y reprodujo las aperturas y cierres ficticios LONG y SHORT con datos públicos de BTC.

Pasaron 422 pruebas en 25 módulos y 7 recorridos Chromium. Sigue pendiente el piloto con fondos reales.

## v186 cargado · Cierre causal en GRVT PAPER

El cierre de una estrategia por vela sólo admite una cotización fechada entre el cierre de la vela de señal y el cierre de la siguiente. El libro comprueba la vigencia dentro de la transacción. Una cotización REST superada por el WebSocket deja la intención pendiente para el siguiente intento. Si vence la ventana, la sesión pasa a GAP y deja la posición abierta para revisión.

La prueba con GRVT público partió de 200 velas BTC de un minuto. Los observadores habituales registraron una apertura y un cierre ficticios de cada dirección. Tras reabrir, se reprodujeron ambas señales y se comprobó que cada cierre usó la cotización del lado correspondiente después de la señal. Las pruebas controladas forzaron un tick un nanosegundo anterior al cierre, una cotización REST superada y una ventana que vence durante la ejecución por ambas vías.

Pasaron 533 pruebas en 31 módulos y 6 recorridos Chromium. El piloto con fondos reales sigue pendiente.

## v185 cargado · Ventanas de velas cerradas en MEXC

Spot PAPER, DCA y los lectores de los bots MEXC y Uniswap fijan el inicio y el fin de su consulta. La comparación con BTC/USDT y BNB/USDT mostró que al enviar sólo el inicio, MEXC devolvía la vela abierta y omitía una cerrada tanto en 1 minuto como en 1 día. Con ambos límites devolvió las tres velas solicitadas. La preparación del DEX pagina hasta 500 velas por consulta y coteja dos cierres en cada solapamiento. [API de velas de MEXC](https://mexcdevelop.github.io/apidocs/spot_v3_en/#kline-candlestick-data).

Dos pruebas con mercado público y bóvedas propias cubrieron el tramo desde 620 velas de LAB hasta tres cierres consecutivos de Spot PAPER por par. Una usó la configuración habitual del observador. Cada sesión registró compra, venta y compra ficticias. El observador volvió a consultar cuando el ticker era anterior al cierre. Se verificaron al reabrir la versión, el backtest y las reglas observadas. La preparación de Uniswap completó el mismo histórico BNB desde 12 velas.

Pasaron 510 pruebas en 27 módulos y 7 recorridos Chromium. La verificación de navegador reúne dos tandas sobre las mismas fuentes. Se repitió el escenario que cruzó el cambio de hora real antes del avance previsto por el reloj de la prueba. La ejecución de bots y el horario diario de DCA se probaron con proveedores controlados. Sigue pendiente el piloto con fondos reales.

## v184 cargado · Estados de ingreso de MEXC

El historial de un ingreso BNB muestra la última consulta View identificada con el mismo recibo, cuenta, contrato, red, destino e importe. Muestra preabono, importe insuficiente, restricción, revisión, rechazo o devolución indicada según el código de MEXC. Si la consulta carece de datos para identificar el depósito, el resultado sigue pendiente de acreditación. [Códigos de la API de MEXC](https://www.mexc.io/api-docs/spot-v3/public-api-definitions#deposit-status).

El estado observado se guarda con la huella de su captura en la bóveda. Se recupera al desbloquear y desde una copia cifrada. La contabilidad se habilita al verificar el abono de estado 5; el asiento se confirma aparte. Un resultado contradictorio de cadena requiere revisar el envío.

Pasaron 346 pruebas en 25 módulos y 6 recorridos Chromium. Se comprobaron el historial tras reabrir, el rechazo de un estado alterado y el circuito controlado hasta compra, venta y retirada a la misma wallet. El piloto con fondos reales sigue pendiente.

## v183 cargado · Mínimo de ingreso USDT por BNB Chain

El plan de ingreso usa el mínimo indicado en la pantalla de depósito de MEXC para USDT en BNB Chain. La API documentada de MEXC expone el mínimo de retirada y las confirmaciones de depósito. El campo de mínimo de depósito se completa con el dato que ve el usuario en MEXC. La app abre esa página sólo al pulsar el enlace. [API de MEXC](https://www.mexc.io/api-docs/spot-v3/wallet-endpoints/query-the-currency-information).

La interfaz y la API comparan unidades enteras exactas antes de crear el plan. Un importe inferior por una unidad del token queda rechazado y un importe igual se admite. El mínimo declarado queda ligado a la huella del plan cifrado. Los planes antiguos sin permiso requieren una preparación nueva. Las solicitudes ya iniciadas conservan su recuperación y su contabilidad.

Pasaron 352 pruebas en 27 módulos y 6 recorridos Chromium. El circuito controlado incluyó ingreso, compra, venta y retirada a la wallet original. El piloto con fondos reales sigue pendiente. [Guía de ingreso](WALLET_FUNDING_BNB.md#recorrido).

## v182 cargado · Señales y ejecuciones de Spot PAPER

La señal permanece en la vela cerrada que confirmó la regla. Las flechas de compra y venta se sitúan en el segundo de ejecución ficticia y en su precio simulado. El detalle conserva los nanosegundos archivados, el bid y el ask. Si la hora del ticker difiere de la hora de ejecución, muestra las dos.

El diario abre el detalle de la operación. La ejecución y su vela de origen se enlazan cuando existe una decisión sobre vela cerrada. Las salidas manuales e intravela no reciben una señal de vela. Un precio simulado sin vela cerrada se muestra como ejecución sin OHLC. El campo de intervalo de la API se conserva para los consumidores existentes.

Pasaron 362 pruebas en 25 módulos y 5 recorridos Chromium. En escritorio se verificó el punto real del gráfico y en móvil la reapertura del detalle y el enlace a sus reglas. Las operaciones del recorrido usaron fondos ficticios y datos controlados.

## v181 cargado · Rango completo de backtests en LAB

Los gráficos de precio, capital y drawdown abarcan el histórico Spot completo hasta 20.000 velas. «Todo disponible» conserva los extremos del rango al pasar de escritorio a móvil. Cada gráfico pide un tramo en una sola consulta visual. Las marcas se consultan después de cargar sus velas. El cliente verifica que todos los puntos coincidan con las fechas del histórico y ofrece reintentar ante errores.

La consulta visual usa números de doble precisión para el gráfico. Los puntos exactos siguen disponibles por páginas, y el backtest y la exportación conservan sus decimales originales. La respuesta se descarta si LAB se bloquea durante su preparación.

La comparación de 10.000 velas de prueba pasó de cinco solicitudes a una por gráfico. El cuerpo de velas pasó de 771,321 a 560,421 bytes y el de capital y drawdown de 1,954,322 a 895,349 bytes. Pasaron 421 pruebas en 43 módulos y 9 recorridos Chromium. Las medidas corresponden a históricos de prueba locales.

## v180 cargado · Conciliación de ejecuciones antiguas en MEXC

Desde el diario, una orden final sin reserva local puede conciliarse si View devuelve la orden y todos sus fills, y ORBIS recupera la solicitud exacta con la huella archivada. La confirmación vuelve a comprobar los términos, los fills, la versión de cartera y el estado pausado. Se aplican los importes y comisiones reales de cada ejecución.

La observación, el asiento y el cierre se guardan juntos si el saldo resultante cuadra con MEXC. Un posible asiento manual duplicado, un cambio de cartera o una diferencia de saldo dejan el caso pendiente. El rearme sigue siendo explícito. La orden no se reenvía. Pasaron 848 pruebas en 63 módulos y 7 recorridos Chromium con servicios controlados. Un segundo proceso verificó la compra recuperada, la venta posterior y sus dos asientos cifrados. Sigue pendiente el piloto con la cuenta del propietario. [Uso](BOT_MEXC_SPOT.md#preparación-atómica-y-recuperación).

## v179 cargado · Cancelación antigua sin ejecuciones en MEXC

El diario permite archivar una decisión antigua sin reserva cuando MEXC devuelve una orden LIMIT cancelada, con cantidad ejecutada y contravalor a cero y una página de fills vacía. La revisión exige la misma cuenta, orden, decisión y estado pausado. Al confirmar, ORBIS repite las lecturas y rechaza una revisión desactualizada.

El archivo cifrado conserva la decisión original, la hora, los términos y la prueba de cancelación. El registro es idempotente y no crea reserva, asiento ni permiso. Reanudar requiere el flujo habitual con una apertura y una autorización nuevas. Las órdenes con fills o sin evidencia suficiente siguen pendientes. Pasaron 843 pruebas en 63 módulos y 6 recorridos Chromium con servicios controlados. También se verificaron la copia cifrada, la lectura en otro proceso y el rechazo de evidencia alterada. [Uso](BOT_MEXC_SPOT.md#preparación-atómica-y-recuperación).

## v178 cargado · Consulta de decisiones antiguas en MEXC

El diario de un bot Spot con una decisión antigua sin reserva local permite consultar la orden con View. Se valida la identidad de la cuenta antes y después de la consulta, y se cotejan el identificador, el par, el lado, el límite, la cantidad y los fills. La lectura exige la sesión local, no la prolonga y devuelve el resultado sólo si la conexión y la decisión permanecen vigentes.

Una respuesta incompleta o ausente conserva el estado sin verificar. Para decisiones fuera de los siete días que cubre la API, el diario indica el identificador para consultar el historial de MEXC. Una coincidencia muestra el estado y el número de fills. La conciliación contable y la reanudación de estos casos siguen pendientes. La consulta no reconstruye reservas ni envía órdenes. Pasaron 842 pruebas en 63 módulos y 5 recorridos Chromium con servicios controlados. [Uso](BOT_MEXC_SPOT.md#preparación-atómica-y-recuperación). [Consulta de órdenes de MEXC](https://www.mexc.com/api-docs/spot-v3/spot-account-trade/query-order).

## v177 cargado · Venta manual atribuida al bot MEXC

Tras una venta Spot manual conciliada, ORBIS reconstruye el inventario ajeno al bot en orden temporal. Cuando ese inventario no cubre la venta y hay un solo bot con unidades del activo, el usuario puede revisar y confirmar el débito exacto, el coste restante y los ingresos netos que corresponden al bot. El rearme vuelve a verificar las pruebas y archiva el ajuste en la misma transacción que la nueva autorización.

Las ventas ambiguas o sin originales comprobables conservan el bloqueo. El libro patrimonial y los marcadores de órdenes del bot conservan su origen. En Chromium se comprobó el rechazo inicial de la revisión, la confirmación sin emitir otra orden, el cierre del saldo restante y la reapertura de la bóveda con el ajuste. Pasaron 841 pruebas en 63 módulos y 6 recorridos Chromium con servicios controlados. El piloto con la cuenta del propietario sigue pendiente. [Uso](BOT_MEXC_SPOT.md#reanudar-tras-una-venta-manual).

## v176 cargado · Comparación LAB con IA

El comparador de backtests ofrece «Analizar comparación con IA» tras verificar ambas pruebas. La vista previa muestra definiciones, métricas, procedencia y supuestos exactos. El paquete excluye nombres libres, velas, operaciones individuales y patrimonio. Sólo las pruebas del mismo histórico y motor incluyen diferencias calculadas. El consentimiento se vincula a la huella del par ordenado y se vuelve a verificar al enviar.

Guardar en LAB cifrado es optativo. El registro se ancla en la prueba Base y conserva las dos ejecuciones con sus huellas. El archivo distingue «Comparación» y permite recuperar la evidencia en JSON o JSONL. Pasaron 165 pruebas en 18 módulos y 5 recorridos Chromium. La reapertura del archivo cifrado en otro proceso produjo el mismo contenido. Se usó un proveedor controlado para comprobar el flujo. [Uso y límites](IA_Y_ALMACENAMIENTO.md#comparar-dos-backtests-con-ia).

## v175 cargado · Divergencias RSI confirmadas

El editor LAB admite divergencias alcistas y bajistas como condiciones de entrada o salida. El periodo RSI es el de ORBIS, con ganancias y pérdidas de los últimos cierres. Se comparan dos mínimos o dos máximos estrictos consecutivos, con un giro opuesto entre ellos. La separación y el número de velas de confirmación a cada lado son configurables. La señal sale una vez, al cierre que confirma el segundo giro, y exige continuidad desde la primera ventana RSI. Un marco superior espera a su último cierre de la temporalidad base. [Concepto de divergencia RSI](https://www.tradingview.com/support/solutions/43000589127-rsi-divergence-indicator/).

En Estructuras, al elegir un caso aparecen G1 y G2 junto al marcador de confirmación. El registro conserva los precios y valores RSI exactos de ambos giros, el giro opuesto y la hora de cierre que confirmó el caso. El backtest distingue señal al cierre de entrada en la siguiente apertura. La regla se añadió al motor actual sin cambiar las anteriores. El replay completo de 54 casos legados de v174 conservó las mismas huellas. Pasaron 391 pruebas en 27 módulos y 6 recorridos Chromium. Spot PAPER reprodujo la evidencia del backtest con una vela entrante controlada. Los pilotos autenticados y la aceptación global siguen pendientes.

## v174 cargado · Recuperación GRVT con View

En «Operar GRVT» se puede reconectar una subcuenta que ya tiene un registro MAINNET con una clave View. ORBIS coteja la subcuenta autenticada y el titular con la huella cifrada del registro. ORBIS guarda la clave en memoria y descarta la conexión al bloquear la bóveda, desconectar o caducar. La conexión View respeta una conexión Trade activa.

La consulta recupera el estado de las órdenes, permite revisar fills observados y concilia localmente un cierre nativo si las dos auditorías cuadran. Recuperar una posición con IOC, renovar un cierre o enviar otra orden siguen exigiendo Trade. Pasaron 154 pruebas en 13 módulos y 5 recorridos Chromium con GRVT controlado. El navegador recuperó el grupo tras bloquear y reabrir, observó un parcial con View y sólo autorizó el cierre tras reconectar Trade. El piloto autenticado con una cuenta del propietario y la activación de parciales MARKET siguen pendientes.

## v173 cargado · Alertas de precio

Alertas permite vigilar varios mercados mientras ORBIS está abierto. Cada regla conserva par, fuente y umbral. Estudio comparte el mismo registro e Inicio muestra el recuento y el último aviso. El navegador guarda las reglas anteriores y su estado de disparo.

Una condición estricta puede generar un aviso al observar el primer precio vigente. Se rearma al volver al umbral o salir de la condición. La comparación usa decimales exactos. Se descartan cotizaciones atrasadas, futuras, de otro mercado o de otra fuente fijada. El historial conserva hasta 200 avisos con precio, fuente y hora de detección. Cuando la fuente no publica su hora, se conserva la hora de recepción. La vigilancia consulta precios públicos por intervalos y registra los que observa con la app abierta. Las reglas y avisos quedan en el navegador local.

Pasaron 99 pruebas en 10 módulos y 5 recorridos Chromium con datos de prueba. Se comprobaron dos mercados desde Inicio, precisión decimal, rearme, datos rechazados, dos pestañas, eliminación, reapertura y tamaños de escritorio y móvil. Quedan pendientes el piloto autenticado con cuentas reales y la aceptación global.

## v172 cargado · DCA por SMA o EMA en Uniswap v3

Una sesión PAPER de BNB/USDT con filtro SMA o importe variable SMA/EMA puede abrir «Preparar DEX». La revisión reproduce la regla y su reserva, coteja las velas de señal y exige un vínculo, límites y permiso de la wallet. El bot usa cierres públicos MEXC y compra WBNB con USDT en Uniswap v3 de BNB Chain.

El factor se aplica antes de los límites reales. El importe calculado se redondea hacia abajo a unidades del token. Un resultado cero queda como espera y no consume una activación. El diario conserva la señal y reproduce el cálculo con sus velas. El rearme conserva el prefijo EMA, los recibos, los importes consumidos y los límites restantes.

Pasaron 292 pruebas en 21 módulos y seis recorridos de navegador. El recorrido de EMA confirmó una compra periódica de 0,005 USDT y un extra de 0,0025 USDT en una cadena local de prueba, con renovación, rearme y reapertura. Los 40 resultados completos de los motores DEX previos coincidieron con v171. Queda pendiente el piloto con una wallet real y la aceptación global. [Uso y alcance](DEX_DCA_PLAN.md#medias-móviles).

## v171 cargado · compartir e importar planes DCA

Desde Simulación PAPER → DCA se puede descargar la versión guardada seleccionada o revisar un archivo JSON. Se admite el formato `orbis.automation.dca.v1` con huella y una plantilla declarativa con nombre y definición. La revisión muestra los parámetros. «Guardar copia» crea una versión 1 local independiente. Se puede cancelar antes de guardar. Reintentar la misma solicitud recupera una sola copia, incluso tras perder la respuesta.

El archivo exportado contiene nombre, versión, motor, definición y huella. Las cuentas, credenciales, permisos, saldos, sesiones y operaciones quedan en sus almacenes locales. Una copia requiere sus propias pruebas y autorización para llegar a MEXC REAL. Los formatos de otros bots quedan pendientes de una muestra concreta.

Pasaron 245 pruebas en 15 módulos y siete recorridos de navegador. Se comprobó la importación en almacenamiento cifrado, la repetición de una solicitud simultánea y la verificación del backtest desde la copia importada. [Formato y uso](BOT_MEXC_SPOT.md#compartir-un-plan-dca).

## v170 cargado · DCA por SMA o EMA en MEXC REAL

Un plan con filtro SMA o importe variable SMA/EMA puede preparar un bot MEXC Spot desde su sesión PAPER. La activación exige reproducir el backtest, comprobar una observación PAPER y una reserva de la misma versión, verificar la cuenta y Trade, fijar capital REAL separado y confirmar el permiso. MEXC observa sus propios cierres desde la activación. La media conserva el prefijo verificado y el rearme rechaza reemplazar los cierres ya archivados.

El multiplicador se aplica antes de los límites de orden, capital, presupuesto mensual, exposición, saldo y costes. Un factor cero no crea reserva ni pedido. El diario conserva el cierre, la media, el factor y el importe solicitado. El verificador vuelve a calcular la señal con el archivo de mercado y comprueba que la reserva cabe en el importe ajustado. La ruta manual de órdenes no puede activar esta política. El DEX sigue limitado a planes compatibles sin ajuste por media móvil.

Pasaron 423 pruebas en 37 módulos y siete recorridos Chromium con proveedores controlados. La prueba de STOP que agotó su espera en paralelo pasó al repetir el grupo MEXC sin cambiar fuentes. Los 45 resultados históricos y de reserva de v169 coincidieron. Otro proceso reprodujo el diario de dos decisiones desde una copia cifrada. Queda pendiente la prueba autenticada con una cuenta MEXC real y la aceptación global. [Uso](BOT_MEXC_SPOT.md#medias-móviles).

## v169 cargado · importes DCA por SMA o EMA

El plan condicionado puede multiplicar la aportación y los extras por caída según el cierre esté sobre o bajo la media. Cada rama admite un factor de 0 a 10. La igualdad usa el importe alternativo. El factor cero omite la compra y deja pendiente la aportación del mismo periodo. Presupuesto, exposición y costes se aplican después del ajuste. DCA fijo y compra inicial se conservan como referencias.

El motor `dca-1.8` comparte la decisión entre histórico, reserva, PAPER y el análisis teórico de huecos. La EMA parte de la media de los primeros N cierres y usa 2/(N+1) para ponderar cada cierre siguiente. El estado incremental se guarda en el registro local. PAPER coteja la semilla con el dataset fijado y registra los cierres, la media, el multiplicador y el importe antes de aplicar límites. La reserva usa los cierres conocidos antes del corte y un presupuesto propio. Los importes históricos usan la apertura siguiente y PAPER el ask observado.

Pasaron 254 pruebas en 22 módulos y seis recorridos Chromium con proveedores controlados. Los 41 resultados completos de motores anteriores, incluido el filtro SMA de v168, coincidieron con v168. Un proceso nuevo recuperó los eventos PAPER y el informe del hueco desde copias cifradas de prueba. El ajuste por media se limita a LAB y PAPER. Quedan pendientes los pilotos autenticados, más condiciones DCA y la aceptación global.

## v168 cargado · filtro SMA para DCA

La variante condicionada puede exigir que el cierre esté por encima o por debajo de una SMA de 2 a 120 velas. La media incluye el cierre de señal. La igualdad y el historial insuficiente impiden la compra; una condición fallida deja pendiente la aportación periódica del mismo periodo. El filtro se aplica a aportaciones periódicas y extras por caída. La comparación mantiene DCA fijo y compra inicial.

El motor `dca-1.7` queda fijado en histórico, reserva y PAPER. La reserva conserva los precios anteriores al corte y empieza con saldo propio. El histórico usa la apertura siguiente. PAPER usa bid/ask observado y archiva la ventana de velas y el valor SMA. El análisis de huecos usa un cierre proxy en una rama teórica que conserva el saldo observado. MEXC REAL y DEX rechazan los planes SMA. Los planes anteriores conservan su motor y formato de datos.

Pasaron 236 pruebas en 22 módulos y seis recorridos Chromium con proveedores controlados. Los 37 resultados completos de motores anteriores y reserva coincidieron con v167. Quedan pendientes las reglas EMA y los importes variables por tramos, la aceptación global y los pilotos autenticados.

## v167 cargado · exportar ejecuciones GRVT TESTNET para investigación

El gráfico de fills descarga un JSONL por episodio. Cada línea conserva la versión LAB, la huella de su dataset PAPER y los datos disponibles al seleccionar la señal. Los fills y su nivel de evidencia aparecen en un bloque de resultados posterior. Los ciclos seleccionados sin ejecución conciliada se conservan y se distinguen de los casos sin auditoría. Se omiten credenciales e identificadores de subcuenta y de orden. Pasaron 59 pruebas y tres recorridos de navegador con proveedores controlados. El archivo descargado volvió a coincidir después de bloquear y reabrir la bóveda. [Formato](IA_Y_ALMACENAMIENTO.md#ejecuciones-grvt-testnet-jsonl).

## v166 cargado · revisar una orden manual MEXC antes de enviar

La operativa manual revisa compra o venta con los datos actuales de la cuenta y del instrumento. Muestra importe, comisión máxima estimada y la regla que impide continuar cuando procede. La consulta carece de autoridad de envío y el flujo real repite sus comprobaciones. Pasaron 188 pruebas y cuatro recorridos de navegador con proveedores controlados, incluidas revisión de mínimos, compra, venta, retirada y la operativa del bot. Queda pendiente el piloto mainnet. [Guía de uso](MEXC_SPOT_MANUAL.md#revisar-orden).

## v165 cargado · ejecuciones de bots GRVT TESTNET en el gráfico

El historial de bots muestra entradas, stops, objetivos y reducciones IOC a partir de auditorías y recibos del ciclo guardados en la bóveda. El selector conserva importe, comisión, fecha y estado de cada fill. El gráfico no necesita credenciales ni consulta GRVT. Pasaron 257 pruebas y cuatro recorridos de navegador con proveedores controlados. Se comprobaron LONG, SHORT, ciclos sin ejecución, cierres nativos, parciales, IOC y reapertura. Queda pendiente el piloto con GRVT autenticado. [Uso y alcance](GRVT_BOT_TESTNET.md#gráfico-de-fills).

## v164 cargado · saldos e historial de Patrimonio en una lectura

Patrimonio reconstruye una vez el libro cifrado para mostrar saldos, posiciones y movimientos. El historial se muestra antes de la consulta de cotizaciones. El CSV sigue leyendo los movimientos originales al exportar. La lectura mantiene el control de sesión y evita el almacenamiento en caché. Pasaron 65 pruebas y cinco recorridos de navegador. La medición comprueba los mismos datos en dos cuentas con 24 recibos DEX firmados en una cadena local. Las consultas SQL bajaron de 12272 a 6136. [Método y alcance](ECONOMIC_READ_SNAPSHOT.md#lectura-de-patrimonio).

## v163 cargado · recuperar el vínculo View de MEXC al reconectar

La bóveda guarda una huella de la clave View renovada. Al introducirla de nuevo, ORBIS verifica el UID y recupera el mismo libro. La clave anterior queda fuera del vínculo operativo y Trade exige una verificación separada. Otra renovación aplica STOP y pausa el bot de esa cuenta. La migración de 80 a 81 conserva una preimagen cifrada y se comprobó con datos de prueba. Pasaron 460 pruebas y siete recorridos de navegador con proveedores controlados. Queda pendiente el piloto mainnet. [Guía de uso](MEXC_SPOT_MANUAL.md#renovar-vínculo-view).

## v162 cargado · renovar el vínculo operativo MEXC

La nueva clave View de la misma cuenta puede usar el libro MEXC original durante la sesión. La confirmación vuelve a comprobar el UID, pausa los bots de esa cuenta, deja STOP persistente y exige verificar Trade y revisar la operativa por separado. Las órdenes pendientes conservan sus identificadores y deben conciliarse para reabrir. Pasaron 431 pruebas y siete recorridos de navegador con proveedores controlados. Se verificaron el ciclo de ingreso, compra, venta y retirada, la invalidación de la apertura anterior y la reanudación explícita del mismo bot con una señal nueva. La clave y su vínculo renovado se mantienen en memoria. El piloto con fondos reales sigue pendiente. [Guía de uso](MEXC_SPOT_MANUAL.md#renovar-vínculo-view).

## v161 cargado · recuperar ingresos y retiradas con View renovada

Las comprobaciones de ingresos y retiradas aceptan una clave View nueva si su UID corresponde inequívocamente a la cuenta original. Las observaciones y los asientos conservan el mismo vínculo contable. La autorización de nuevos envíos, retiradas y órdenes sigue separada. Pasaron 419 pruebas y seis recorridos de navegador con proveedores controlados. Se verificó el ingreso tras reabrir por nonce, la retirada por RPC de wallet y por RPC de ORBIS, el asiento de un traslado y la resolución de un rechazo sin cargo. El navegador también rechazó una clave de otro UID antes de comprobar la retirada. El piloto con fondos reales sigue pendiente. [Guía de uso](MEXC_SPOT_MANUAL.md#view-renovada).

## v160 cargado · recuperar una orden Spot con View renovada

El diario de pendientes permite comprobar una orden original con la nueva clave View. ORBIS exige una coincidencia única con la huella del UID archivado y vuelve a verificarla antes de contabilizar o cerrar. El vínculo, las credenciales de Trade y las autorizaciones de ejecución se mantienen separados. Pasaron 393 pruebas y seis recorridos de navegador con proveedores controlados. Se rechazaron un UID distinto, un UID que cambió durante la consulta y vínculos ambiguos. La prueba de API contabilizó una ejecución parcial una sola vez. El navegador recuperó la orden original sin enviar ni cancelar otra con la clave renovada. El piloto con fondos reales sigue pendiente. [Guía de uso](MEXC_SPOT_MANUAL.md#view-renovada).

## v159 cargado · recuperar una orden MEXC pendiente

Orden Spot y Bot llevan al diario de pendientes de la cuenta seleccionada. La guía distingue una reserva anterior al envío de una orden que debe sincronizarse. El diario se puede leer con View desconectada. Sólo habilita la sincronización con la conexión View de la misma cuenta. El formulario manual bloquea nuevos envíos mientras el diario conserve una orden o reserva pendiente. Pasaron 231 pruebas y siete recorridos de navegador con proveedores controlados. Se comprobó una respuesta perdida después de que MEXC aceptara una orden abierta, la sincronización con View y la continuidad hasta la retirada sin repetir el envío. El piloto con fondos reales sigue pendiente. [Guía de uso](MEXC_SPOT_MANUAL.md#órdenes-pendientes).

## v158 cargado · comprobar el recorrido wallet a MEXC y regreso

El historial de cada ingreso ofrece **Ver recorrido**. La revisión local distingue el abono y el asiento, comprueba la compra y la venta Spot conciliadas y exige que la retirada contabilizada vuelva a la wallet de origen. El control de la wallet se vuelve a verificar al abrir la bóveda. La consulta funciona con MEXC View desconectada. Pasaron 207 pruebas y cinco recorridos de navegador con proveedores controlados. Se verificaron una retirada a otra wallet y dos ingresos contabilizados en un orden distinto de su fecha de abono. El piloto con fondos reales sigue pendiente. [Guía de uso](WALLET_FUNDING_BNB.md#ver-recorrido).

## v157 cargado · comprobar el ingreso MEXC desde el RPC de ORBIS

El ingreso se puede comprobar mediante el RPC de ORBIS aunque la wallet no devuelva el recibo. Se consulta el txId ya registrado o se recupera la transacción por nonce. ORBIS coteja el abono con MEXC View antes de habilitar la contabilización. El historial conserva la fuente del recibo. Pasaron 193 pruebas y cinco recorridos de navegador. El RPC predeterminado resolvió además una transacción pública por txId y nonce. [Guía de uso](WALLET_FUNDING_BNB.md#comprobar-el-ingreso-desde-orbis).

## v156 cargado · comprobar una retirada MEXC desde el RPC de ORBIS

La recepción se puede verificar aunque la wallet no devuelva su recibo. ORBIS consulta el hash confirmado por MEXC y comprueba el índice del bloque finalizado, la transferencia USDT y el destinatario. La fuente queda en el recibo local. La contabilización sigue siendo explícita. Pasaron 188 pruebas y tres recorridos de navegador. El RPC configurado por defecto también devolvió un recibo público finalizado. [Guía de uso](MEXC_WITHDRAWAL_BNB.md#consulta-desde-orbis).

## v155 cargado · reanudar un bot MEXC tras una venta manual

ORBIS permite revisar una venta Spot manual conciliada cuando cada fill puede cubrirse con el inventario ajeno a los bots disponible en su fecha. El bot conserva sus unidades y coste. Una compra posterior no cubre una venta anterior. Pasaron 99 pruebas y cuatro recorridos de navegador. [Guía de uso](BOT_MEXC_SPOT.md#reanudar-tras-una-venta-manual).

## v154 cargado · reanudar un bot MEXC tras una compra manual

Una compra Spot manual conciliada del mismo par admite reanudar el bot con confirmación de la asignación. El bot conserva sus fills, unidades y coste. El recorrido controlado rechazó primero la revisión, luego reanudó sin emitir una orden y vendió dejando intactas las unidades de la compra manual. Pasaron 95 pruebas y tres recorridos de navegador. Las ventas manuales requieren revisar la atribución. [Guía de uso](BOT_MEXC_SPOT.md#reanudar-tras-una-compra-manual).

## v153 cargado · secuencia IOC y stop de GRVT Testnet

La comparación de muestras acepta una tercera captura opcional del mismo grupo. Coteja los fills previos y concilia el saldo por tramo. Si los fills del IOC y del stop se intercalan o empatan en el tiempo, el informe conserva la ambigüedad. Pasaron 110 pruebas y tres recorridos de navegador. El ensayo autenticado de Testnet sigue pendiente. [Guía de uso](GRVT_PERP_PARTIAL_REAL.md#comparar-muestras).

## v152 cargado · seguimiento de ingresos y retiradas MEXC

Wallet ↔ MEXC muestra el siguiente paso pendiente del libro vinculado al desbloquear ORBIS. Distingue recuperación del envío, comprobación del abono o recepción y contabilización. La lectura es local y mantiene los permisos de cada acción. Pasaron 182 pruebas y tres recorridos de navegador. [Guía de uso](WALLET_FUNDING_BNB.md#guía-wallet--mexc).

## v151 cargado · cierre de ingresos MEXC cancelados o fallidos

Buscar en BNB cierra un ingreso con una cancelación firmada de la misma wallet o con un recibo revertido del envío original. Conserva el gas observado y el nonce consumido sin crear un abono. Un nonce posterior admite otro plan. El recorrido controlado cerró la cancelación, envió un nuevo ingreso, compró, vendió y registró la retirada. Pasaron 169 pruebas y tres recorridos de navegador. [Procedimiento](WALLET_FUNDING_BNB.md#cierre-en-bnb).

## v150 cargado · recuperación de ingresos MEXC por nonce

Buscar en BNB recupera la referencia finalizada de un ingreso pendiente o ya registrado si coincide con la wallet, el nonce, el contrato, el destino, el importe y las condiciones del plan. El abono View y el asiento siguen siendo pasos independientes. En Chromium se simuló una respuesta perdida después de emitir la transferencia. El recorrido reabrió la bóveda, recuperó el txId, comprobó el abono, compró, vendió y registró la retirada hacia la wallet. Pasaron 166 pruebas y tres recorridos con proveedores controlados. [Procedimiento](WALLET_FUNDING_BNB.md#recuperar-un-txid-en-bnb).

## v149 cargado · reanudación MEXC tras cambios de efectivo

La reanudación de un bot pausado admite confirmar cambios exclusivamente de efectivo de su par si reconstruyen el libro anterior. Conserva sus unidades y coste exactos, vuelve a comprobar el saldo y enlaza la revisión a la nueva autorización. Los movimientos de activos y las revisiones desactualizadas mantienen el bot pausado. En Chromium se rechazó y se aceptó la misma aportación sin enviar otra orden al reanudar. Pasaron 75 pruebas y tres recorridos de navegador con proveedor controlado. [Procedimiento](BOT_MEXC_SPOT.md#reanudar-tras-cambios-de-efectivo).

## v148 cargado · comparación local de muestras GRVT Testnet

Dos JSON del mismo grupo muestran los cambios de posición, stop, objetivo, IOC y saldo. La comparación verifica la continuidad de los fills e identifica la parte de la variación del saldo sin explicación en esas ejecuciones. Pasaron 98 pruebas y tres recorridos de navegador. El ensayo en Chromium recorrió la reducción IOC y el disparo posterior del stop con un proveedor controlado. Sigue pendiente el ensayo autenticado. [Procedimiento](GRVT_PERP_PARTIAL_REAL.md#comparar-muestras).

## v147 cargado · muestras de parciales GRVT Testnet

El registro de una entrada protegida TESTNET captura la posición, los fills, las salidas nativas y un IOC identificado. La acción lee con una clave View efímera y permite descargar un JSON local. En Chromium se verificaron las muestras antes y después de un IOC controlado, sin envíos adicionales. Pasaron 86 pruebas y tres recorridos de navegador. La comprobación con GRVT Testnet autenticado y la activación de parciales MARKET en REAL continúan pendientes. [Procedimiento](GRVT_PERP_PARTIAL_REAL.md#capturar-el-ensayo).

## v146 cargado · histórico de mercado en el intervalo elegido

Estudio conserva el intervalo al cambiar el periodo visible. La carga inicial solicita páginas anteriores con el mismo mercado, fuente e intervalo y deja seguir al desplazar el gráfico. Una regresión en Chromium usa la librería real con precios controlados para comprobar un año en 4h, marcas PAPER, fechas con velas semanales, el archivo diario de BTC/USD desde 2012 y la última página de Inicio. Pasaron 205 pruebas y cinco recorridos de navegador.

## v145 cargado · exportación JSONL del archivo de IA

LAB exporta una selección explícita de análisis guardados en un JSONL local. Cada fila conserva la pregunta, la evidencia agregada, la respuesta marcada como anotación del modelo y sus huellas. El lote se rechaza íntegro si una huella no coincide, falta un análisis o aparece un identificador sensible. Cerrar el panel cancela la descarga pendiente. Pasaron 93 pruebas y cuatro recorridos de navegador. [Formato y alcance](IA_Y_ALMACENAMIENTO.md#exportación-de-anotaciones-jsonl).

## v144 cargado · registro de saldo previo en MEXC

Desde Orden Spot se abre el inventario inicial de una cuenta REAL vacía. Cambiar de cuenta limpia la vista previa. Una respuesta perdida permite reintentar la misma solicitud y recuperar los asientos ya confirmados en esa sesión aunque el saldo del exchange haya cambiado. La confirmación de ese registro local queda separada de revisar el saldo y habilitar órdenes. Pasaron 106 pruebas y cinco recorridos de navegador. La prueba nueva empieza con BTC y USDT ya depositados, registra el coste declarado y completa compra, STOP y venta sin LAB. Una prueba de API completa además la retirada a una wallet vinculada y su asiento como traslado interno. Ambas usan proveedores controlados. El piloto con la cuenta real sigue pendiente. [Uso](MEXC_SPOT_MANUAL.md#saldo-que-ya-está-en-mexc).

## v143 cargado · selección de BNB Chain en wallet

El acceso Wallet enfoca el cambio de red en una wallet EVM conectada. El cambio y el alta opcional requieren dos acciones separadas. La guía revalida la red por RPC e invalida la prueba y las cotizaciones anteriores. El recorrido verifica rechazo, red desconocida, alta sin cambio, respuesta sin cambio efectivo, confirmación con y sin evento y respuesta tardía después de desconectar. Pasaron 142 pruebas y seis recorridos de navegador. Se usaron proveedores controlados. Queda pendiente el piloto con la wallet y la cuenta del propietario. [Uso](MEXC_SPOT_MANUAL.md#red-de-la-wallet).

## v142 cargado · guía Wallet ↔ MEXC

La cartera REAL distingue wallet conectada, red BNB, prueba de control, observación de USDT, vínculo local con View y verificación de Trade. La guía muestra STOP, actividad del bot y revisiones pendientes de órdenes o retiradas. Tras conectar View, el botón abre el vínculo de la cuenta. El estado de la guía es una lectura local pasiva y no cambia los permisos. Pasaron 804 pruebas y cinco recorridos de navegador, incluidos el manual y el de bot con ingreso y regreso a la wallet de pruebas. Queda pendiente el piloto con la cuenta real del propietario. [Uso y alcance](MEXC_SPOT_MANUAL.md#guía-wallet--mexc).

## v141 cargado · verificación de copias DEX con recibos distintos

La comprobación de una copia REAL autentica una vez cada enlace entre revisiones. Cada revisión sigue reproduciendo sus asientos y saldos. Se reutiliza un testigo idéntico sólo durante la comprobación de la misma copia cifrada y con un límite de memoria. Con 24 conciliaciones de 24 recibos distintos firmados en Anvil, la misma copia pasó de 10.102 consultas SQL a 2.409 y se reprodujeron las 24 revisiones. Pasaron 122 pruebas y cuatro recorridos de navegador. La tanda inicial detectó una expectativa de prueba que omitía una aportación. Se corrigió el cálculo esperado y se repitió el módulo completo con la aplicación idéntica. Se conservan los formatos y el esquema. [Medición y alcance](DEX_HISTORICAL_RECONCILIATION_PLAN.md#verificación-de-copias-con-recibos-distintos).

## v140 cargado · sensibilidad a las reglas de GRVT

Una prueba de derivados guardada puede reproducirse con otra captura del mismo mercado. El cálculo mantiene TRADE, MARK, financiación, estrategia, perfil y ventana. Cambia sólo el margen y la ficha de instrumento de la captura elegida. La tabla compara resultados y omite la diferencia de capital cuando se interrumpe un escenario. El JSON fija las huellas de los originales y resultados. El informe se recalcula desde LAB cifrado y no crea un nuevo backtest ni una habilitación PAPER o REAL. Pasaron 195 pruebas y cuatro recorridos, incluidos reapertura y exportación. Coinciden byte por byte 64 salidas completas de los motores publicados. Las capturas siguen siendo observaciones experimentales. [Uso](DERIVADOS_HISTORICOS_PLAN.md#sensibilidad-a-las-reglas-grvt).

## v139 cargado · cancelación puntual de órdenes Spot MEXC

En una cuenta detenida, «Cancelar orden» identifica la orden LIMIT abierta seleccionada. Exige confirmación, View y Trade de la cuenta vinculada, y una observación reciente del exchange. La cancelación usa la misma reclamación cifrada que el supervisor y conserva los fills previos. Un resultado incierto deja la orden pendiente de sincronización. La reserva se libera sólo después de confirmar el estado final, los fills y el saldo. El navegador comprobó que la orden abierta bloquea la retirada y que la retirada a la misma wallet continúa después de conciliar la cancelación. Pasaron 94 pruebas y tres recorridos con proveedores controlados. El piloto mainnet sigue pendiente. [Uso](MEXC_SPOT_MANUAL.md#uso).

## v138 cargado · operativa manual MEXC desde la wallet

Cartera real → Orden Spot permite guardar límites manuales para un par sin crear una estrategia LAB. Los límites están desactivados hasta revisar saldo e identidad y autorizar la operativa con Trade. Cada orden LIMIT pide confirmación. STOP se mantiene accesible con importes ocultos. El origen manual queda en el ámbito inmutable ya existente de política e intención y se rechaza para bots aunque tengan una prueba PAPER válida. No cambia el esquema ni los registros anteriores. El recorrido controlado sin router LAB ni PAPER realiza un ingreso, una compra, una venta y una retirada a la misma wallet BNB, con saldos y reapertura comprobados. Perder la respuesta de guardar límites no duplica el registro. Pasaron 167 pruebas y tres recorridos de navegador. Sigue pendiente el piloto mainnet con una cuenta real. [Uso](MEXC_SPOT_MANUAL.md#uso).

## v137 cargado · archivo local de investigación con IA

Laboratorio reúne las respuestas guardadas en LAB cifrado. La lista filtra por estructuras o backtests y abre cada respuesta junto a la evidencia agregada original. Se verifica su huella al leer y se pide confirmación para exportar el JSON en claro o borrar un registro. Borrar una respuesta conserva la ejecución original. Si cambia la sesión de LAB mientras responde el proveedor, ORBIS descarta esa respuesta. Se mantiene la compatibilidad con archivos previos sin migración. Pasaron 75 pruebas y cuatro recorridos de navegador. La respuesta del proveedor se verificó con un doble de prueba. La primera comprobación de la pantalla en la instancia local activó una actualización heredada de LAB plano. Se reprodujo la actualización de esquema 12 a 13 con bases de prueba en v136 y v137. La comprobación final interceptó las consultas LAB de la pantalla. [Uso y límites](IA_Y_ALMACENAMIENTO.md#capa-de-investigación-con-ia-primer-corte-optativo).

## v136 cargado · preparación DEX desde PAPER

Desde Spot PAPER o DCA, una sesión activa BNB/USDT puede abrir la revisión de Uniswap v3 en BNB Chain. La búsqueda protegida comprueba la sesión y su tipo exactos. Si falta el original o está bloqueado, la revisión queda pendiente. Al cambiar de cuenta o conectar una wallet se conserva la referencia y se limpian los límites y revisiones anteriores. Guardar el vínculo exige revisar los límites, verificar control de la wallet y elegir su ejecutor. El salto entre pantallas sólo consulta datos locales. Ocultar importes enmascara también los campos de límites y gas de esta preparación. Pasaron 179 pruebas y seis recorridos de navegador. El piloto en BNB Chain con una wallet del propietario continúa pendiente.

## v135 cargado · preparación de GRVT REAL desde PAPER

La inspección reproducida de PAPER abre la revisión de su sesión exacta en GRVT MAINNET. Los perfiles MARKET v1 y LIMIT v2/v3 se resuelven por separado y la búsqueda exacta no depende de la página actual. Editar la subcuenta conserva esa referencia de PAPER e invalida toda revisión de límites anterior. La revisión y la activación siguen exigiendo la subcuenta dedicada, la firma y la confirmación del usuario. La selección se conserva en memoria durante la revisión y se omite de la URL y del almacenamiento local. Pasaron 59 pruebas y cinco recorridos de navegador, con GRVT y MEXC controlados. El piloto con una cuenta real y la modalidad MARKET con parciales continúan pendientes.

## v134 cargado · preparación de MEXC REAL desde PAPER

Las sesiones activas de Spot PAPER y DCA pueden abrir la preparación de MEXC REAL con la sesión exacta. La selección vive en memoria y se confirma contra la lista autorizada. Si falta la sesión u observación, se muestra el motivo y queda pendiente elegir una prueba válida. Al cambiar la cuenta REAL se limpian el presupuesto y los controles de la anterior. El formulario guía a la verificación Trade y abre el borrador con su versión y par. La activación pide confirmar la cuenta, los límites y el capital real. Pasaron 48 pruebas, cuatro recorridos de navegador y las comprobaciones de estado del formulario. El recorrido controlado conserva la misma prueba desde PAPER y completa compra, venta y retirada. El piloto con una cuenta real continúa pendiente.

## v133 cargado · evolución diaria de patrimonio

Patrimonio permite consultar 7, 30, 90 o 365 días por cuenta y divisa. La serie reconstruye el libro efectivo vigente al cierre de cada día UTC. Las correcciones contables aceptadas pueden cambiar el historial. Sólo se valoran posiciones abiertas con el cierre diario del par en la fuente Spot elegida. Cada consulta admite hasta 24 pares y envía únicamente solicitudes de mercado público. La caja negativa y la falta de cierres dejan huecos. La tabla conserva importes decimales, rendimiento realizado y no realizado, aportaciones y retiradas diarias. Se invalida la vista al cambiar de cuenta, ocultar importes o bloquear la sesión. Pasaron 110 pruebas y tres recorridos de navegador. Se comprobaron 365 cierres de BTC en cada una de las cuatro fuentes admitidas.

## v132 cargado · distribución de patrimonio registrado

Inicio y Patrimonio muestran el valor de cada activo y del efectivo dentro de su divisa de valoración. Los importes se conservan como decimales. El reparto asigna 10.000 puntos básicos mediante cocientes exactos y restos mayores, con desempate estable. Cuando la caja es negativa o falta una cotización vigente, el reparto de esa divisa queda pendiente. Una divisa con valor cero carece de pesos. Inicio usa una misma lectura del libro efectivo para patrimonio, rendimiento y flujos. Se comprueba la sesión después de consultar precios. Ocultar importes retira los pesos y valores de la vista. Pasaron 85 pruebas y tres recorridos de navegador.

## v131 cargado · espacios de Inicio

General, Trading, Investigación y Cartera guardan por separado el orden, la visibilidad y el ancho de siete paneles. El botón Restablecer espacio recupera su disposición inicial. Las preferencias se guardan en el navegador y contienen sólo identificadores y opciones de disposición. El gráfico visible conserva su instancia al reorganizar u ocultar paneles. Se omite la carga inicial del histórico cuando el espacio no muestra el gráfico. Pasaron 71 pruebas y tres recorridos de navegador.

## v130 cargado · exportación conjunta de investigaciones

En Laboratorio, Estructuras permite exportar varias consultas con una única fecha de reserva UTC. El JSON conserva los casos completos, descarta etiquetas que alcanzan el corte y registra la procedencia de cada duplicado. Las exportaciones individuales se mantienen. Pasaron 68 pruebas y dos recorridos de navegador, incluida la descarga desde LAB cifrado. [Formato y límites](IA_Y_ALMACENAMIENTO.md).

## v129 cargado · Wallet y MEXC en Patrimonio

La barra Wallet ↔ MEXC abre los paneles de wallet, ingreso, orden Spot, bot y retirada. Incluye un acceso a la clave View. El teclado lleva el foco al panel abierto y se conserva la cuenta elegida. Pasaron 38 pruebas y tres recorridos de navegador. El circuito completo mantiene las confirmaciones existentes y se verificó con proveedores controlados. GRVT muestra el STOP confirmado incluso con una lectura de historial pendiente. El piloto con una cuenta real continúa pendiente.

## v128 cargado · salidas parciales en GRVT LIMIT REAL

v128 conecta perfiles LIMIT con parciales a GRVT REAL desde una prueba PAPER v3. La revisión fija los objetivos, el presupuesto y los intentos reservados para cerrar. El diario cifra y reconstruye los intentos y sus fills. La reanudación conserva los límites y pide otro permiso. Pasaron 1.149 pruebas de 92 módulos y ocho recorridos de navegador con datos y cuentas de prueba. El piloto con una cuenta del propietario y los parciales REAL con entrada a mercado quedan pendientes. [Uso y alcance](GRVT_PERP_PARTIAL_REAL.md).

## v127 cargado · salidas parciales en PAPER

v127 activa perfiles con salidas parciales en PAPER desde una reserva LAB verificada. Los perfiles conservan los porcentajes con entrada a mercado o LIMIT para LONG y SHORT. La interfaz muestra cada cierre parcial y la cantidad abierta. El archivo cifrado se reproduce tras reiniciar y exporta los mismos eventos. Pasaron 1.086 pruebas de 86 módulos y ocho recorridos de navegador. Los resultados completos de 32 escenarios LAB v3 y 16 secuencias PAPER v1/v2 coinciden con v126. La activación REAL de estos perfiles sigue pendiente. [Uso y alcance](PERP_LIMIT_PAPER.md#salidas-parciales).

## v126 cargado · salidas parciales en LAB

v126 admite hasta cinco salidas parciales reduce-only para LONG y SHORT, tanto con entrada a mercado como LIMIT. Cada objetivo conserva el fill, los costes y la cantidad pendiente. Los marcadores P1 a P5 y los tramos de validación se pueden volver a abrir desde el archivo cifrado. Pasaron 996 pruebas de 81 módulos y ocho recorridos de navegador. La prueba de carga de 5.000 velas conservó 34.986 órdenes y verificó el saldo exacto. Los 32 resultados completos con perfiles v1 y v2 coinciden con v125. Los perfiles parciales están disponibles en LAB. Quedan pendientes PAPER y REAL. [Uso y alcance](DERIVADOS_HISTORICOS_PLAN.md#salidas-parciales-en-lab).

## v125 cargado · archivo de reglas GRVT

v125 archiva las reglas de margen y del instrumento observadas al capturar o descargar datos. LAB 13 conserva las muestras y sus huellas en el archivo cifrado, y la prueba muestra la fecha de sus reglas. Las capturas anteriores siguen en sus datasets. Pasaron 977 pruebas de 79 módulos y ocho recorridos de navegador. [Uso y alcance](DERIVADOS_HISTORICOS_PLAN.md#historial-de-reglas-observadas).

## v124 cargado · recuperación de llamadas DEX

v124 extiende la búsqueda por nonce a swaps manuales, llamadas de operadoras detenidas y despliegues del ejecutor. Las llamadas externas distintas conservan la revisión de efectos y la confirmación separada. Pasan 562 casos en 55 módulos y siete recorridos de navegador. El piloto con una cuenta real continúa pendiente. [Uso y alcance](DEX_CALL_RESOLUTION_PLAN.md#búsqueda-por-nonce).

## v123 cargado · búsqueda de recibos BNB

v123 añade la búsqueda por nonce en BNB para financiación y devolución de gas. La revisión verifica el recibo finalizado y conserva la confirmación explícita. La devolución se recupera tras reabrir la bóveda sin restaurar la clave de la operadora. Pasan 386 pruebas de 42 módulos y seis recorridos de navegador con fondos de prueba. El piloto con una cuenta real continúa pendiente. [Uso y alcance](DEX_NATIVE_RESOLUTION.md#búsqueda-por-nonce).

## v122 cargado · recuperación LIMIT con historial

v122 añade una consulta al historial autenticado para recuperar la entrada LIMIT y las reducciones de GRVT si la búsqueda directa devuelve 1004. Conserva la reserva si el historial no acredita la orden. Pasan 468 casos de 36 módulos y un recorrido de navegador que recupera un intento tras reabrir la bóveda y muestra sus cuatro fills. También se repitieron el gráfico y el cierre LIMIT de la operativa anterior. El piloto con una cuenta real sigue pendiente. [Uso y alcance](PERP_LIMIT_REAL_CORE.md).

## v121 cargado · LIMIT en REAL

v121 incorpora en Cartera REAL la revisión y activación del bot LIMIT de GRVT, su historial, la reanudación con permiso separado y la liberación de reservas locales que nunca se enviaron. El gráfico muestra fills verificados. La migración de la bóveda de 79 a 80 se verificó con datos propios. Pasaron 1.600 pruebas afectadas y un recorrido de navegador que cerró y reabrió la bóveda con una orden pendiente. El piloto con una cuenta real continúa pendiente. [Uso y alcance](PERP_LIMIT_REAL_CORE.md).

## v120 cargado · LIMIT en PAPER

v120 conecta las reservas LIMIT con PAPER observado. El modelo abre órdenes LONG y SHORT con vigencia y reserva de margen, cruza bid o ask con el límite y registra fills, expiraciones y cancelaciones. Las ejecuciones se ven en el gráfico. La reapertura reproduce el diario cifrado y el JSONL. Pasan 1.092 casos de 82 módulos afectados y ocho recorridos de navegador. Los 32 resultados completos comparados coinciden con v119. La ejecución LIMIT en REAL, la operativa avanzada y la aceptación completa siguen pendientes. [Uso y alcance](PERP_LIMIT_PAPER.md).

## v119 cargado · validación LIMIT

v119 aplica el motor LIMIT a reserva cronológica, ventanas sucesivas y sensibilidad TRADE/MARK. Cada tramo parte de una cuenta ficticia nueva y conserva la versión y las órdenes originales. El informe cifrado se reproduce, exporta y abre sus operaciones en el gráfico. La referencia OOS v2 carece de autoridad y el servidor conserva la barrera de PAPER para LIMIT. Pasan 1.086 casos en 81 módulos afectados y seis recorridos de navegador, con 19 casos nuevos incluidos. Una comparación de 48 informes MARKET da resultados completos iguales a v118. Los demás módulos conservan su evidencia. [Uso y alcance](PERP_LIMIT_VALIDATION.md). El PAPER compatible, la operativa avanzada y la aceptación completa siguen pendientes.

## v118 cargado · entradas LIMIT en LAB

v118 conecta entradas LIMIT LONG/SHORT con distancia respecto al cierre de señal, vigencia, cancelación por vencimiento, diario, marcadores y reproducción cifrada. El perfil y el motor v2 conservan los originales de mercado v1. Pasan 1.067 casos en 79 archivos afectados y cinco recorridos actuales de navegador. Los 25 casos nuevos están incluidos en esos 1.067. Otros 32 escenarios de mercado comparan resultados completos idénticos con v117 y se documentan por separado. Los demás módulos conservan la evidencia previa. [Contrato y alcance](PERP_LIMIT_ENTRIES.md). La validación y PAPER de LIMIT siguen pendientes, junto a la aceptación global del proyecto.

## v117 cargado · lectura completa de cartera

v117 amplía la lectura protegida a la consulta completa de cartera y a la revisión previa. Cada cabecera se autentica una vez por lectura SQL. Se conservan el corte histórico, la detección de datos alterados y el cierre del ámbito al devolver los datos. Pasan 792 casos en los 65 módulos afectados, con identidades cotejadas y cuatro salidas 0, y siete recorridos actuales de navegador. Los diez focalizados están incluidos en esos 792. Los demás módulos conservan la evidencia de la versión anterior. [Mediciones y límites](ECONOMIC_READ_SNAPSHOT.md). El objetivo completo sigue abierto con los pendientes del documento.

## v116 cargado · continuidad y recuperación

v116 corrige la siguiente orden manual tras una revisión externa aceptada y el límite de órdenes por cuenta/fecha para transacciones compartidas. La recuperación reutiliza cabeceras, bookings y grupos sólo dentro de una lectura SQL protegida. Pasan 1.392 casos de los 147 módulos financieros afectados, con identidades cotejadas y cuatro salidas 0, y 13 recorridos actuales de navegador. Los 68 focalizados están incluidos en esos 1.392. Los módulos restantes conservan su evidencia previa. Se verifican doce recibos firmados distintos y reapertura cifrada con cantidades, costes y gas exactos. [Decisiones y límites](ECONOMIC_READ_SNAPSHOT.md). El objetivo completo sigue abierto con las tareas pendientes del final.

## v114 cargado: revisión conjunta de wallets

Cobertura **5.208 casos / 435 archivos** por identidad: 5.207 pasan en la
tanda inicial; un montaje Git se corrige sólo en su test y se repiten los
18 casos del módulo. El fallo y las salidas [1,0,0,0] se conservan; no es
otra tanda completa limpia. **27 recorridos actuales** sobre la misma
aplicación; de las **1.275 fuentes** sólo cambia después ese test. Incluye
dos proveedores detectados, selección explícita
de cuenta, recibos propios de cada wallet, contexto separado y aceptación
conjunta recuperada sin reenviar. Los 123 focalizados forman parte del total.
No hay nuevas migraciones; ocho motores/puertos financieros permanecen iguales.
La carga y la revisión pasiva mantienen REAL cerrado, 51 recursos correctos,
58 rechazos privados anónimos y seis archivos del propietario intactos.
[Contrato y límites](ECONOMIC_MULTI_WALLET_REVIEW.md). Evidencia:
`~/.local/state/orbis/verification/v114-economic-multi-wallet-r1/verification.json`.
El objetivo completo permanece abierto con los pendientes del final.

## v113 cargado: asistente de orden

El asistente está cargado y verificado: **5.189 casos / 432 archivos**, cuatro
salidas 0, identidades cotejadas y cero fallos/errores/omitidos; **26 recorridos
actuales** de navegador sobre **1.270 fuentes**. Incluye primer uso, LAB/PAPER,
cartera/DCA, MEXC, GRVT, wallet, DEX y los dos recorridos nuevos de orden manual.
Los 243 focalizados forman parte del total. Ocho motores/puertos financieros
conservan sus hashes y no hay nuevas migraciones. Contratos: 26 retenidos,
fuentes/artefactos iguales.

Única instancia PID 824816, start ticks 6312693. Reinicio autorizado tras
comprobar identidad y estado inactivo. Cincuenta y un recursos y 58 rechazos
privados anónimos verificados; escritorio/móvil sin errores ni escrituras en
el recorrido pasivo. REAL permanece cerrado y los seis archivos de datos
conservan sus hashes. La herramienta no pudo recargar la pestaña del usuario;
el navegador propio verifica la instancia, sin desbloquear ni operar.

Evidencia: `~/.local/state/orbis/verification/v113-economic-order-assistant-r1/verification.json`.
El objetivo completo sigue abierto con los pendientes del final del documento.

## Preparación de v113: evidencia de desarrollo

El [asistente de movimientos simultáneos](ECONOMIC_ORDER_ASSISTANT.md) conecta
selección manual, índices de red fijos, revisión financiera y recuperación del
criterio vigente, tanto para recibos existentes como lotes nuevos. Dos nuevos
recorridos firmados/cifrados y cinco regresiones de navegador pasan sobre las
mismas 1.270 fuentes. Pasan 243 casos focalizados / 19 módulos, con identidades
cotejadas, incluidos 22 nuevos. La API verifica un ciclo actual sin recurrir a un
saldo alternativo. Batería general en curso: 5.189 casos / 432 archivos; no se
anuncia aún su resultado. Los motores y formatos financieros permanecen iguales.
Se repiten también 19 recorridos básicos/de compatibilidad: 26 actuales en total,
sin contar recorridos anteriores como repetidos.
No está cargado; la instancia y `latest.json` continúan en v112. Permanecen
pendientes wallets nuevas simultáneas, efectos externos desconocidos, rendimiento
ampliado y el resto de la auditoría. El objetivo completo sigue abierto.

## v112 cargado: revisión contable DEX y gas

La revisión conectada de recibos, bloques y gas está cargada en una única
instancia local: PID 735639, `127.0.0.1:8000`. Pasan **5.167 casos / 430 archivos**,
con colección/identidades cotejadas y cuatro salidas 0, sin fallos, errores ni
omitidos. Pasan **24 recorridos de navegador** sobre las mismas **1.266 fuentes**,
incluidos primer uso LAB/PAPER, cartera/DCA, MEXC completo, GRVT perpetuo,
wallet, devolución DEX y la revisión de gas. Los 186 casos focalizados forman
parte de esa batería; no se suman como casos diferentes. Contratos: 26 retenidos
con ocho fuentes de contrato y ambos artefactos iguales a la referencia.

Reinicio autorizado con identidad exacta y estado REAL cerrado. La igualdad
de los seis archivos con la referencia previamente verificada sin actividad,
sin journals pendientes, comprueba el estado persistido sin abrir las bases
desde el cargador. Los archivos siguen iguales tras la carga y el navegador.
Se comprueban 51 recursos y 57 rechazos privados anónimos; escritorio/móvil sin
errores JS ni solicitudes de escritura. Un error de ruta local del comprobador
de recursos se conserva; se corrigió en un verificador pasivo que comprobó el
mismo proceso, sin segundo reinicio ni cambios de aplicación.

SQL preparado: 75. No se ha abierto ni migrado REAL del propietario, protegido
ningún legado, conectado credenciales ni utilizado fondos. Siguen pendientes las
brechas enumeradas al final; esta publicación no completa el objetivo.
Evidencia: `~/.local/state/orbis/verification/v112-economic-review-r1/verification.json`.

## Preparación de v112: evidencia focalizada previa a la carga

La revisión de cuenta ya conecta originales, pruebas firmadas de bloque,
testigos, registro/aceptación atómicos y cálculo actual. Incluye las nueve
familias de gas, perfiles separados de operaciones, gas parcial, criterios
versionados y conservación del historial. La interfaz optativa «Gas» permite
revisar recibos nuevos y gastos previamente registrados. Una respuesta perdida
se recupera por digest sin reenviar la confirmación. La publicación cargada
sigue siendo v111; estas capacidades no se atribuyen a su proceso.

La evidencia focalizada pasa **186 casos / 15 módulos** y **cinco recorridos de
navegador**, incluidos dos nuevos con recibos firmados de Anvil, SQLCipher real,
escritorio/móvil y bloqueo/reapertura. Fuentes congeladas: 1.266. Evidencia:
`~/.local/state/orbis/verification/economic-gas-ui-dev-final-r1/verification.json`.
La batería general está en curso sobre esas fuentes: **5.167 casos / 430
archivos**, cuatro procesos propios. No se presenta como aprobada hasta leer
sus resultados y comprobar las identidades de todos los casos.

Esto avanza la continuidad DEX de la tarea 3: prueba de orden recuperable,
revisión de recibos registrados fuera de orden, gas y materialización conjunta
están conectados a API/interfaz. Siguen pendientes los asistentes de empates y
wallets nuevas simultáneas, llamadas externas exitosas desconocidas, medición
con linajes largos y auditoría global de los requisitos. No hay piloto mainnet
del propietario, uso de sus fondos/credenciales, ni acceso/migración de sus
almacenes. El objetivo completo permanece abierto.

Se repiten además ocho recorridos básicos sobre las mismas 1.266 fuentes:
`basic_journey`, `portfolio_dca_journey`, `mexc`, `mexc_bot_chart`, `wallet_manual`,
`dex_dca`, `dex_return` y `perp_bot_activation`. Todos pasan; primer uso y cartera
incluyen reapertura en otro proceso. Evidencia propia y transportes controlados:
`~/.local/state/orbis/verification/economic-gas-ui-basic-flows-r1/browser.json`.
La medición pura de 100.007 asientos conserva cálculo y originales (1,381 s con
carga concurrente). Cincuenta aceptaciones selladas de un recibo firmado propio
conservan prefijo y copia fría (captura 0,059 s, 479 consultas). Son primeras
medidas delimitadas, no aceptación de rendimiento de todo el catálogo ni de
linajes con muchos recibos distintos. Detalles y pendientes en el registro de
progreso y `economic-gas-lineage-performance-r1/performance.json`.

## v111 cargado: creación, protección y reentrada de PAPER manual

El lanzador abre Ajustes con un archivo ausente o cifrado cerrado. Crear,
desbloquear y proteger el legado usan el motor estable, acceso revocable y
SQLCipher reales. La protección drena el servidor, verifica una copia y la
igualdad lógica, y vuelve al mismo puerto bloqueada. Recupera una respuesta
perdida por ID; reabrir en otro proceso conserva cantidades, saldos e IDs.

Pasan 4.812 casos / 404 archivos, identidades cotejadas y ocho salidas 0.
Después se ajustan dos componentes JS y su recorrido, manteniendo iguales
backend y tests unitarios; se repiten 19 casos de UI, cinco recorridos afectados
y cuatro regresiones de copias/bloqueo. Trece recorridos compatibles de la misma
tanda se conservan explícitamente, sin afirmar una repetición completa final.

Instancia única PID 220692 en 127.0.0.1:8000, recursos actuales y UI sin errores,
51 rechazos privados anónimos y huellas de archivos del propietario iguales
antes/después de reiniciar y revisar la UI. REAL sigue bloqueado; los legados
del propietario siguen planos hasta su acción. No se cifran ni desbloquean sus
archivos, no se conectan credenciales y no se realizan operaciones de dinero.
Evidencia: `~/.local/state/orbis/verification/v111-manual-paper-gui-r2/`.
[Uso y límites](MANUAL_PAPER_GUI_USAGE.md). El objetivo completo sigue abierto.

## v110: activación perpetua de extremo a extremo

API y GUI ya conectan el origen LAB/PAPER reproducido con revisión de presupuesto
REAL, autorización de un uso, fuente propia, supervisión y cierre conciliado.
El recorrido nuevo de navegador pasa con proveedores controlados, junto con
pruebas de integridad, épocas, revocación, vencimiento y concurrencia.
La verificación final acredita 4.790 casos / 401 archivos y quince recorridos
actuales. Un test esperaba dos recursos antiguos; se corrigieron esas
expectativas y se repitieron sus 19 casos, conservando byte por byte aplicación,
fixtures y pruebas restantes. El informe registra la salida inicial fallida y
la resolución, sin presentarla como ocho salidas iniciales 0.

v110 está cargado en una única instancia local, con UI sin errores y 51 rutas
privadas rechazando anónimos. REAL permanece bloqueado, sin acciones de dinero
ni cifrado/migración de almacenes del propietario. **No es un piloto humano
mainnet ni finalización del objetivo completo.**
[Recorrido, evidencia y pendientes](GRVT_PERP_BOT_ACTIVATION.md).

El [coordinador y supervisión v5](GRVT_PERP_SUPERVISION.md) forma parte de esa
activación. Sus pruebas dirigidas se incluyen en la aceptación de v110.

## v109: señales y concesión perpetua internas

Se verifica fuente pública independiente del saldo PAPER, origen LAB/PAPER
reproducido, concesión v4, diario cifrado, selección durable de señal/plan REAL,
claim v3, envío protegido, fills/costes/funding, recibo y lectura fría sin
autoridad. Incluye LONG/SHORT × CROSS/ISOLATED, revocación, respuesta perdida,
reetiquetado/alteración, capacidad, bloqueo/reapertura y compatibilidad anterior.
Es evidencia de módulos conectados con proveedores propios controlados;
**no acredita activación del bot nuevo en producción ni su GUI**.

La tanda completa final pasa **4.683 casos / 396 archivos** y catorce recorridos
actuales de navegador: GRVT, MEXC, DEX, retorno, primer uso, PAPER observado y
protección/reapertura. Los otros recorridos previos son históricos y no se
cuentan como repetidos. No se recompilan contratos sin cambios: se conserva
su evidencia y se cotejan fuentes/artefactos. La UI cargada responde sin
errores y REAL permanece bloqueado; no se usan dinero o claves del propietario.
Evidencia: `~/.local/state/orbis/verification/v109-perp-bot-grant-r1/`.

Quedan el coordinador con permiso RAM propio, vigilancia de posición efectiva
y costes, salidas que conserven bid/ask/prefijo/tiempos del modelo nuevo,
revisión sellada/GUI y aceptación completa. [Contrato y límites](GRVT_PERP_BOT_GRANT.md).

## Recorrido básico comprobado desde cero

`basic_journey` en `scripts/verify_orbis.py` usa la aplicación y su ciclo de
vida reales, bases propias inicialmente vacías y el trabajador Spot PAPER
real. Sólo se controla el proveedor público de mercado y su reloj. No usa
credenciales, direcciones, bases ni dinero del propietario.

| Paso de aceptación | Evidencia directa |
| --- | --- |
| Crear LAB y Spot PAPER cifrados desde Ajustes | Formularios reales, respuestas HTTP y archivos SQLCipher propios |
| Descargar histórico y crear una estrategia | Paginación y validación reales del descargador MEXC con OHLC controlado; 120 velas, definición LONG y versión persistida |
| Probar LONG y SHORT sin sobrescribir la primera prueba | Dos versiones y dos backtests distintos; resultado reproducible, operaciones y marcas visibles |
| Compartir e importar un bot nativo | LAB `orbis.lab.strategy.v1` y DCA `orbis.automation.dca.v1` con copias independientes y sin sesiones activas |
| Llevar la versión probada a PAPER | Botón disponible tras lectura verificada; conserva run, dataset y versión LONG aunque la última edición sea SHORT |
| Activar PAPER por decisión del usuario | No hay sesiones antes del clic en «Activar ficticio»; caja inicial 1.000, independiente del beneficio histórico |
| Ejecutar compra y venta ficticias | El trabajador recibe observaciones nuevas, compra al ask y vende al bid por objetivo; diario, estado y marcas BUY/SELL concuerdan |
| Detener y recuperar | Parada explícita; terminación del servidor y nuevo proceso sobre los mismos archivos; ambas bases empiezan bloqueadas |
| Volver a abrir sin cambiar resultados | Desbloqueo mediante Ajustes; mismos hashes, métricas, operaciones, versión y estado detenido; no se crea ni activa otra sesión |
| Mantener los entornos separados | La prueba no crea bóveda REAL ni hace escrituras `/real/`; el capital PAPER no se hereda del resultado LAB |

Los dos procesos, logs, capturas y resultados se conservan en
`browser-basic_journey/`. Esta prueba demuestra el recorrido local con datos
controlados; no demuestra conectividad actual del proveedor ni rentabilidad.

## DCA y cartera: primer uso comprobado

`portfolio_dca_journey` crea LAB, automatizaciones y una bóveda REAL propios
desde las pantallas iniciales, sin registros presembrados. Sus datos REAL son
un libro manual ficticio de prueba, sin cuenta de exchange ni envío de órdenes.
El servidor es la aplicación completa con su trabajador DCA; únicamente el
mercado público diario y el reloj se controlan en el proceso de prueba.

- DCA diario: presupuesto 500, importe 100, extra 50 por caída, máximo mensual
  150 y exposición configurada. La comparación histórica incluye DCA fijo,
  condicionado y compra inicial; el resultado se verifica antes de fijarlo
  como origen de PAPER. No se activa una sesión hasta pulsar el botón.
- Dos observaciones procesadas por el trabajador: BASE + DIP invierten 150 en la
  primera; la siguiente registra SKIP por límite y conserva 350 de caja,
  dos unidades y un solo extra. La sesión se detiene expresamente.
- Cartera manual: aportación 1.000 EUR, dos compras BTC con comisión, venta
  parcial BTC, compra ETH y retirada 25. Se comprueban caja 771,5, beneficio
  realizado 123,5, BTC 1,5 con coste 226,5 y media 151; ETH 2 con coste 100,5
  y media 50,25. Las comisiones BTC/ETH son 3 y 0,5 EUR. Los flujos de capital
  permanecen separados del beneficio y no alteran el capital DCA.
- Una venta de ETH superior al saldo y una nota con dirección completa se
  rechazan con 422 sin añadir eventos. La exportación CSV contiene seis
  movimientos; la vista previa de reimportación detecta los seis duplicados.
- Otro proceso reabre los mismos archivos SQLCipher bloqueados. Tras
  desbloquear mediante las pantallas reales, recupera el resultado DCA,
  la sesión STOPPED y el libro completo exactamente iguales. No reactiva
  sesiones ni añade compras, ventas o asientos.

Evidencia: `v86-portfolio-dca-r4/browser-portfolio_dca_journey/` bajo el directorio
local de verificación. `result.json`, `reopened.json`, logs y capturas conservan
las identidades de los dos procesos y los resultados de la prueba. Esto cierra
el recorrido mínimo de primer uso de §16.2 y §16.3; no convierte en completas
las ampliaciones de la especificación ni el piloto con dinero del propietario.

La regresión v87 añade recuperación tras errores: una dirección completa
rechazada permanece editable en su transferencia; al cambiar a compra o retirada,
la nota oculta no se incluye en el POST. Se comprueban los payloads reales y
los mismos saldos finales. `manual_portfolio` comprueba por separado que las
notas visibles de transferencia y ajuste sobreviven al CSV, y recorre privacidad,
importación y restauración cifrada con la aplicación completa y PAPER legado
cifrado propio. No cambia la validación de direcciones del servidor.

## Reserva DCA reproducible

La entrega v88 añade `DCA → Fuera de muestra`: selecciona la versión guardada
y el histórico, reserva el tramo final y compara condicionado, fijo y compra
inicial. Cada tramo empieza con presupuesto y estado financiero independientes;
la reserva conserva únicamente referencias del prefijo ya conocido. La primera
señal usa la última vela previa al corte y las compras pertenecen a la reserva.

El artefacto fija dataset, versión, corte, hashes, `dca-1.6` y
`dca-holdout-1.0`. Su contexto decimal es propio y constante. La tabla de
automatizaciones se incorpora mediante migración aditiva 5→6 y participa en
cifrado, copias y restauración. La UI obtiene un resumen verificado antes de
mostrar cifras; conserva los resultados de la versión antigua tras editar el
plan. Una reserva con menos de 30 velas se rechaza sin mostrar cifras anteriores.

`dca_validation_journey` prueba creación desde bases vacías, CSV identificado
como importado, tres variantes, capital independiente, corte inválido,
escritorio/móvil y recuperación en otro proceso. Después comprueba descarte de
respuesta atrasada y bloqueo de un resultado alterado. Los recorridos MEXC,
DCA/cartera, DEX DCA y gráfico del bot MEXC se vuelven a comprobar.

La etiqueta «Reproducible» no acredita independencia estadística ni éxito futuro.
En v89, LAB y DCA permiten exportar una referencia JSON tipada tras repetir la
validación. Fija versión, datos, método, motores y ventanas, conserva su huella
tras reabrir bases cifradas y rechaza originales alterados. El recorrido comprueba
LONG/SHORT, ventanas sucesivas, DCA y respuestas tardías. La verificación LAB ya
es un servicio de dominio compartido; las puertas REAL existentes todavía no
consumían esta referencia. En v90 MEXC exige y conserva la validación en cada
activación/rearme nuevo, con comprobación de origen PAPER, mercado, temporalidad,
motor y corte; el snapshot permanece con las autorizaciones y la renovación.
Su recorrido compra/venta/wallet pasa con transportes controlados. En v91 GRVT
MAINNET incorpora la reserva, backtest y prefijo PAPER observado en revisión v2
y concesión v3. Los formatos anteriores permanecen legibles. Se conservan los
nanosegundos exactos y se declara la ausencia de modelo histórico de margen y
financiación. En v92 DEX incorpora la reserva a preparación, vínculo y cada
activación/rearme, con señales MEXC y ejecución Uniswap separadas. Guarda el
contexto DCA efectivo y conserva la última reserva al renovar/reabrir. Completa
la integración de procedencia del paso 3 de [validación OOS](VALIDACION_OOS_PLAN.md),
sin reinterpretar los formatos financieros anteriores ni acreditar el coste
histórico DEX. En v93 pasan 793 pruebas afectadas/compartidas y 27 recorridos
finales. La cobertura por identidad reúne 4.060 casos y 47 recorridos, con 3.267
y 20 resultados previos sin cambios respectivamente. No es una repetición completa
sobre las fuentes finales. Los intentos fallidos se conservan y no se cuentan
como entregas aprobadas.

v93 comprueba renovación LONG/DCA tras ejecución, cancelación o reversión
recuperadas: dos renovaciones sucesivas, nueva compra/venta, cuotas y gas
acumulados, claves consumidas, STOP recuperado y reapertura cifrada sin autorun.
Rechaza hechos aún no aceptados en el corte, comprobantes alterados, degradación
de formato y gas agotado aunque se aporte BNB adicional. Dos recorridos nuevos
comprueban confirmación del propietario, aplicación, reserva original y rearme
por la UI. Las pruebas usan EVM y bóvedas propias; no acreditan un piloto humano.

## Cobertura de la especificación y límites

Esta tabla distingue pruebas directas del recorrido nuevo, implementaciones
con evidencia anterior y requisitos que todavía requieren trabajo o una
comprobación específica. Las pruebas anteriores sólo se conservan si sus
fuentes y la identidad de sus casos siguen siendo compatibles con la entrega.

| Requisito | Implementación / evidencia que debe revisarse | Estado de aceptación |
| --- | --- | --- |
| §1–2. LAB/PAPER/REAL separados, privacidad y trazabilidad | `app/lab`, `app/automation`, `app/derivatives`, `app/portfolio`; `basic_journey`, pruebas de fronteras, fuentes y bóvedas | Recorrido LAB→PAPER comprobado; REAL usa sus puertas propias |
| §3–4. Navegación, Inicio, importes, preferencias y espacios | `app/web/static/app.js`, `estudio.js`, gráficos compartidos; recorridos de Inicio/Operativa y privacidad | Implementado en partes; falta auditoría conjunta de cada panel configurable y fluidez con grandes históricos |
| §5. Gráficos, herramientas, fuentes e históricos largos | Estudio, rangos compartidos, archivo BTC y adaptadores; pruebas de cobertura y rangos | Existen implementaciones y pruebas; no se presume disponibilidad de todas las temporalidades en todos los proveedores |
| §4–5. Alertas por precio | Sección Alertas, Estudio e Inicio. Recorrido de navegador market_alerts | Vigilancia de varios mercados, registro local y rearme verificados con la app abierta |
| §6 y §16.1. Crear, ejecutar, inspeccionar, versionar y recuperar LONG/SHORT | `app/lab/engine.py`, `store.py`, `api.py`; pruebas LAB y `basic_journey` | Mínimo local comprobado desde cero; semántica ampliada de derivados todavía parcial |
| §6. Modos realista/experimental y parámetros de ejecución avanzados | El validador acepta un conjunto explícito de parámetros; rechaza los no implementados | Parcial: margen/funding/liquidación experimentales, libro compartido con PAPER observado y puerta REAL separada comprobados; faltan reglas históricas acreditadas, órdenes y posiciones avanzadas |
| §7. Eventos, estadísticas, comparación, validación y optimización | `app/lab/analytics.py`, `research.py`, `validation.py`, `optimization.py`, `volatility_regimes.py`; `automation.validation` y pruebas correspondientes | Incluye reserva DCA con persistencia/reapertura verificadas; no acredita independencia de una estrategia ni la aceptación global de todos los gráficos previstos |
| §7.5. Sin datos futuros, gaps y ambigüedad intravela | Motor y pruebas de gaps, cobertura, precisión, multi temporalidad y repetición | El motor aplica límites explícitos; no se presenta la simulación como ejecución realizable garantizada |
| §8. PAPER actual, diario, interrupciones y comparación | Spot, DCA y monitor de derivados; archivo de observaciones y análisis separado de gaps | Compra/venta/parada/reapertura Spot y nuevo PAPER observado comprobados. El motor nuevo registra liquidación modelada al incumplir mantenimiento; no acredita fills de liquidación del exchange ni resuelve una trayectoria perdida |
| §9 y §16.3. DCA, presupuesto, límites, histórico y modo ficticio | `app/automation/dca.py`, `paper_dca.py`; pruebas DCA y `portfolio_dca_journey` | Primer uso mínimo, comparación, activación explícita, límite mensual y recuperación comprobados desde bases vacías; ampliaciones todavía parciales |
| §10 y §16.2. Libro patrimonial, costes, comisiones, CSV y recuperación | `app/portfolio/domain.py`, `repository.py`, `valuation.py`, `csv_import.py`; pruebas `test_real_*` y `portfolio_dca_journey` | Primer uso mínimo multiactivo, venta parcial, comisiones y recuperación comprobados en bóveda propia; no se ha inspeccionado ni modificado el patrimonio del propietario |
| §11. Cifrado, bloqueo, copias y no persistir direcciones completas | Bóvedas SQLCipher, backups, `sensitive_text.py`, referencias HMAC de wallets; recorridos de protección y PAPER manual | Bases nuevas y migración gráfica LAB/DCA/Spot PAPER/GRVT PAPER/PAPER manual comprobadas con SQLCipher propio y reapertura. El legado del propietario sigue plano hasta su acción; `unlocked:false` no prueba cifrado |
| §12–13. Arquitectura modular, datos y semántica compartida | [Arquitectura](ARQUITECTURA_V0.2.md), migraciones, evaluador compartido, referencias de origen | Python/FastAPI y UI existente son la decisión documentada; React/Tauri eran tecnologías candidatas, no una migración ya realizada |
| Ampliación. Wallet↔MEXC, saldo previo, compra/venta y retirada | [Flujo MEXC](BOT_MEXC_SPOT.md), ingresos/retiradas y `browser_mexc_roundtrip_flow.py` | Recorrido controlado existente; piloto humano mainnet pendiente; ver saldo no contabiliza ni autoriza por sí solo |
| Ampliación. Bots MEXC y GRVT, LONG/SHORT | [MEXC](BOT_MEXC_SPOT.md), [GRVT](GRVT_BOT_MAINNET.md), pruebas de permisos, parada, cierres y recuperación | Implementados con autorización independiente; comprobar proveedor, cuenta y permisos reales corresponde al piloto, sin usar claves View para operar |
| Ampliación. Un DEX importante y wallet BNB | [DEX BNB](DEX_BOT_BNB.md), pruebas EVM propias y recorridos de wallet/operadora; [revisión contable y gas](DEX_GAS_ORDER_EXTENSION.md) | Swap manual y bots LONG/DCA con límites; renovación tras resultados recuperados comprobada. Revisión de varias wallets y recuperación de recibos externos cargadas en v115. Quedan otros adaptadores de nonce, rendimiento ampliado y piloto humano |
| Ampliación. Bots recibidos de terceros | Importación declarativa nativa; nunca se ejecuta código importado | Otros formatos diferidos por el propietario hasta disponer de un archivo; no se presume compatibilidad |
| Ampliación. IA y datos para investigación | `app/lab/ai_research.py`, datasets y exportación con procedencia | No equivale a un modelo entrenado ni a conectar una cuenta ChatGPT; debe definirse y verificar el alcance concreto antes de presentarlo operativo |
| §15. Correlaciones, Monte Carlo, complementos y futuras clases de activo | Funciones futuras explícitas de la especificación | No forman parte de la aceptación mínima inicial; no se dan por implementadas |
| §16.4. Calidad, migraciones, precisión y respuesta fluida | XML por caso, manifiestos de fuente, pruebas de cálculo/datos, copias y recorridos de navegador | Cada entrega debe justificar su alcance; el número de tests no prueba por sí solo la aplicación completa |

## Archivo de derivados y situación local — v94

TRADE y MARK se descargan y archivan por separado junto a funding con ancla y
capturas actuales de reglas. La UI exporta los datos exactos; no produce todavía
un backtest completo de margen/liquidación. Cada lectura y copia verifica el
archivo nuevo y no lo mezcla con los datasets existentes. El recorrido de
navegador usa LAB cifrado propio creado desde cero, copia, exportación y otro
proceso que recupera el mismo archivo. La fuente pública real se comprobó sin
credenciales ni transacciones financieras.

La tanda completa r2 aporta 4.107 aprobados y un fallo de montaje LAB antiguo.
Tras corregir sólo ese test, r3 repite los 91 casos del archivo: junto con 4.017
aprobados sin cambios cubre las 4.108 identidades. Los 48 recorridos de navegador
pasan con la misma aplicación. No es una repetición completa sobre las fuentes
finales. El código de producción responde en `127.0.0.1:8000`.

El entorno local conserva LAB, DCA y los dos PAPER legados en formato plano.
Las pantallas de Ajustes lo identifican; el indicador de sesión `unlocked:false`
no los convierte en bases cifradas/bloqueadas. Guardar el archivo nuevo requiere
LAB cifrado. La migración actual exige cerrar ORBIS y usar un script con una
contraseña elegida por el propietario; no se ha cifrado su legado ni se ha
abierto/migrado su bóveda REAL durante esta entrega. Debe añadirse una ruta de
migración accesible desde la aplicación antes de dar por completa su usabilidad.

## Protección desde Ajustes — v95

La limitación gráfica de los cuatro almacenes anteriores se resuelve mediante
**Proteger legado**. El recorrido nuevo cifra LAB, DCA, Spot PAPER y GRVT PAPER
propios, verifica copias y filas, desbloquea por sus formularios y conserva el
archivo perpetuo después de cerrar y arrancar otro proceso. Contraseñas ausentes
de almacenamiento web, errores públicos, logs, argumentos y entorno; foco estable
durante sondeos. No se cifra REAL ni el PAPER manual desde este formulario.

La regresión r1 completa aprueba 4.129 casos y los 49 recorridos de navegador.
Una revisión visual descubre después un aviso antiguo del resumen; se retira
esa frase y se actualiza la caché. La r2 repite 19 pruebas de interfaz y los dos
recorridos de creación/reapertura afectados. La cobertura por identidad conserva
4.110 casos y 47 recorridos de r1; no se presenta como otra tanda completa sobre
el código final. El contrato y todos los módulos financieros permanecen iguales.

Servidor local en el mismo PID 85605, UI/activos correctos, sin errores JS,
sin envíos privados y 46 rutas REAL rechazadas sin token. Los cuatro legados
del propietario siguen planos: no se ha enviado el formulario ni utilizado
una contraseña suya. La evidencia activa es
`~/.local/state/orbis/verification/v95-protection-r2/verification.json`.
PAPER manual mantiene instrucciones offline; esta entrega no cierra el requisito
de cifrado gráfico de todos los archivos del producto ni el motor de derivados.

## Núcleo contable experimental — v96

`lab.perp_account` y `perp_book_types` incorporan órdenes/fills, posiciones netas,
aumento/reducción/reversión, margen inicial/pre-trade, financiación asentada y
liquidación completa cross/aislada con remanente y déficit explícitos. El diario
se reconcilia por delta y los estados son inmutables. Las 50 pruebas propias
incluyen cifras calculadas a mano, costes residuales, duplicación de funding,
cancelaciones previas al fill y separación de garantías. Se describen en
[aceptación del núcleo](PERP_BOOK_ACCEPTANCE.md).

Pasan 382 casos actuales y cuatro recorridos; las otras 3.797 identidades y
45 recorridos se conservan porque todas las fuentes previas permanecen iguales.
La cobertura total por identidad es 4.179 casos y 49 recorridos, no una tanda
completa nueva. Backend PID 104871, fuente actual, 46 rutas REAL rechazadas sin
token y smoke UI correcto. Evidencia:
`~/.local/state/orbis/verification/v96-account-core-r1/verification.json`.

La aplicación existente no importa todavía este núcleo. No produce backtests
guardados, gráficos, OOS ni nuevas sesiones PAPER; las referencias y los permisos
REAL no cambian. Por tanto no cierra §6–8 ni la tarea 2 de abajo. Faltan el perfil
inmutable, la cronología histórica con cobertura/ambigüedad verificadas y el
recorrido completo de archivo, reproducción y uso por el usuario.

## Pruebas históricas de derivados — v97

El [contrato de pruebas](PERP_EXPERIMENTS_PLAN.md) conecta el núcleo a datos,
versión de estrategia y perfil inmutables. Guarda una ejecución cifrada con
margen/funding/costes/liquidación y permite reproducirla desde originales, ver
marcas TRADE/MARK, operaciones, equity y diario, exportar y recuperar en otro
proceso. Datos alterados, resultados manipulados con hash nuevo y reautenticación
durante cálculo se rechazan. Nunca activa PAPER ni concede autoridad REAL.

El modo es experimental: reglas actuales e hipótesis OHLC. Oráculos financieros
independientes cubren dirección, costes, funding, cross/aislado, umbrales y
redondeos; la UI recorre creación, edición posterior de versión, copia, reapertura,
respuesta tardía y bloqueo. Hay 4.230 casos por identidad y 50 recorridos actuales
sobre aplicación idéntica. El único fallo inicial fue un test de esquema antiguo;
se corrige sólo ese módulo y pasan de nuevo sus tres casos. No se afirma una
segunda tanda completa. La evidencia cargada está en
`~/.local/state/orbis/verification/v97-perp-engine-r2/verification.json`.

Esto avanza §6–7, pero no cierra el modelo profesional completo: faltan OOS y
comparación para este motor, PAPER compatible, ejecución avanzada y reglas
históricas acreditadas. Las referencias de los flujos REAL existentes conservan
su semántica anterior; no se atribuye a ellas este nuevo modelo de margen.

## Validación perpetua — v98

[Reserva y sensibilidad perpetua](PERP_VALIDATION_PLAN.md) conservan una versión
fija y reinician cuentas por tramo. El usuario crea y guarda reservas, ventanas
sucesivas y cuatro escenarios, inspecciona sus operaciones/marcas en el gráfico,
exporta y recupera resultados en otro proceso. Las referencias OOS fijan perfil,
motores, ventanas y hashes, con autoridad NONE. La sensibilidad no se presenta
como OOS ni como un límite garantizado de ejecución.

Pasan los 4.262 casos actuales completos y nueve recorridos afectados. Se
conservan 42 recorridos anteriores de funciones wallet/exchange/DEX sin cambios;
no se afirma repetición completa del navegador. La nueva UI prueba los tres
métodos, cuenta nueva, inspección, edición posterior de versión, copia cifrada,
reapertura exacta, respuestas tardías, manipulación y bloqueo. La evidencia es
`~/.local/state/orbis/verification/v98-perp-validation-r1/verification.json`.

Esto incorpora OOS y sensibilidad para el motor experimental. No cierra PAPER
compatible, su nuevo vínculo de investigación en revisión REAL, las reglas
históricas ni la ejecución avanzada. Las puertas REAL existentes conservan las
referencias de precios anteriores y no admiten esta referencia perpetua nueva.

## Derivados observados — v103 cargado

Motor compartido, archivo cifrado, trabajador, servicio, API y UI están
cargados en el servidor local. El recorrido completo en dos procesos verifica
reserva → activación → compra/cierre ficticios → gráfico/marcas → diario →
export/replay → reapertura sin autoarranque. La recepción conserva recogida y
aceptación; controles y append se serializan, sin convertir un dato futuro en
conocido. El indicador de cobertura comprueba también la edad del proveedor.

En v103, la colección de 4.374 casos coincide con la tanda completa 4.365 y las
regresiones focalizadas 575/28. Once recorridos afectados pasan en etapas; 42
anteriores se conservan con fuentes idénticas. Evidencia:
`~/.local/state/orbis/verification/v103-observed-ui-r3/verification.json`.
No equivale a otra ejecución completa de todos los casos sobre fuentes finales.

v104 permite elegir plazo de quince minutos hasta treinta días al crear o
abrir GRVT PAPER, con caducidad fija y sin persistir claves/política. Pasan
392 casos y cuatro recorridos afectados: creación de 24 horas, desbloqueo de
siete días, claves borradas del formulario, compra/cierre y reapertura bloqueada.
Las pruebas con reloj controlado verifican los vencimientos; no acreditan
observación real durante treinta días. Evidencia cargada:
`~/.local/state/orbis/verification/v104-paper-duration-r1/verification.json`.
v105 extiende el selector a LAB, Spot y DCA, conservando la comprobación LAB
antes de cada compra DCA. Corrige además que un desbloqueo pendiente pudiera
deshacer un bloqueo. El reloj controlado de claves avanza con el mercado en
las pruebas Spot/DCA; no se simula una sesión larga manteniendo su clave en
quince minutos reales sin probar el vencimiento.

Se verifica una tanda completa 4.431, seguida de 443 casos afectados tras esa
corrección: colección final exacta 4.451 en 381 archivos, sin afirmar otra
repetición completa final. Pasan los once recorridos afectados sobre fuentes
finales; 42 anteriores se conservan. Evidencia cargada:
`~/.local/state/orbis/verification/v105-local-duration-r2/verification.json`.
[Plazos y dependencias](LOCAL_OBSERVATION_SESSIONS.md).
Continúan pendientes la reconciliación tras GAP, la medición de observaciones
prolongadas reales y la revisión de esta procedencia para REAL. El objetivo
continúa abierto con las brechas de abajo.
[Diseño](PERP_OBSERVED_PLAN.md), [uso](PERP_OBSERVED_USAGE.md).

## Cierre tras GAP — v106 cargado

Una posición interrumpida admite cierre **estimado** explícito, con datos actuales,
funding continuo, ancla fijada y libro compartido. Se conserva el original;
ningún checkpoint cargado se convierte en cuenta. Una lease de sesión protege
contra observadores vivos entre procesos. Copia cifrada, hash y exportación
compuesta se reproducen; no se presentan como operaciones observadas o REAL.

El recorrido nuevo compra, provoca GAP, acepta la incertidumbre, guarda cierre,
pierde su respuesta y recupera por GET; compara precio/caja exactos, gráfico/marca
violeta, JSONL y otro proceso sin cotización/autoarranque. Los valores desaparecen
al bloquear. 4.483 casos / 385 archivos pasan sobre la aplicación final; sólo se
ajusta después un fixture de navegador por un estado transitorio, con fuentes y
colección cotejadas. Once recorridos afectados aprobados por etapas; 42 retenidos.

Benchmark controlado de 100.000 velas y treinta días de funding simulado verificado,
con una posición y ocho eventos originales; no acredita uptime ni linaje extenso.
Evidencia: `~/.local/state/orbis/verification/v106-gap-close-r2/verification.json`.
[Guía](PERP_GAP_CLOSE_USAGE.md). La resolución exacta de trayectoria perdida no
se promete: la incertidumbre persiste explícita. La puerta REAL del nuevo motor
sigue pendiente, junto a las brechas siguientes; este tramo no cierra el objetivo.

## Revisión perpetua para REAL — v107 cargado

En Patrimonio → GRVT, **Revisar PAPER → REAL** reproduce los originales LAB y
PAPER, fija el prefijo y compara dos lecturas de margen/apalancamiento por
instrumento. Las cantidades ficticias no determinan el presupuesto REAL. GAP,
liquidación, cierre estimado, muestra vacía y discrepancias son condiciones
explícitas; no se publica una autorización de ejecución.

Pruebas propias comprueban el dataset LAB frente a un seed alterado con hashes
recalculados, replay frío sin arranque, épocas, bloqueo y desconexión durante
HTTP. El navegador comprueba coincidencia, discrepancias, eliminación de
resultados, respuesta tardía y escritorio/móvil. Se corrige además la carrera
del estado inicial de conexión MEXC/GRVT, con reproducción y regresión.

Tanda completa final: 4.526 casos / 388 archivos, identidades cotejadas y ocho
salidas 0. Trece recorridos de navegador repetidos sobre fuentes finales;
otros recorridos conservados son evidencia histórica. Backend, activos,
módulos y 47 rechazos anónimos comprobados sin desbloquear REAL del propietario
ni usar sus fondos. Evidencia:
`~/.local/state/orbis/verification/v107-perp-live-review-r2/verification.json`.

## Trabajo pendiente para afirmar finalización

v108 incorpora el [núcleo interno de entrada perpetua](GRVT_PERP_ENTRY_KERNEL.md):
capital REAL separado, libro Decimal, preflight por instrumento, registro v2,
firma/envío protegidos, fills/funding/costes, recuperación del residuo y lectura
fría. Tanda completa final 4.581 casos / 391 archivos y ocho recorridos MAINNET.
El plan con datos públicos reales usa capital sintético; las cuentas/órdenes
se comprueban con proveedores propios. No se confunde esa evidencia con uso de
fondos del propietario ni con activación del nuevo bot desde la interfaz.

La [revisión de configuración perpetua](PERP_REAL_REVIEW.md) de v107 añade una
comparación funcional de originales con GRVT, sin cerrar el pendiente de
ejecución: no concede permisos, no copia el capital PAPER y no transforma una
estimación tras GAP en evidencia observada. El contrato de presupuesto/autoridad
y su recorrido de ejecución, parada y conciliación completos quedan conectados
y verificados en v110 con proveedores controlados. Su piloto humano mainnet
continúa pendiente.

1. Completar la auditoría de las funciones parciales de la tabla con evidencia
   por requisito. Revisar especialmente la UI completa, rendimiento, derivados
   históricos y las ampliaciones de DCA/cartera. Su primer uso mínimo ya está
   comprobado desde una instalación vacía; no se mantiene como tarea pendiente.
2. Completar el [modelo histórico de derivados](DERIVADOS_HISTORICOS_PLAN.md): margen, financiación,
   liquidación y las órdenes/posiciones avanzadas requeridas por la especificación.
   La procedencia fuera de muestra ya se vincula a MEXC (v90), GRVT (v91) y DEX
   (v92), repitiendo originales antes de nueva autoridad. Los registros antiguos
   permanecen legados; las puertas nuevas no convierten teoría en una garantía
   de rentabilidad ni en un modelo de ejecución DEX o margen fiel al exchange.
3. Continuidad DEX: conciliación histórica con índices ausentes/empates ya contabilizados,
   llamadas externas exitosas desconocidas y medición con linajes largos.
   Los formatos financieros publicados permanecen congelados.
   El [núcleo complementario de orden](DEX_HISTORICAL_RECONCILIATION_PLAN.md)
   ya conecta originales, pruebas, gas, aceptación y cálculo con la interfaz
   cargada en v112 y ampliada con el asistente de orden de v113.
   [Evidencia y límites actuales](DEX_GAS_ORDER_EXTENSION.md).
   La revisión de wallets nuevas simultáneas queda conectada en v114.
   v115 añade recuperación de efectos externos para las fuentes DEX verificadas.
   Quedan otros adaptadores de nonce y rendimiento ampliado.
4. Piloto humano mainnet con los permisos, activo/red y límites que el
   propietario configure. Las pruebas propias no autorizan a usar fondos ni
   claves reales y no sustituyen este piloto.

No se asigna un porcentaje global a esta tabla: la aceptación se determina por
resultados verificables y por las brechas explícitas, conservando el alcance
original y las ampliaciones pedidas.

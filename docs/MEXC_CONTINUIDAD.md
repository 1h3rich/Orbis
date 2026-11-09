# Continuidad MEXC: evidencia causal antes de renovar autoridad

## Estado

`CONTINUOUS_V1` implementado en API, trabajador y panel «Bots · REAL», con
pruebas de exchange controlado. Es un opt-in separado: fecha final explícita,
máximo 30 días y nunca posterior al borrador de límites aprobado. Renueva
evidencia diaria, no duración, presupuesto ni alcance de la autorización.
`OPERATIONAL_V1` mantiene su caducidad original, como máximo 24 h, sin renovarla.
El ensayo humano mainnet sigue pendiente; no se declara un servicio 24/7.

La revisión no recibe un cliente Trade, no envía órdenes, no asienta dinero,
no levanta STOP ni cambia ninguna política. Siempre conserva
`authorizes_renewal=false`, `orders_enabled=false` y
`PARITY_OBSERVED_NOT_RECONCILED`. Una lectura histórica no es una autorización.

## Contrato del núcleo

`app/live_execution/mexc_continuity_review.py` ofrece:

- `record_continuity_review`: recibe bóveda, View, política ya activa, UUID
  de revisión y guarda de autorización. Obtiene dos barridos completos,
  verifica su coherencia y guarda el resultado en una transacción cifrada.
- `read_continuity_review`: reproduce la revisión contra el prefijo del libro
  comprometido en ella, con una instantánea de lectura SQLite explícita.
- `verify_continuity_review`: variante componible que utiliza la transacción
  del llamante; también se ejecuta al verificar copias de seguridad.

Repetir un UUID devuelve la misma evidencia y fecha, sin nuevas consultas. No
la convierte en evidencia fresca. Reutilizarlo para otra política se rechaza.
Una revisión nueva de una política caducada se rechaza antes de consultar View.

## Frontera temporal y concurrencia

La frontera causal es el **inicio del primer barrido**, anterior a sus
consultas. No es la fecha en que termina de guardarse el informe. Las ventanas
de ejecuciones y traslados deben cubrir desde la apertura previa hasta esa
frontera. La política siguiente vuelve a comprobar desde esa frontera:
usar la hora final descartaría el intervalo durante el que se consultó MEXC.

Cada barrido reutiliza el verificador de ejecución existente: identidad View,
saldo y órdenes, instrumento, ventana de fills propios asentados, traslados,
inventario y riesgo del libro. La revisión conserva un recibo saneado de esas
comprobaciones. Las marcas privadas deben ser posteriores al inicio de su
propio barrido; no basta reutilizar una lectura aún menor de 60 segundos.
Los extremos de las ventanas no pueden estar después de su observación.

La guarda de generación protege el trabajo local mientras se lee View. Antes
del commit se vuelven a cotejar autorización, STOP, política, huella de apertura
y revisión del libro. Un cambio o fallo deja la revisión sin guardar. La
guarda de autorización se comprueba también inmediatamente antes del commit.
No se repara un hueco ni se reconstruyen compras supuestamente observadas.

SQLCipher reutiliza una conexión por hebra: abrir un segundo `BEGIN` dentro
de un commit ya abierto puede revertirlo o fallar. Por ello el auditor de
fills acepta la sesión del llamante y no la confirma, cierra ni revierte.
El coordinador conserva la propiedad de su transacción.

## Persistencia, privacidad y precisión

Se reutiliza `real_mexc_parity_observations`, añadiendo el objeto opcional
`causal` con protocolo `MEXC_CAUSAL_REVIEW_V1`. Se conserva la semántica original
de paridad no conciliada; no se crea una tabla de permisos encubierta. El
esquema continúa en v31, sin migración ni reescritura de registros anteriores.

Se fijan política, apertura anterior y sus huellas, frontera, revisión del
libro y los dos recibos. Sólo se conservan campos reconocidos de saldos,
órdenes, fills, ventanas y riesgo. No se guardan UID, claves, URLs firmadas,
direcciones de usuario ni columnas desconocidas del proveedor. Los contratos
públicos de token y las referencias locales saneadas de traslados mantienen
el contrato de privacidad ya existente.

Importes, comisiones, coste, unidades y P/L permanecen en `Decimal`, serializado
como texto. El riesgo se reproduce con los eventos conocidos en la frontera.
Cambiar de día UTC recalcula el flujo diario; no pone a cero el coste ni las
unidades de una posición. No modifica asignaciones totales o mensuales DCA.

Una aportación manual posterior puede convivir con la revisión histórica,
pero impide otra revisión causal automática de esa apertura. Un consumidor
debe cotejar la revisión **actual completa** del libro, no confundir que una
prueba histórica siga íntegra con que describa el saldo actual.

Las copias reproducen prefijos, riesgo, inventario, cobertura y correspondencia
de fills; rechazan pruebas modificadas incluso si se recalcula su digest local.
Las huellas detectan incoherencias, no a un atacante capaz de reescribir todo
el archivo cifrado y su historial.

## Alcance de la evidencia externa

Continúa el contrato conservador de un par y uso exclusivo de la cuenta. La
consulta de órdenes sin símbolo conserva `UNFILTERED_COVERAGE_UNPROVEN`: no
demuestra cobertura universal. El histórico de fills consultado es de un
símbolo; no demuestra ausencia de operaciones intermedias en otros pares.
Una cuenta operada simultáneamente por otra aplicación no queda protegida
frente a todas las carreras externas.

No se deduce causalidad sólo porque dos saldos coincidan: una compra/venta
ajena en el par, aunque deje saldo neto igual, debe aparecer en la ventana
completa y tener archivo, orden y asiento propios. Datos parciales, faltantes,
caducados o sin red no equivalen a una lista vacía. La revisión tampoco
demuestra el coste histórico inicial declarado manualmente por el usuario.

Contratos oficiales contrastados durante la implementación:
[ejecuciones por símbolo](https://www.mexc.com/api-docs/spot-v3/spot-account-trade/account-trade-list),
[depósitos](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/deposit-historysupporting-network)
y [retiradas](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/withdraw-history-supporting-network).
Se mantienen los adaptadores y límites de paginación existentes; no se añade
ningún endpoint privado ni se consulta una cuenta del usuario en las pruebas.

## Concesión continua y renovación efectiva

Seleccionar «Permiso → Continuo · con caducidad» muestra «Hasta». La activación
requiere `ACTIVAR_BOT_CONTINUO`; el rearme humano, `REANUDAR_BOT_CONTINUO`.
`continuous_until` debe incluir zona horaria. Fuera de ese modo se rechaza la
fecha; una confirmación de Sesión u Operativo no concede continuidad.

La concesión fija ID, política raíz y borrador con sus huellas, condiciones
financieras, fecha de aprobación/final y alcance exacto: cuenta, bot, versión,
origen probado, presupuesto de entrada, asignaciones y referencia inicial DCA,
inicio DCA y huella del inventario ajeno. El trabajador compara ese descriptor
con el que conserva en memoria. El token HTTP no es autoridad de renovación.

Dentro de las últimas 12 horas de una política, antes de planificar otra
orden, `spot_bot_continuity.renew_bot_policy` obtiene una revisión causal y
prepara una política hija inmutable. Su expiración es el mínimo entre la
fecha final de la concesión y **frontera causal + 24 horas**. No se prolongan
políticas expiradas ni se refresca un informe repetido; las huellas y el
historial anteriores se conservan.

El commit único revalida claim/generación, autorización, STOP, cuenta, libro
completo, inventario propio, saldo libre y ausencia de pedidos/retiradas
pendientes. Registra política, enlace causal, autorización subordinada y
semilla de mercado derivada del prefijo anterior, sin fabricar otra observación.
La copia cifrada reproduce también esa ascendencia de mercado y de aperturas.
Una revisión puede quedar archivada sin consumir si falla el paso siguiente;
por sí sola conserva `authorizes_renewal=false` y nunca concede autoridad.

Se mantienen cursor, posiciones, coste, P/L, gastos totales/mensuales DCA,
referencia, activaciones consumidas y última decisión. Los límites diarios de
riesgo se calculan desde el libro del día UTC, no desde el número de políticas.
La identidad de salida LONG depende de concesión y revisión de fills, no de
la política diaria: renovarla no proporciona un ID nuevo para reintentar una
venta. Las claves de compra/activación DCA tampoco cambian.

El trabajador actualiza política/evidencia esperada, pero **no su deadline
monotónico ni la fecha UTC de la concesión**. Reautenticar, sondear, proteger
la vista o repetir una petición no los amplía. Puede continuar con PAPER
cerrado y vista protegida: conserva clave y clientes únicamente en memoria.
La bóveda no está criptográficamente cerrada mientras opera.

Datos incompletos, huecos, lecturas ajenas, suspensión, error o pedidos
inciertos mantienen las puertas conservadoras de pausa/revisión. LONG con
posición no renueva si perdió más de 30 segundos de vigilancia de precio;
DCA BUY-only mantiene sus reglas de continuidad diaria, sin heredar ese stop
LONG. Ni una renovación ni una nueva lectura levantan STOP. Cerrar/reiniciar
ORBIS revoca autoridad y descarta claves: no hay autoarranque. La caducidad
con vista protegida cierra la clave y pausa la **última política hija**.

## Verificación y límites restantes

API y Chromium atraviesan más de 24 horas de reloj controlado, renuevan bajo
la misma concesión/hebra, compran/venden y prueban STOP o caducidad con vista
protegida. DCA renueva después de gastar y conserva presupuesto y disparadores.
Se comprueban consentimiento, fechas, repetición, rearme, pérdida de evidencia,
alteraciones rehasheadas, transferencia visible tarde y STOP durante la revisión.
No son ensayos de rentabilidad ni de ejecución mainnet.

El ensayo independiente `python -m tests.continuous_horizon_smoke` configura
una concesión de 30 días, observa hasta el último semidía anterior al vencimiento
y verifica 57 renovaciones, presupuesto conservado, una sola hebra y copia
cifrada completa. Los controles de caducidad se prueban por separado en API y
navegador. El transporte ficticio respeta las ventanas temporales solicitadas.
En la primera ejecución de referencia el ciclo máximo alcanzaba 15,78 s.
Tras acotar la validación común de mercado, el mismo horizonte conserva las
57 renovaciones con máximo 4,18 s y copia verificada en 9,08 s. Son mediciones
locales de proveedor ficticio: **no demuestran una cadencia sostenida de cinco
segundos ni disponibilidad mainnet**. El intervalo del trabajador se suma al
tiempo de cálculo/red. Los antecesores causales siguen reproduciéndose y una
copia larga aún puede tardar; esta mejora no elimina todos los costes futuros.

## Lectura de mercado coherente y validación común acotada

El perfil de seis días detectaba que cada antecesor de mercado validaba otra
vez el historial completo de permisos del mismo bot. Para 3 y 8 renovaciones,
una consulta de estado consumía 240 y 830 SELECT respectivamente. No era
principalmente el cifrado ni el cálculo de indicadores.

`read_market_stream` reutiliza la configuración ya validada del mismo bot
**sólo dentro de una invocación recursiva**. Abre una instantánea SQLite real
si el llamante no tiene ya una transacción. No abre otro BEGIN, ni confirma o
revierte una escritura del coordinador. Los ancestros siguen comprobando su
semilla, definición, velas, prefijo, huellas y vínculo de renovación.

El resultado no se guarda en la Session, la bóveda, el trabajador ni un caché
global; una nueva llamada vuelve a validar incluso dentro de la misma
transacción. Compartir la definición histórica validada no sustituye una
comprobación de autoridad de ejecución: este lector no autoriza órdenes ni
cachea STOP, saldos, precios o respuestas View/Trade. Las consultas de estado
del ensayo bajan a 151 y 336 SELECT, sin cambiar permiso, posición o presupuesto.

Las regresiones cubren crecimiento del trabajo SQL por el endpoint real,
alteraciones rehasheadas tras una lectura válida, escrituras propias sin
confirmar y cambios de términos en otra llamada de la misma transacción.
Otra conexión independiente no puede confirmar una escritura mientras existe
la instantánea; tras cerrarla puede hacerlo y la siguiente petición observa
el cambio. Esto se prueba con dos conexiones SQLCipher reales, no con mocks.
El ensayo admite `--half-days 12` para un perfil corto, manteniendo por defecto
el horizonte completo. Los perfiles se ejecutan sólo contra bases y clientes
ficticios; nunca contra la bóveda del usuario.

No hay custodia de claves tras reinicio, garantía de disponibilidad o vigilancia
con el equipo dormido. Sigue pendiente un ensayo humano mainnet autorizado,
incluidas comisiones, residuos y condiciones reales de la cuenta. El contrato
de uso exclusivo/un par y las limitaciones de cobertura externa anteriores
siguen vigentes. No se amplían permisos de ingreso, retirada, DEX o GRVT.

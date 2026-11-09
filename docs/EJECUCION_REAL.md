# Ejecución real: MEXC, GRVT, wallet y Uniswap

**Estado:** operativa manual y bot nativo MEXC Spot con autorización expresa,
implementados y verificados con un exchange controlado; pendiente de ensayo
con credenciales reales. El rearme revisado conserva la posición propia.
Se incluye [DCA REAL sólo compras](BOT_MEXC_SPOT.md#dca-real-sólo-compras):
backtest fijado, observación PAPER íntegra y concesión REAL total/mensual/por
orden separada, sin ventas, stops ni servicio 24/7. Comparte transporte y libro
con LONG; no usa sus reglas de salida ni convierte fondos ficticios en reales.
El [ingreso USDT/BEP-20](WALLET_FUNDING_BNB.md) dispone de plan cifrado y
solicitud de envío confirmada en wallet, cotejo de recibo observado/abono View
y asiento explícito, probados con transporte controlado.
La [retirada puntual USDT/BSC](MEXC_WITHDRAWAL_BNB.md) tiene plan y permiso
separado de un solo intento, recuperación View, recepción RPC observada y
asiento explícito de neto/comisión. Mantiene STOP incluso al contabilizar;
no reenvía tras timeout. Verificada con proveedores controlados, no mainnet.
El RPC de la wallet no es una prueba independiente de consenso. La
reasignación tras intervenciones manuales sigue pendiente.
El ciclo conjunto wallet→ingreso MEXC→bot BUY/SELL→retirada a la misma wallet
está verificado por API y Chromium sobre una única bóveda y saldos ficticios
compartidos, incluyendo respuesta perdida y reapertura sin reenvíos. Véase el
[recorrido operativo](BOT_MEXC_SPOT.md#wallet--bot--wallet). No equivale al ensayo
humano mainnet ni a una liquidación automática de todos los activos.
Primera red elegida por el usuario el 30-10-2026: **BNB Chain (BEP-20)** para
el recorrido wallet↔MEXC, tomando USDT como activo inicial de la pregunta.
Cada ingreso exige verificar la red y contrato publicados por MEXC, el activo,
la wallet y el gas; no supone disponibilidad permanente de BSC. El cruce de
recibos Ethereum existente no se presenta como soporte de acreditación BSC.
Existe un guardián puro, un journal cifrado
interno para intenciones MEXC Spot, reservas locales idempotentes, parada
persistente y un transporte interno de un solo intento para órdenes Spot LIMIT.
Un puente interno puede revalidar una señal de entrada MEXC Spot PAPER y crear
una intención con tamaño y precio REAL independientes. También puede solicitar
una reserva cifrada mientras mantiene bloqueada la sesión PAPER; la bóveda
v17 archiva su procedencia y evidencia junto a la intención. Las reservas
nuevas fijan además una huella de toda la observación PAPER. El transporte
interno exige la misma huella antes de la orden de prueba y del único POST,
manteniendo estable la sesión durante ese intento. Un origen anterior sin
esa huella se puede leer, pero no transmitir. La ruta manual exige activar
una política nueva ligada a una apertura de saldo revisada, y confirmar cada
pedido. Las claves View y Trade permanecen en memoria. La conexión no inicia
un bot: éste requiere su propia autorización temporal. Nunca se ha verificado con
una orden real. Véase el [recorrido manual MEXC](MEXC_SPOT_MANUAL.md).

El [núcleo de bot nativo Spot](BOT_MEXC_SPOT.md) dispone ya de activación
interna revisada y un ciclo de decisión→pedido durable→envío único→fill→
posición propia REAL. La bóveda v25 separa configuración, cursor y decisiones;
las magnitudes PAPER no se convierten en capital ni fills reales. Stop,
objetivo, regla, tiempo y stop móvil usan la posición propia. Cuenta ya con API,
trabajador y panel «Bots · REAL»: autorización explícita, estado/diario pasivos,
presupuesto y parada. Bloquear, caducar el permiso, cambiar claves o cerrar el servidor
revoca el permiso sin liquidar posiciones ni reanudar al abrir. «Reanudar»
revisa otra apertura, política y origen PAPER actual, conserva los fills de
permisos anteriores y registra el hueco sin vigilancia. Pedidos pendientes
o movimientos ajenos con exposición bloquean esa revisión. No se ha arrancado
ningún bot con credenciales o fondos del usuario.
Las activaciones nuevas disponen de [mercado propio REAL](BOT_MEXC_SPOT.md#mercado-propio-real-y-preparación-de-ejecución-continua):
semilla aprobada, cierres públicos contiguos y archivo cifrado v31 enlazado a
las decisiones. Ya no dependen de que PAPER publique otra vela para operar;
sus permisos y controles de bloqueo siguen siendo temporales. El opt-in
[Operativo V1](BOT_MEXC_SPOT.md#permiso-operativo-vista-protegida-política-con-caducidad)
permite proteger la vista y cerrar PAPER conservando clave y clientes operativos
sólo en memoria, hasta la caducidad de la política revisada (máximo 24 h).
No renueva aperturas ni extiende límites. El opt-in diferente **Continuo V1**
fija fecha explícita (máximo 30 días, dentro del borrador revisado), cuenta,
origen y límites. Consume la [revisión causal de cuenta](MEXC_CONTINUIDAD.md):
dos barridos, frontera anterior a las consultas y archivo cifrado reproducible,
para crear políticas diarias subordinadas sin ampliar la concesión ni
reiniciar posición, coste, presupuesto o activaciones DCA. STOP/revocación
ganan a la renovación; Bloquear/Detener y bloquear revoca todo. La pantalla
muestra fecha y renovaciones. No autoarranca al reiniciar ni garantiza 24/7.
Los recorridos de más de un día, compra/venta y caducidad/parada se verifican
con exchange controlado; el ensayo humano mainnet sigue pendiente.

La recuperación manual permite liberar selectivamente una reserva antes de
la primera marca de envío, sin reutilizar el ID ni liberar pedidos inciertos.
La cuenta se detiene aunque no tenga órdenes. Reabrir exige una apertura
posterior a STOP y ninguna intención pendiente: la revisión mantiene la
parada activa hasta guardar una política nueva y levantar STOP juntos.
Las políticas anteriores no reviven; una nueva parada durante la revisión
gana sobre la reapertura. Estas acciones no ejecutan órdenes ni retiradas.

La acción **Sincronizar orden** une el seguimiento de una intención propia
ya enviada: consulta orden y fills con View, archiva la observación, asienta
las ejecuciones finales una sola vez y reconfirma orden, fills e inventario
antes de cerrar la reserva. Exige la cuenta REAL vinculada y el UID conocido.
Si la orden sigue abierta, sólo actualiza su observación. Si falla el saldo
después del asiento, conserva éste y la reserva; repetir la acción continúa
sin duplicar compras o ventas. Los pasos manuales siguen disponibles en un
desplegable. La ruta protegida es
`POST /real/execution/mexc/intents/{intent_id}/synchronize`, con `account_id`.
La coincidencia de inventario es puntual y usa el mismo saldo y transacción
que el cierre; no constituye conciliación global ni activa órdenes nuevas.
La recuperación de un cierre anterior devuelve explícitamente que no se
consultó el exchange ni se volvió a comprobar el saldo.

Verificación de este recorrido: compra y venta con P/L, comisiones en base o
quote, ejecución parcial, orden cancelada sin fills, interrupción después del
asiento, discrepancia de saldo mínima, otro activo no registrado, identidad
cambiante, origen HTTP y reapertura de la bóveda. Pruebas de navegador del
recorrido conjunto y de los pasos manuales con MEXC controlado, sin fondos ni
credenciales reales. Las lecturas mantienen los contratos oficiales de
[consulta de orden](https://www.mexc.com/api-docs/spot-v3/spot-account-trade/query-order)
y [ejecuciones](https://www.mexc.com/api-docs/spot-v3/spot-account-trade/account-trade-list).

La pantalla REAL sí permite preparar un borrador inmutable de política MEXC
Spot LONG con límites de orden, volumen, exposición, pérdida diaria, costes,
antigüedad de datos y caducidad. Exige una versión LAB, MEXC View conectada,
UID seudónimo previamente verificado y vínculo local de la cuenta REAL. Se
guarda cifrado con `enabled=false`; crear o consultar estos borradores no
autoriza señales, Trade ni órdenes. Su activación manual conserva el borrador
y crea otra política con el mismo límite y una apertura nueva. Exige igualdad
actual de inventario, dos lecturas estables, identidad View/Trade, cero órdenes
abiertas y actividad desde la apertura explicada por fills propios asentados.
La sesión se limita a un par y un máximo de 24 horas. Un cambio manual o un
traslado obliga a revisar la apertura; una orden pendiente impide otra nueva.
Un borrador puede ensayar una compra límite con la clave Trade verificada:
se usa sólo `POST /api/v3/order/test`, que MEXC documenta sin envío al motor
de casación. La bóveda v22 reclama y cifra el intento antes de abrir red,
archiva aceptación o falta de confirmación y nunca reenvía el mismo ID tras
una interrupción. El historial se consulta desde el borrador; no autoriza
ni contabiliza órdenes negociables.
Un libro REAL con movimientos previos puede ahora vincularse explícitamente a
la conexión MEXC View observada sin rehacerlo como inventario inicial. El
servidor consulta UID, saldos y UID de nuevo, reclama una huella seudónima en
una transacción cifrada y rechaza asignaciones cruzadas o rotaciones de clave
no revisadas. El vínculo no asienta depósitos, compras o ventas, no coteja
cantidades y no demuestra titularidad ni saldo disponible. La UI solo ofrece
crear borradores cuando esa identidad local está confirmada.
La comprobación read-only de paridad MEXC toma dos fotografías de saldos con
UID igual antes, entre y después. Liga ambas a una misma revisión de todos
los eventos del libro REAL y bloquea si cambian, si las unidades difieren,
hay efectivo negativo o fondos bloqueados. El resultado favorable se llama
`PARITY_OBSERVED_NOT_RECONCILED`: es una condición necesaria para una futura
puerta de ejecución, **no** prueba ausencia de órdenes/fills/traslados en
tránsito ni activa Trade. La bóveda REAL v20 puede guardar una observación
estable sin bloqueos y mostrar su vigencia respecto al libro actual. Es
evidencia diagnóstica cifrada, **no** un certificado de conciliación. Su
huella SHA-256 detecta alteraciones accidentales del registro; no equivale a
una firma externa del exchange.
La lista de intenciones presenta versión, dataset, intervalo y hora de esa
señal sólo si la procedencia local supera su comprobación de integridad; no
confunde esta marca con una ejecución externa.
La observación y conciliación MEXC requieren aún intervención explícita.
La auditoría read-only de traslados recientes compara UID View antes y después
de consultar ingresos/retiros, exige una ventana completa y reciente y marca
cualquier movimiento como pendiente de conciliación causal. Una ventana vacía
no demuestra el pasado completo ni se usa para habilitar Trade. La consulta
está disponible como diagnóstico protegido en la política MEXC de la pantalla
REAL, separada de la auditoría por par de fills.
Una correspondencia concreta entre referencia seudónima de traslado final
MEXC y asiento pareado REAL puede confirmarse manualmente y persistirse en la
bóveda v23. El vínculo es uno a uno y reabrible, pero no demuestra titularidad
de la wallet, ausencia de otros movimientos ni recepción de fondos; nunca
construye un estado `reconciled=True` por sí solo. Los retiros con comisión no
se vinculan hasta definir su contabilidad exacta.
Para ingresos EVM, Cartera REAL puede buscar un `txId` introducido por el
usuario entre las capturas MEXC **con UID View verificado**. Calcula en memoria
la misma referencia HMAC seudónima archivada para el depósito y no guarda ni
devuelve el hash completo. Distingue no encontrado, pendiente, final y
capturas contradictorias. «No encontrado» sólo habla de las ventanas
verificadas disponibles; una coincidencia prueba que MEXC registró ese
identificador, **no** que la wallet seleccionada enviara la transacción ni
que la cadena, el saldo o el libro REAL estén conciliados. No habilita Trade.
MEXC Spot y GRVT mainnet tienen observación privada
puntual de saldos y posiciones accesibles, en solo lectura. MEXC admite ahora
un inventario inicial manual revisado: una cuenta REAL vacía, costes
declarados para todos los activos no monetarios, nueva lectura e identidad
antes de guardar todos los asientos en una transacción cifrada. No importa
compras históricas ni produce un estado reconciliado. Esa misma transacción
asigna la referencia local MEXC al libro y reclama una huella seudónima del
UID observado por View para impedir duplicar patrimonio en otra cuenta REAL;
una rotación de clave requiere revisión explícita, no reapertura automática.
No demuestra titularidad ni permisos Trade. La comparación puntual de saldos
MEXC vuelve a leer el UID antes y después del saldo. Si cambia durante la
consulta o contradice una huella local, se bloquea; un vínculo antiguo sin
UID guardado se etiqueta como no verificado. Incluso con UID coincidente,
las cantidades son sólo diagnóstico, no conciliación patrimonial. LAB y PAPER
usan fondos ficticios. REAL añade la operativa manual y el bot MEXC descritos
al inicio; los traslados USDT/BSC wallet↔MEXC están implementados y ensayados con
proveedores controlados. El ensayo humano mainnet y las otras vías siguen
pendientes. Este documento recoge
la aclaración del usuario del 13-10-2026: quiere estrategias **automáticas con
límites y parada**, financiadas desde una wallet **o con saldo ya existente en
MEXC o GRVT**, ejecutables allí o en un DEX importante, con retorno y
conciliación de datos y activos. Alcance deliberadamente pequeño: **MEXC
Spot**, **GRVT** y **Uniswap en una red EVM compatible** como candidato DEX.
GRVT se tratará como venue separado, de autocustodia y con cuentas de
financiación/trading; sus instrumentos derivados no se equiparan a spot.
Cada mercado, red, token y permiso se validará antes de habilitar capital real.

GRVT puede consultar órdenes abiertas, una orden por ID cliente y los fills
correspondientes desde su creación. La búsqueda de fills drena cursores dentro
de un límite explícito y falla si no puede completar la consulta; compara
cantidades por instrumento/lado. La evidencia saneada se puede archivar en la
bóveda cifrada v13 y reabrir sin conexión, pero ni esa comparación ni un estado
`FILLED` acreditan por sí solos saldo, financiación, liquidación o ausencia de
órdenes en tránsito. El archivo no alimenta una puerta de envío REAL.

Para GRVT existe además un prototipo **exclusivamente testnet**: firma EIP-712
con clave dedicada no persistida, diario cifrado de un solo intento, parada por
subcuenta y transporte IOC de una sola orden. Justo antes de preparar el envío,
consulta resumen privado, órdenes abiertas y reglas públicas de margen de
testnet. Sólo acepta una subcuenta simple cross/single-asset, sin órdenes
abiertas; las aperturas requieren ausencia de exposición previa y un colchón
de equity **y saldo en la divisa de liquidación** de 2 veces el nocional.
En `SINGLE_ASSET_MODE`, un saldo no nulo de otro activo bloquea la lectura:
[GRVT describe ese modo como de un solo activo de garantía](https://api-docs.grvt.io/schemas/api_sub_account_summary_response/).
Los cierres deben ser `reduce-only` y no
superar una posición única del mismo instrumento. Un intento sin conciliar
bloquea IDs posteriores de la subcuenta. Estas condiciones son deliberadamente
restrictivas y no reproducen liquidación, financiación ni el riesgo agregado
de GRVT. Antes de marcar el POST, el transporte repite la lectura privada de
cuenta y órdenes abiertas y la de reglas públicas; exige dos decisiones
permitidas, marcas temporales no regresivas e igualdad de saldos, exposición,
órdenes observadas y parámetros de margen. Un cambio bloquea sin crear
intención ni enviar. Las lecturas no son atómicas: [GRVT advierte que las
órdenes abiertas pueden omitir órdenes aún en tránsito](https://api-docs.grvt.io/trading_api/).
Un puente interno puede vincular una acción `OPEN`/`CLOSE` GRVT
`AUTO_PAPER` recién asentada a un ID testnet estable. Bajo bloqueo de la base
PAPER verifica asiento, posición, dirección, `reduce-only`, versión LAB,
dataset y huella; además reproduce **todas** las observaciones PAPER contra
las velas y reglas archivadas antes de delegar el envío. Una señal, explicación,
vela u observación alterada, o una versión de motor cuya semántica no se pueda
repetir, impide el intento. Vuelve a comprobar frescura durante el preflight. Tamaño y
precio testnet tienen límites independientes del capital ficticio. No hay
credenciales testnet configuradas ni comprobación en una cuenta viva. Hay una
ruta API local y confirmada para un único intento testnet ya firmado, pero no UI,
observador que invoque el puente automáticamente ni permiso para mainnet. Las
pruebas del puente usan transporte ficticio y no acreditan un fill externo.
Si un intento originado en PAPER llega al preflight, éste archiva en la bóveda
cifrada la sesión, acción, dirección, versión, dataset y huella, ligados al ID
cliente y al digest de la orden. El informe de recuperación y su tabla muestran
esa procedencia tras reiniciar; los intentos legados muestran origen ausente.
Un `PREPARED` que cayó antes del preflight aún puede carecer de procedencia.
El resumen local no demuestra que PAPER siga activo ni habilita otro envío.
La parada testnet ya puede reclamar una sola cancelación por ID cliente para
un intento previamente enviado o incierto. La bóveda v13 fija la marca antes
del POST; un timeout o caída prohíbe repetirlo y exige consulta por ID.
Se usa el TTL máximo documentado de 5 segundos para interceptar órdenes en
tránsito, pero ese límite temporal no garantiza que una orden futura no llegue.
Una respuesta `CANCELLED`, `FILLED` o `REJECTED` se archiva como observación:
no libera el intento ni demuestra saldo o P/L. No existe ruta UI/API de
cancelación, activación mainnet ni automatismo de cancelación con dinero real.
Una auditoría posterior de cancelación exige la marca persistida antes de
consultar la red y usa la misma observación para revisar orden, fills,
financiación, movimientos y cuenta. Si la orden terminó ejecutada pese a la
cancelación, registra `FILLED` y mantiene la parada; no presenta esa respuesta
como cancelación efectiva ni como saldo conciliado.
Si termina `CANCELLED` con ejecución parcial, el archivo conserva por separado
el tamaño original, el tamaño ejecutado y los fills que lo respaldan. El resto
no ejecutado de la orden no se confunde con una posición cerrada ni se libera
automáticamente como capital disponible.
El informe local de recuperación testnet recompone tras reinicio la parada,
el estado de cada intención, la cancelación única y si la observación actual
tiene una auditoría verificable. Una marca `AUDITED` se escribe sólo después
de completar esa auditoría y vuelve a requerir revisión si cambia la
observación. El informe es de solo lectura, no consulta GRVT y siempre deja
`can_submit=false`: sigue sin demostrar conciliación causal ni habilitar
operativa real.
Una preparación no reclamada puede abandonarse sin reutilizar su ID; después
del punto de POST incierto ya no. El lector testnet consulta fills de toda la
subcuenta desde el preflight archivado (o desde la creación de la orden en
intentos legados) y agota los cursores. Distingue los fills de la orden propia
de los de otras órdenes y archiva ambos sin confundir sus importes. Conserva importes
decimales de comisiones por divisa, comisión de builder, P/L realizado y
nocional bruto; rechaza un fill de liquidación inesperado. No convierte una
coincidencia en liquidación conciliada ni libera automáticamente el siguiente
envío. La auditoría lee además saldo, posiciones, márgenes y órdenes abiertas
posteriores; exige que el resumen no sea anterior al último fill conocido.
Archiva esa lectura y la compara con el preflight, pero las lecturas no son
atómicas ni atribuyen causalmente movimientos externos a la orden. También
consulta todos los pagos de financiación documentados para la subcuenta en
esa ventana, con cursores agotados, y guarda su importe firmado por divisa.
Consulta también transferencias internas de GRVT que afecten a la subcuenta;
archiva sólo ID, hora, divisa, cantidad y dirección relativa, nunca cuentas
completas, firmas ni metadatos. Un movimiento propio no se contabiliza como
beneficio. La variación de saldo ajustada por estos flujos conocidos sigue
siendo sólo diagnóstica. Lee asimismo retiros on-chain de la subcuenta,
incluidos los pendientes; guarda sólo ID, hora, divisa, cantidad y estado,
sin dirección de destino, firma ni hashes de cadena. Permite que un retiro
pendiente pase a finalizado, pero rechaza que desaparezca, cambie de importe
o retroceda a pendiente. El saldo posterior debe ser tan reciente como el
último retiro observado. Los depósitos on-chain se leen a nivel de cuenta
principal: el destinatario se compara en memoria con la sesión GRVT y se
archiva sólo una referencia seudónima derivada del hash L1, fecha, divisa,
importe y estado pendiente/confirmado. No se guardan direcciones ni hashes
brutos, ni se atribuye el depósito a la subcuenta o al P/L; dicha referencia
podría correlacionarse con datos públicos de la cadena y no debe presentarse
como anonimato. Sólo la transferencia interna observada aporta contexto
para el saldo de la subcuenta. Los pagos de interés horario MAM de la
subcuenta se leen y archivan por divisa con capital prestado y marcas de
tiempo, pero su signo contable queda sin verificar y no se resta ni suma
al P/L. Faltan otras clases de cargos, cobertura histórica y la transición
auditada para cerrar un intento enviado con ejecución.

Existe ahora una transición **interna de testnet** para el caso más estrecho:
una apertura que el exchange observó como `REJECTED` y `traded_size=0`.
Exige preflight archivado, dos auditorías posteriores separadas al menos
cinco segundos, lectura reciente de cuenta sin variación frente al preflight,
cero fills propios o externos y ausencia de financiación, transferencias,
retiros, depósitos e intereses en **todas** las auditorías guardadas del
intento. Sólo entonces archiva `REJECTED_CLOSED`; el ID original nunca se
reutiliza. La ruta local protegida y Cartera permiten solicitar este cierre
con confirmación, sin consultar GRVT ni enviar una orden nueva. Una
observación parcial, una cancelación o cualquier fill permanece bloqueado.
La [definición oficial de estados GRVT](https://api-docs.grvt.io/schemas/api_order_state_response/)
describe `REJECTED` como rechazo del motor antes de que la orden esté abierta;
esto no sustituye la validación en una cuenta testnet viva ni autoriza mainnet.
El cierre de órdenes que sí ejecutaron algo continúa pendiente.

El preflight MEXC añadido consulta reglas Spot públicas y comprueba una orden
LIMIT candidata sin redondearla. Un cliente separado sólo conoce el endpoint
firmado `/api/v3/order/test`; la UI de borradores lo usa con la clave Trade
introducida localmente. [MEXC documenta](https://www.mexc.io/api-docs/spot-v3/spot-account-trade/test-new-order)
que este endpoint no envía al motor de casación. No se interpretará su éxito
como fill, reserva de saldo en MEXC ni aprobación para usar `/api/v3/order`.
El transporte de POST real, conectado al coordinador de operativa manual, usa cuerpo
`key=value&…` firmado con `Content-Type: application/json`, igual que la
[guía Spot V3 vigente](https://www.mexc.io/api-docs/spot-v3/introduction).
MEXC advierte que `application/x-www-form-urlencoded` puede devolver
`700013`; las pruebas de contrato fijan encabezado, orden de parámetros y
firma. No se ha validado el POST con dinero real.

El journal tiene un punto de no-reintento `SUBMISSION_UNKNOWN` que debe
confirmarse en disco antes de un eventual envío. Mantiene la reserva incluso
si la app se reinicia o se activa la parada. Una consulta privada de lectura
puede observar una orden por ID cliente durante el plazo documentado por
[MEXC](https://www.mexc.io/api-docs/spot-v3/spot-account-trade/query-order),
pero una respuesta ausente o fallida no demuestra que el POST no se procesó.
La transición a ese punto exige de nuevo la intención exacta y un estado
reciente de cuenta/cotización. El transporte exige una segunda lectura
independiente posterior a `/order/test`: cuenta, cotización, reglas del par y
último precio deben tener marcas de observación posteriores a las iniciales.
Si la lectura falla, no avanza o sus nuevos valores invalidan la orden, no
hay POST real y la intención permanece reservada. Esta interfaz interna de
refresco **no demuestra por sí sola la procedencia** de la lectura: todavía
no existe un orquestador REAL que produzca una cuenta completamente conciliada.
La segunda puerta comprueba identidad, conciliación, parada,
vigencia de señal y política, saldo libre, exposición, pérdida diaria y
reservas de otras intenciones. Una reserva antigua por sí sola no autoriza
transmitir. Si esta segunda puerta falla, la intención sigue reservada y
no se marca como enviada; la parada puede liberar esa reserva inequívocamente
no enviada. En una venta, ambas puertas requieren además un coste medio
reconciliado por unidad base: calculan la pérdida proyectada al precio límite,
incluida la comisión prevista, y rechazan si alcanzaría el máximo diario. Sin
coste verificable se rechaza la venta; ese dato aún no lo produce el orquestador
REAL. El journal suma también pérdidas potenciales de ventas locales pendientes
en la misma divisa antes de reservar y antes de enviar. Rechaza una petición
archivada alterada o una venta de otra base cuyo coste no puede verificar;
no sustituye la conciliación de órdenes externas. Para SELL se reserva además
el equivalente base de la comisión estimada, y la pérdida usa el mayor coste
entre comisión en quote y base al coste medio. La cantidad de la orden externa
se contrasta con `quantity_base`, no con esta reserva ampliada. El volumen
diario pendiente se agrega entre políticas de la misma
cuenta y divisa de pago en ambas puertas; importes en divisas distintas no
se suman sin conversión verificable. Este control local no demuestra que MEXC mantenga el mismo saldo
después de la última observación. Después de `/order/test` y de refrescar las
lecturas, el transporte vuelve a comprobar referencia/UID View y permisos/UID
Trade antes de abrir el punto de envío. Si falla, retiene la reserva sin hacer
POST real; esta segunda comprobación tampoco garantiza que los permisos no
cambien inmediatamente después. El transporte interno hace un solo
`POST /api/v3/order` LIMIT después de verificar UID Trade/View, reglas públicas,
reserva, orden de prueba, segunda puerta de riesgo y una marca duradera anterior
a abrir red. Un timeout, respuesta no verificable o caída deja la reserva como
incierta: jamás se repite el POST automáticamente. La parada confirmada antes
del POST lo impide; si ya se envió, **no cancela por sí misma una orden abierta
en MEXC**. El transporte sólo se ensaya con HTTP ficticio y no está expuesto
por API/UI. Ya existe una lectura interna sin caché de saldo View, UID, reglas,
ticker y órdenes abiertas MEXC con procedencia y vigencia comprobadas. Hace
dos barridos y rechaza cambios de identidad, inventario, órdenes o parámetros
de negociación del instrumento entre ambos (ignorando sólo la hora de lectura),
así como una captura que caduque antes de terminar. El UID se relee al terminar
cada barrido y cada saldo debe cumplir total = libre + bloqueado sin redondeo.
No es una instantánea
atómica: dos lecturas iguales pueden ocultar movimientos intermedios y no
reconstruyen el P/L diario. Una auditoría interna adicional compara una
ventana paginada completa de fills de un par con el archivo cifrado, la orden
propia y el asiento REAL. Señala fills externos, alterados, ausentes o sin
asentar; un resultado consistente sólo corresponde a ese par y periodo, no
habilita ejecución ni acredita P/L. La API local protegida y Cartera REAL
pueden consultarla para una política registrada con View conectada; la
respuesta nunca concede permiso para enviar órdenes. Otra auditoría compara las órdenes del
par con el diario: una orden ajena, propia
sin archivo o de resultado incierto bloquea. Esta prueba **no** genera un estado
REAL autorizado: aún falta contrastar exposición de otros pares, P/L diario
y coste previo con un libro de riesgo reconciliado. Tampoco está conectada al
orquestador de envío. Éste todavía debe custodiar la clave Trade, vincular
señales, observar y conciliar resultados, y disponer de cancelación supervisada
antes de habilitar ejecución automática.

La reapertura de un asiento ya importado, la vista del ciclo y el cierre de la
reserva aplican el mismo contrato exacto fill→evento: cuenta vinculada, ID de
petición determinista, huella de carga, cantidad, precio, coste bruto,
comisión, hora y nota. Una alteración deja la vista en
`RECONCILIATION_INCONSISTENT` y bloquea el cierre; no se responde «ya
contabilizado» sólo por existir un vínculo. Es una comprobación local de
integridad, no una conciliación completa con MEXC.

La auditoría exige ahora que cada orden abierta coincida con un archivo de
fills no anterior a la observación de órdenes: mismo estado, ID externo y
cantidad ejecutada, con cantidad pendiente coherente. Un cambio a parcial,
un archivo anterior o IDs duplicados bloquean hasta volver a observar y
archivar. Esto afecta también a la sonda de otros pares, que continúa sin
certificar cobertura global.

Una sonda adicional puede consultar `/api/v3/openOrders` sin `symbol` con la
clave View y detectar órdenes ajenas o intenciones inciertas en otros pares.
Su resultado lleva siempre la etiqueta `UNFILTERED_COVERAGE_UNPROVEN`: la
[documentación Spot V3 de MEXC](https://mexcdevelop.github.io/apidocs/spot_v3_en/)
menciona la consulta sin símbolo pero marca `symbol` obligatorio y no garantiza
paginación ni cobertura exhaustiva. El escáner falla ante respuestas llenas,
malformadas, caducadas o duplicadas y **nunca** certifica la cuenta como libre
de órdenes. La consulta del par exacto continúa separada; ninguna de las dos
sondas construye por sí sola una puerta de riesgo para operar.

La Cartera REAL expone esta sonda como revisión puntual para un libro
vinculado: muestra cuántas órdenes aparecen por par, omite sus IDs y conserva
la advertencia de cobertura no probada incluso si devuelve cero. La bóveda
REAL v21 archiva la observación saneada e íntegra para inspección posterior;
la UI no expone IDs. Esto no altera la paridad guardada ni cambia permisos de
ejecución.
Dos capturas de la misma identidad pueden compararse sin conexión: ORBIS
detecta apariciones, desapariciones, progreso declarado e inconsistencias de
términos con decimales exactos. El informe mantiene
`TWO_UNFILTERED_SNAPSHOTS_UNPROVEN`; una desaparición exige consulta del estado
final y fills, nunca se convierte por sí sola en operación contable.

La evidencia View conserva ahora todos los saldos Spot observados. Un cálculo
interno de exposición valora la cuenta en la divisa de una política usando
precios bid recientes de pares directos y rechaza activos sin cotización
verificable. Cartera REAL ofrece además una vista puntual de valoración en
USDT/EUR: actualiza saldos, muestra el valor por activo y sólo publica un
total si todos tienen precio reciente; de otro modo indica la cobertura
faltante. El total incluye saldo libre y bloqueado, pero **no** es coste de
adquisición, P/L diario ni una cuenta reconciliada. Ninguna valoración
alimenta todavía el estado de autorización REAL.

Un borrador MEXC vinculado puede solicitar «Exposición MEXC»: dos barridos
View concordantes y precios bid recientes valoran todos los saldos Spot con
par directo frente a su divisa. La respuesta separa el activo base de los
demás activos y muestra el margen frente al límite de ese borrador. Si falta
el precio de cualquier activo, se rechaza el total en vez de omitirlo.
La consulta requiere bóveda desbloqueada, revalida la identidad y el borrador
al terminar, respeta «Ocultar importes» y mantiene
`reconciled_with_book=false` y `execution_ready=false`. No demuestra
historial completo, coste, P/L ni autoriza una orden.

Cada borrador MEXC puede consultar el riesgo **sólo del libro REAL local**:
volumen negociado del día UTC, P/L realizado, margen de volumen restante y
si alguno de esos dos límites locales se alcanzó. El botón «Riesgo local»
oculta importes cuando la cartera está velada. Aportaciones y traslados no
cuentan como volumen o beneficio. Este diagnóstico excluye pérdidas latentes,
fills no importados y exposición de otros pares; devuelve siempre
`execution_ready=false` y no modifica la política.
La misma respuesta muestra unidades y precio medio del par y etiqueta su
procedencia: coste manual, declarado o cadena local de fills MEXC vinculados.
Si no quedan unidades, el coste medio es ausente (`null`), no cero. Un
inventario positivo de coste declarado cero conserva el cero; la interfaz
distingue ambos estados sin revelar unidades cuando se ocultan importes.
Antes de calcular comprueba las huellas de todos los asientos de la cuenta.
Una cadena local exacta no demuestra que no existan operaciones externas
ausentes ni convierte por sí sola el coste en conciliación de venue.

La bóveda REAL v16 conserva la petición canónica exacta de las intenciones
MEXC nuevas. Tras reiniciar, un procedimiento interno reconstruye y contrasta
política, cuenta, precio, tamaño, comisión prevista y hora de señal antes de
que esa intención pueda usarse en una cancelación dirigida. Las intenciones
anteriores a v15 no tienen esta evidencia completa y quedan fuera de dicha
recuperación automática; nunca se infieren campos desde la huella.

Antes de reservar para un envío interno, la coincidencia del UID Trade/View
queda seudonimizada con una sal aleatoria en la bóveda cifrada v16. Si una
referencia de clave cambia de UID o una nueva referencia apunta al UID de otra
clave ya registrada, se rechaza sin llamada de prueba ni orden: rotar la clave
no puede reiniciar silenciosamente límites o parada. Es un bloqueo, **no** un
flujo de rotación: no traslada órdenes o reservas antiguas y las intenciones
anteriores a v16 no poseen esa evidencia. La futura recuperación deberá
comprobar UID, intenciones pendientes, parada y libro antes de reasignar.
El transporte de envío exige el cliente View concreto, comprueba que su
referencia coincide con la intención y obtiene el UID desde él; no acepta una
cadena UID de un llamador como prueba de identidad.
La consulta View de identidad ya puede detectar, sin escribir ni mostrar el
UID, si la clave conectada es la misma de un envío v16, una clave rotada con el
mismo UID o un UID distinto en la referencia conocida. Muestra la parada y las
intenciones pendientes de la referencia previa sin revelar ésta. Es una ayuda
para recuperación manual, no una migración o permiso para operar con la nueva
clave.

Ya existe también una cancelación **interna** de una orden LIMIT
individual mediante `DELETE /api/v3/order` y su ID cliente determinista,
conforme al [contrato Spot V3 de MEXC](https://mexcdevelop.github.io/apidocs/spot_v3_en/).
Sólo se intenta después de una parada persistida y una observación reciente
de esa orden abierta, se marca duraderamente antes de abrir red y no se repite
a ciegas. No se usa `DELETE /api/v3/openOrders`, que alcanzaría órdenes ajenas.
El acuse de cancelación **no libera la reserva**: puede haber fills parciales o
una carrera con la ejecución; siguen siendo obligatorias la reobservación y
conciliación final. La API local y Cartera REAL pueden iniciar explícitamente
el supervisor con View/Trade conectadas sobre una cuenta previamente detenida.

Un coordinador interno puede escoger una única intención MEXC de una cuenta
localmente detenida por llamada. Sólo considera la solicitud v15 exacta y una
orden propia abierta archivada hace menos de 30 segundos; la función de
cancelación vuelve a comprobar todo antes del DELETE. Una observación vieja,
un intento legado o un acuse anterior no producen otro envío. El bucle sólo
existe tras `VIGILAR_PARADA` por API o Cartera REAL, nunca automáticamente al conectar la
clave o al abrir ORBIS; no equivale a una parada garantizada por MEXC.
También exige el cliente View de la misma referencia, obtiene de él el UID y
lo contrasta con Trade. Para envíos v16, verifica además la huella del UID
archivada antes de reclamar el DELETE; una clave ajena o UID alterado no cambia
el diario ni cancela. Las intenciones anteriores a v16 no tienen esa huella,
pero conservan las demás puertas y requieren revisión especialmente cuidadosa.

Un segundo paso interno, sólo con la clave View vinculada a esa referencia de
cuenta, puede reconsultar una cancelación incierta o acusada y archivar la
orden junto con sus fills. Si aún figura abierta, devuelve ese estado y permite
otra observación posterior; si aparece final, deja pendiente el asiento de
los fills que existan y la reconfirmación de saldo antes de cerrar la reserva. Un error de
red, ausencia de orden o clave View ajena no se interpreta como cancelación.
No repite el DELETE ni utiliza este resultado para
habilitar una nueva orden. Un ciclo interno ya prioriza la reobservación de
cancelaciones inciertas, consulta una intención detenida sin observación final
y, sólo si confirma una orden propia abierta reciente, llama a la cancelación
dirigida una vez. Las órdenes con resultado incierto no vuelven a DELETE.
Una lectura idéntica refresca la hora de última confirmación del contenido
archivado; una respuesta antigua que retrocede tras una ejecución/final se
rechaza. Varias cancelaciones pendientes rotan por la observación más antigua.
Esto no es aún un proceso persistente en segundo plano: no se recupera ni
reinicia tras cerrar ORBIS. El recorrido completo se ha ensayado sólo con
View/Trade y transporte HTTP ficticios; no con una cuenta mainnet.
Existe un trabajador interno optativo que repite ese ciclo sólo tras arranque
explícito para una cuenta ya detenida. Se detiene cooperativamente, sale al
detectar la bóveda bloqueada **o reemplazada por otro desbloqueo**, no reintenta
automáticamente tras error y descarta
las referencias a los clientes con credenciales al terminar. La API protegida
puede arrancarlo únicamente tras una confirmación expresa para una intención
incierta de cuenta detenida, UID archivado, View coincidente y Trade verificada
de nuevo. **Puede cancelar una orden real propia en MEXC** si está abierta.
La UI de Cartera REAL exige confirmación para iniciar y detener la vigilancia;
no hay arranque al abrir ORBIS. Cerrar la aplicación interrumpe su vigilancia.
Cartera REAL y la barra global muestran el estado
del trabajador mientras la sesión está abierta; el sondeo no prolonga el
desbloqueo por inactividad. Un estado no verificable exige revisión directa
en MEXC, no presupone que la orden se canceló.
Al desbloquear tras reiniciar, ORBIS cuenta las intenciones inciertas de cuentas
detenidas y marca «PARADA SIN VIGILANCIA» si no hay trabajador local de esa
misma cuenta. Un trabajador de otra cuenta no oculta el aviso. Esta detección
no reconecta credenciales ni inicia automáticamente cancelaciones; requiere
revisión directa en MEXC y activación explícita si corresponde.
Cuando ya se solicitó detener el trabajador, el hilo puede seguir vivo hasta
terminar su llamada actual, pero ORBIS deja de contarlo como vigilancia futura
y mantiene el aviso para cualquier intención incierta.
Un ciclo que no progresa durante 90 s también deja de contarse como vigilancia;
al terminar fuera de plazo, el trabajador se detiene sin repetirlo y exige
revisión. El límite no corta una llamada en curso y no puede confirmar si
MEXC aceptó o ejecutó la cancelación antes de responder.
El propietario debe solicitar su parada antes de bloquear o desconectar y
esperar a que acabe cualquier llamada ya iniciada. Una llamada
del ciclo toma una guarda de sesión: el bloqueo espera a que termine ese tramo
de red, para no reutilizar credenciales de la sesión antigua en una nueva.
La parada
local no equivale a una orden protectora residente en MEXC.
La vista local de Cartera REAL señala si el intento quedó incierto, recibió
acuse sin reobservación, sigue abierto después o tiene un estado final
archivado. Estos rótulos no modifican la reserva ni prueban cancelación
efectiva o saldo disponible.
Tras la parada local, Cartera REAL puede consultar por ID cliente una intención
pendiente usando únicamente la clave MEXC View conectada. Archiva orden y
fills verificados, sin cancelar, asentar ni cerrar la reserva. Un error de red
o una clave de otra referencia/UID conserva el estado incierto; observar una
orden abierta tampoco significa que la parada externa esté completada.
Antes de archivar la observación, ORBIS vuelve a comprobar referencia y UID
View después de leer orden y fills. Si cambian o no pueden verificarse,
conserva la reserva y no publica el snapshot.

La observación actual también consulta los fills por `orderId`, exige que
cantidad y contravalor sumados coincidan exactamente con el snapshot de la
orden, y archiva fills y estado en la bóveda REAL v13 vinculados a la intención.
El archivo de fills y snapshot es atómico y exige progreso monotónico: no
acepta menos ejecuciones, una hora de observación anterior, fills previamente vistos omitidos ni un estado
distinto tras el final. Las comisiones quedan separadas por activo. Un snapshot externo no acredita
por sí solo el coste fiscal, no libera una reserva incierta y no crea un
movimiento de cartera: esas transiciones necesitan conciliación adicional.
La ruta local protegida `GET /real/execution/mexc/intents` expone una vista de
solo lectura del journal: distingue reserva sin envío, orden no verificable,
orden observada abierta y estado final observado aún sin contabilizar. Los
importes retenidos siguen siendo reservas **locales**, no saldo MEXC; la ruta
no consulta la red, no libera fondos ni importa operaciones a la cartera.
Para órdenes finales, esa vista proyecta con decimales exactos el cambio
observado en BTC/USDT y en cualquier activo de comisión, y comprueba que
todos los fills archivados concuerdan con el snapshot. Distingue orden
cancelada sin ejecución, archivo incompleto y comisión en un tercer activo
distinto de base y quote. Las comisiones en base o quote son representables;
una compra con comisión en base exige que queden unidades netas positivas.
Incluso en estos casos, la importación requiere
elegir explícitamente una cuenta REAL y confirmar `IMPORTAR` en la ruta local
protegida `POST /real/execution/mexc/intents/{id}/book`. Se exige orden final,
archivo completo, contravalor exacto, inventario para ventas y efectivo
registrado a la fecha de cada compra. Los fills se asientan juntos o ninguno,
con unicidad externa y reintento idempotente; la vista distingue `FINAL_BOOKED_RESERVED`
de `FINAL_OBSERVED_UNBOOKED`. En Cartera REAL, «Órdenes y conciliación REAL»
muestra el destino seleccionado, las variaciones observadas y las razones de
bloqueo; pide escribir `IMPORTAR` antes de asentar. Esta vinculación manual
**no prueba el UID** ni reconcilia el saldo actual del exchange. Las reservas
inciertas siguen retenidas. `fee_model_compatible` indica sólo si la comisión
puede representarse en el libro actual; `quote_fee_model_compatible` conserva
su significado anterior de comisión exclusivamente en quote. Ninguna significa que la operación esté importada
o lista para activación real. Si el contravalor informado por MEXC difiere de
precio × cantidad, el asiento conserva **ambos**: el precio comunicado y el
contravalor exacto del fill. Caja, coste y P/L usan este último; el CSV REAL
también lo preserva. No se sustituye por un precio unitario derivado que
pueda requerir infinitos decimales. Una comisión en base se conserva con su
activo en el evento REAL: reduce unidades netas en compras y consume inventario
adicional en ventas, sin fingir un gasto en quote. Las comisiones en un tercer
activo aún se bloquean sin inventar movimientos contables. El CSV REAL v2
incluye `fee_asset`; el importador sigue aceptando v1 como comisión en quote.
La bóveda v18 añade esta columna en una copia cifrada verificable, conservando
una preimagen recuperable.

Una reserva `SUBMISSION_UNKNOWN` puede cerrarse sólo con una acción explícita
`CERRAR` desde Cartera REAL o su ruta local protegida. La clave View conectada
necesita `SPOT_DEAL_READ` además de lectura de cuenta y
debe corresponder a la misma referencia; ORBIS vuelve a consultar la orden por
ID de cliente, todos sus fills por ID de orden y los saldos Spot. Contrasta
el UID View antes y después de esas lecturas y, para envíos con vínculo v16, su
huella cifrada archivada: otra clave o un UID cambiado no libera la reserva.
Las intenciones legadas sin vínculo v16 sólo pueden comprobar la referencia
de la clave y un UID válido, no demostrar retrospectivamente el UID original.
Después comprueba estado final, identidad, importes, fills idénticos, asiento de todos los fills
ejecutados, igualdad de cada asiento con cantidad, precio, contravalor,
comisión y hora del fill archivado, y saldo posterior reciente. Una diferencia
mantiene la reserva. Entonces, en una transacción cifrada,
guarda la evidencia mínima y pone las reservas locales a cero. Si la orden se
canceló sin fills, no se exige ni se crea un asiento. Una consulta fallida,
saldo antiguo o diferencia deja la reserva intacta; jamás autoriza reenvío.
Esto **no** verifica el patrimonio total ni el coste de activos anteriores:
la próxima señal real necesitaría su propia observación y puerta de riesgo.
Las dos comprobaciones de identidad acotan un cambio durante la consulta,
pero las lecturas no son una transacción atómica del exchange.
La consulta de orden por ID de cliente tiene cobertura temporal limitada en
MEXC. Si ya no puede reconfirmarse, la reserva permanece retenida; falta un
procedimiento manual auditado de recuperación, nunca un desbloqueo automático
basado sólo en una ausencia de respuesta.

La clave MEXC View puede consultar el UID privado por una ruta optativa;
ORBIS retiene ese UID sólo en memoria y comunica únicamente si se observó.
Una sonda GET-only inspecciona los permisos mínimos de Trade y compara su UID
con el UID View. La API local protegida permite conectarla sólo para ese
diagnóstico después de conectar View; la conserva en memoria hasta bloqueo,
desconexión, reemplazo de View o caducidad de la bóveda. El estado devuelve
`execution_enabled=false` para **órdenes nuevas** y
`verification_scope=AT_CONNECTION_ONLY`: permisos
o identidad podrían cambiar después, por lo que cualquier envío futuro tendrá
que repetir la comprobación. No expone UID ni claves. La ruta separada de
vigilancia puede llegar a `DELETE /api/v3/order` bajo las puertas descritas;
la nueva ruta de órdenes manuales vuelve a verificar cuenta y permisos bajo
la política activa y la apertura revisada. La UI local ofrece también la
verificación GET-only y retirada de la clave Trade, con formularios que se
vacían tras el envío. La comprobación es puntual, no una autorización
persistente ni una activación de operaciones.
La referencia archivada derivada de credenciales y el UID View por separado
no prueban que ambas claves sean de la misma cuenta. El aislamiento y
almacenamiento seguro de credenciales de trading siguen siendo puertas
pendientes.

La observación MEXC ya incluye una página reciente de fills por par, guardada
en la bóveda cifrada como observación separada del libro. No es una
importación contable completa: el endpoint oficial `myTrades` sólo cubre el
último mes y requiere consultar cada símbolo. Para saldos previos y coste de
adquisición seguirá haciendo falta importar historial contrastado. El coste
inicial declarado ya permite registrar unidades con trazabilidad local, pero
no valida el coste ni la cuenta completa. Según la [FAQ oficial de
MEXC](https://www.mexc.com/mexc-api), su API no ofrece sandbox: la puerta de
ensayo de órdenes deberá resolverse con adaptador ficticio, pruebas de
contrato y una autorización específica posterior para una operación mínima
real; nunca se presentará esa simulación como prueba del exchange.

## Flujo objetivo

```text
Origen A: wallet → ORBIS → DEX → wallet (sin depósito en CEX)
Origen B: wallet → depósito MEXC → ORBIS → órdenes MEXC → retiro a wallet
Origen C: saldo preexistente MEXC → ORBIS → órdenes MEXC → retiro a wallet
Origen D: saldo GRVT → ORBIS → órdenes GRVT → retiro/puente a wallet
En todos: fuente externa → recibos/órdenes/fills/saldos → conciliación ORBIS
```

No todo trade requiere trasladar fondos: en DEX se opera desde la wallet; en
MEXC se puede operar con saldo ya depositado, sin exigir un paso por la wallet.
El traslado wallet↔MEXC es un proceso
asíncrono distinto de la orden, nunca una transacción atómica. Una señal puede
caducar mientras se espera el depósito: se reevalúa antes de operar. Retirar
requiere otra política y permiso, no se deduce del permiso de trading. Si el
proveedor no permite automatizar una etapa de forma acotada y verificable,
ORBIS la marcará como pendiente de autorización humana y no fingirá ejecución
automática completa.
Como primer paso del origen B, una cuenta MEXC View vinculada puede consultar
temporalmente su destino de depósito ERC-20 en Ethereum. ORBIS exige una única
dirección sin memo y compara el contrato de la ficha MEXC con el contrato
observado en la wallet conectada. La dirección no se persiste, caduca en la
pantalla al minuto y debe comprobarse dentro de MEXC antes de mover fondos.
Este paso **no** firma, envía, contabiliza ni activa la estrategia.

La observación MEXC puede convertirse, tras revisión, en un inventario inicial
de una cuenta REAL vacía. Esto exige un coste por activo introducido por el
usuario; el exchange no lo aporta. Las operaciones anteriores todavía no se
importan con su procedencia. Un saldo preexistente **no se convierte en una
compra nueva** ni genera beneficio ficticio. Si no se conoce el coste, no se
debe introducir uno inventado: deja el activo sin importar hasta contar con
historial o documentación. Los depósitos y retiros entre ubicaciones propias son
transferencias, no rendimientos. La estrategia sólo podrá comprometer el
saldo libre asignado, respetando órdenes abiertas y reservas de otras reglas.
La comparación actual del libro con retiros MEXC exige `transferType=0`
(retiro externo). `transferType=1` es traslado interno MEXC y una fotografía
anterior sin ese dato queda con ruta desconocida: ninguno de los dos se
presenta como posible retorno a wallet. Incluso un retiro externo con importe
y hora coincidentes sigue siendo sólo un candidato, no prueba destino ni
recepción on-chain.
El [historial oficial de retiros MEXC](https://www.mexc.io/api-docs/spot-v3/wallet-endpoints/withdraw-history-supporting-network)
publica `amount` y `transactionFee` por separado, pero no define en esa
ficha si `amount` representa el débito bruto o lo recibido tras comisión.
ORBIS no debe vincular automáticamente un retiro con comisión a un traslado
REAL aplicando una resta supuesta. Hay que confirmar la semántica con evidencia
de saldo y recepción de la red, y registrar de forma explícita la comisión en
el activo correspondiente antes de considerarlo un vínculo verificable.
La comparación de traslados ahora busca, sólo como hipótesis, tanto
`amount = débito bruto` como `amount = recepción neta` cuando el libro REAL
registra una comisión idéntica en el activo enviado y conserva el par
origen/destino. Presenta cuál igualdad produjo el candidato, pero no ofrece
«Vincular» para esos casos. Una comisión distinta o ausente en el libro no se
presenta como coincidencia exacta. La ficha de historial no resuelve por sí
sola la semántica de `amount` ni la moneda real de `transactionFee`.
Las capturas nuevas de retiros ETH/ERC20 guardan por separado huellas HMAC del
`transHash`/`txId` EVM válido y de la dirección receptora sin memo, cuando
MEXC los publica. La wallet puede cotejar el recibo y una recepción nativa o
ERC-20 con esas huellas; no guarda hash/dirección completos ni infiere el
importe neto. Una referencia compartida por varios retiros o modificada en
capturas posteriores se marca contradictoria. Esta observación no crea
asientos, no prueba finalidad irreversible ni habilita trading o retiro.
Para un único evento recibido, ORBIS puede comprobar numéricamente si coincide
con `amount` o con `amount − transactionFee` (suponiendo la misma unidad y
`decimals()` disponible). El resultado sólo distingue hipótesis; no establece
qué representa `amount`, si la tasa se cargó en ese activo ni la identidad
histórica del contrato. Varios eventos dejan el importe ambiguo.
Cuando la consulta de [información de monedas de MEXC](https://www.mexc.io/api-docs/spot-v3/wallet-endpoints/query-the-currency-information)
devuelve un único contrato ETH con retiro habilitado, la captura conserva esa
dirección pública como ficha de ese momento. La UI distingue contrato del
evento coincidente o distinto; no extrapola la ficha al pasado ni la utiliza
como permiso de retirada.
Las consultas View nuevas convierten `txId` de depósitos e `id` de retiros en
una referencia HMAC dentro de la bóveda cifrada. El identificador bruto no se
devuelve por API ni se archiva. La misma referencia permite reconocer una
fila en dos instantáneas de la misma conexión local, incluso si cambia su
estado, pero no prueba unicidad del evento on-chain: una transacción puede
producir más de un abono. Si falta el identificador, la fila queda sin
referencia; al rotar la conexión, se pierde la continuidad seudónima. Las
fotografías anteriores siguen legibles y no adquieren identidad retroactiva.
En la UI REAL, un `txId` EVM introducido voluntariamente se consulta primero
mediante la extensión conectada: transacción, recibo exitoso y cuenta emisora
en la red seleccionada. Sólo después se cruza temporalmente con esas huellas
de depósitos MEXC. La respuesta separa coincidencia final, pendiente,
contradictoria o ausencia en capturas verificadas. No se archiva el hash bruto,
no se crea un asiento ni una autorización. Para Ethereum mainnet, cuando MEXC
entrega una dirección EVM sin memo, se archiva sólo una huella HMAC separada.
La UI compara con ella receptores de transferencias nativas simples o eventos
`Transfer` ERC-20 y, si hay un único candidato, coteja el importe exacto usando
`decimals()` del contrato en ese bloque. El contrato aún no se vincula a un
catálogo histórico inmutable. La consulta View puede añadir el contrato que
MEXC publica actualmente para un activo ETH con depósito habilitado, siempre
que esa asignación sea única; ORBIS compara esa dirección pública con el log.
Varios eventos, red distinta o dato ausente no
se resuelven por conjetura. El RPC recibe txId y consultas; el servidor local
recibe receptores transitorios, nunca los guarda completos. No se demuestra
finalidad irreversible ni conciliación patrimonial.
La comparación sigue los campos publicados en el
[historial de depósitos de MEXC](https://www.mexc.io/api-docs/spot-v3/wallet-endpoints/deposit-historysupporting-network)
y el evento `Transfer` del [estándar ERC-20](https://eips.ethereum.org/EIPS/eip-20).
El contrato procede de la [información de monedas de MEXC](https://www.mexc.io/api-docs/spot-v3/wallet-endpoints/query-the-currency-information).
Las capturas nuevas guardan una prueba local de que el UID de la clave View
coincidía con el vínculo seudónimo de esa cuenta antes y después de consultar
MEXC. Sólo esas capturas permiten confirmar manualmente el vínculo de una
transferencia con un movimiento REAL ya registrado. Las capturas anteriores
siguen consultables, pero no sirven para crear vínculos nuevos; deben volver a
consultarse. Los vínculos ya confirmados antes de esta comprobación se conservan
como históricos y no se elevan retroactivamente a evidencia de identidad.
Un vínculo individual tampoco sustituye la conciliación global de saldos,
movimientos, comisiones y recepción en la wallet.
La auditoría local de transferencias reúne todas las capturas de la conexión
MEXC vinculada a la cuenta REAL. Deduplica referencias finales entre capturas
con UID verificado y muestra vínculos confirmados, referencias sin asiento,
asientos sin vínculo, referencias contradictorias, filas sin identificador y
tramos consultados. Excluye las capturas sin UID verificado de esa cobertura;
los vínculos históricos permanecen señalados como anteriores a la prueba.
Una referencia final que retrocede a un estado pendiente o cambia de datos
entre capturas verificadas queda como contradictoria, no como pendiente normal.
Los asientos REAL sin vínculo se separan según si las capturas verificadas
cubren íntegramente las ±24 horas en que el comparador buscaría un candidato.
Un asiento con cobertura parcial o ausente no se interpreta como movimiento
que MEXC debería haber devuelto.
Incluso con cero pendientes el resultado **no** acredita saldo, historial
completo, titularidad o recepción en wallet, ni habilita ejecución.
El archivo puede comparar dos capturas sucesivas de la misma referencia local
sólo donde sus periodos se solapan. Informa estado cambiado, finalidad que
retrocede, campos distintos, fila nueva o que ya no se devuelve, además de
referencias repetidas y filas sin ID. La ausencia en una consulta no prueba
que desaparecieran fondos; la comparación nunca asienta ni libera capital.
En el registro manual de un traslado de activos entre cuentas propias, si la
comisión de red se paga en el activo enviado, se distingue cantidad enviada,
comisión y cantidad neta recibida. La base de coste completa acompaña al neto
para seguimiento patrimonial, sin P/L ficticio; no es una determinación fiscal
y requiere revisar la normativa aplicable. No se admiten aquí comisiones
pagadas en un tercer activo ni se infiere una transferencia externa de una
coincidencia de importes.
La misma regla de procedencia rige GRVT, con sus cuentas de financiación y
trading separadas. Los resultados de perpetuos requieren margen, financiación
y liquidación explícitos; mientras ese motor no exista, GRVT se limita a
conexión/observación y a los instrumentos cuya ejecución se haya validado.
Antes de operar LONG/SHORT en GRVT real, la misma versión deberá haber pasado
backtest y seguimiento PAPER local con capital ficticio. El testnet de GRVT
servirá para comprobar autenticación, firmas, órdenes, parciales y retiros,
pero no demostrará por sí solo que una estrategia sea rentable o robusta.
Las recompensas o airdrops se registran sólo cuando estén confirmados por el
venue; no se proyectan como rendimiento ni se hacen operaciones para
fabricar volumen de campaña.

## Separación de responsabilidades

1. LAB produce una definición versionada y resultados históricos; PAPER
   valida la misma semántica con datos actuales. Activar una versión en REAL
   será una acción distinta y explícita; editarla no alterará la versión
   activada. Esto se aplica tanto a DCA como a estrategias LONG/SHORT, con
   modelos de ejecución diferentes pero trazabilidad común. La IA podrá
   proponer o explicar, pero no cambiar permisos ni saltarse controles.
2. Un planificador produce una **intención** identificada de forma única. El
   guardián de riesgo autoriza o rechaza usando saldo libre, exposición,
   presupuesto, pérdida máxima, tamaño, liquidez/frescura, mercado, red y
   estado del interruptor de parada. La intención rechazada también se audita.
3. Un adaptador DEX o CEX convierte una intención autorizada en una orden o
   transacción específica. El adaptador no decide el tamaño ni el riesgo.
   Ninguna ruta PAPER o endpoint heredado `LIVE` puede saltarse al guardián.
4. Un conciliador compara el estado local con la fuente externa: transacción
   enviada/confirmada/fallida/reorganizada, orden abierta/parcial/cancelada,
   fills, comisiones y saldos. Sólo hechos confirmados entran en el libro REAL;
   la intención y la estimación quedan separadas del hecho contable.

Los contratos entre módulos serán explícitos: `MarketObservation` (venue,
instrumento, tiempo y calidad), `Signal` (versión y vela cerrada),
`ExecutionIntent` (ID idempotente y política), `ExternalOrder`/`Fill` (estado,
cantidad y comisión exactas), `FundingTransfer` (red, origen, destino y
confirmaciones) y `ReconciliationEvent` (diferencia y resolución). Se usarán
cantidades decimales y unidades del instrumento, no `float` monetario. Cada
adaptador publicará sus capacidades reales —spot/perpetuo, órdenes, balances,
depósitos, retiros, entorno de prueba— para impedir que la UI ofrezca una
acción no soportada.

## Permisos y custodia

- **Wallet:** ORBIS nunca recibirá la frase semilla ni la clave principal.
  La prueba optativa `personal_sign` EIP-191 verifica control de una EOA sólo
  durante la sesión REAL desbloqueada. La dirección y la firma llegan
  temporalmente al servidor local, pero no se persisten; un nonce de un solo
  uso, origen, cuenta, red y caducidad vinculan el mensaje. No funciona como
  autorización de pago, aprobación o delegación, ni prueba que el saldo
  observado pertenezca exclusivamente a la cuenta contable seleccionada.
  Las wallets de contrato requieren otro método de verificación.
  Un vínculo EOA optativo puede conservarse en la bóveda cifrada como alias,
  red, fecha y huella HMAC con sal por bóveda, sin dirección completa ni firma.
  El usuario lo confirma después de la prueba de sesión y puede desvincularlo.
  Es evidencia histórica de control, no una prueba de control actual: antes
  de cualquier flujo de ejecución habría que revalidar la wallet y otorgar
  una autorización de gasto separada y limitada.
  La consulta local de vínculo sólo se permite tras una prueba EOA de la
  sesión presente y distingue wallet de esta cuenta, de otra cuenta o sin
  vínculo. Se vuelve a evaluar por red; no convierte el alias en identidad
  de titularidad ni en saldo conciliado.
  Conexión de lectura no permite gastar. Ejecución DEX sin intervención por
  operación requiere una capacidad delegada revocable, con alcance de cadenas,
  contratos, tokens, destinatarios, importe por operación/periodo y caducidad
  **impuestos por el mecanismo de firma**, no sólo comprobados por la UI. Se
  evaluarán cuentas inteligentes/permisos avanzados donde sean compatibles;
  MetaMask y Trust Wallet no se supondrán equivalentes. Una credencial de
  delegación, si existe, se guardará cifrada y aislada, nunca en `localStorage`
  ni en registros. Sin delegación segura y compatible, la firma será manual.
- **MEXC:** ORBIS usará claves API oficiales con permisos mínimos; nunca
  pedirá la contraseña de la cuenta ni automatizará su
  formulario de inicio de sesión. Lectura, trading y retiro son alcances
  distintos. La primera ejecución CEX no necesitará permiso de retiro; para
  automatizar el regreso a la wallet hará falta autorización separada,
  destinos permitidos y límites más estrictos. La conexión de observación
  actual introduce una clave View en la UI local y la mantiene sólo en memoria
  de la sesión desbloqueada: no persiste ni se incluye en copias. Una clave
  Trade se puede comprobar opcionalmente por GET en la UI y queda sólo en
  memoria durante esa sesión; no habilita nuevas órdenes. Para automatización
  sostenida se exigirá custodia local segura y aislamiento más fuerte; las
  credenciales nunca estarán en LAB, PAPER ni copias sin cifrar.
- **GRVT:** la observación actual autentica una clave View por una cookie de
  sesión en memoria y sólo consulta resúmenes de cuenta. La respuesta no
  acredita el permiso de la clave: se debe crear y comprobar View en GRVT.
  El trading por API requiere una clave API **y** firmar órdenes
  con la clave privada de la dirección Ethereum vinculada a ella. Para
  automatizar se evaluará una clave de firma **dedicada al API**, distinta de
  la wallet principal y del SecureKey principal, limitada al permiso `Trade`
  de una Trading Account concreta, con IP permitidas cuando sea viable. Esta
  clave sí es un secreto sensible: requerirá bóveda cifrada, desbloqueo
  controlado, rotación/revocación y prueba de que no aparece en logs/backups
  sin cifrar. No se pedirá la semilla ni la clave principal de MetaMask o
  Trust Wallet. Depósitos, transferencias internas y retiro tienen permisos
  y firmas aparte. La existencia de la API no demuestra que una estrategia
  spot pueda ejecutarse con la misma semántica en GRVT.
- **Parada:** desactiva nuevas intenciones y cancela órdenes abiertas cuando
  el adaptador lo admita. No puede revertir una transacción on-chain ya
  confirmada. Revocación externa, saldo insuficiente, red distinta,
  desconexión, cotización caducada o datos incompletos dejan el sistema en
  estado seguro, sin nuevos envíos. El proceso sólo opera mientras el servicio
  local autorizado esté activo; reiniciar exige reconciliar antes de emitir.

## Identidad, datos y contabilidad

Cada activo se identifica por cadena + contrato (o activo nativo), no sólo
símbolo. Cada destino de depósito requiere cadena, dirección y memo/tag si
aplica; se comprueban red admitida, mínimo, comisión y tiempo de confirmación.
Las direcciones completas necesarias para depósitos/retiros se guardarán sólo
en la bóveda REAL cifrada con consentimiento, lista permitida y borrado.
Consultar RPC, relay o CEX puede revelar dirección e IP a terceros.

Entidades previstas: `live_policies` (versión de estrategia, venue, límites,
vigencia, estado y aprobación), `live_intents` (idempotencia y decisión de
riesgo), `external_orders`, `external_fills`, `chain_transactions`,
`funding_transfers`, `balance_observations` y `reconciliation_events`. Las
referencias externas y snapshots guardan proveedor, cuenta/red, tiempo UTC,
fuente, versión de adaptador, estado y respuesta saneada. Los IDs de orden
cliente y hashes de transacción evitan duplicados tras reintentos. El libro
REAL registrará aportaciones/retiros, transferencias propias, compras/ventas,
fees y ajustes con procedencia; nunca contará un movimiento interno como
beneficio ni sumará dos veces un saldo observado y uno manual vinculado.

## Entrega progresiva y puertas de aceptación

El transporte GRVT testnet de `app/live_execution/grvt_testnet_order.py` es
una pieza **exclusiva de testnet**: acepta una única orden IOC límite ya
firmada para la subcuenta de una clave testnet, con chain ID 326, tope local
de nocional y `client_order_id` del rango de integradores. El transporte no
firma ni verifica criptográficamente la orden recibida; GRVT debe validarla.
Un acuse no acredita
ejecución. La instancia no reenvía tras respuesta dudosa; permite consultar
la orden por ID de cliente en testnet, pero una ausencia no demuestra que no
se haya creado. El firmante interno de `grvt_testnet_signer.py` aplica el
dominio EIP-712 de testnet y exige metadatos públicos, tamaños/precios exactos
y dirección de un firmante dedicado. No conserva su clave; tampoco ofrece aún
un almacén seguro de ella ni autorización de Trade comprobada. La UI descrita
abajo usa el puente firmado, no el transporte sin verificar por separado.
La auditoría de fills, costes y cuenta es observacional: faltan conciliación
causal de posición/saldo y una transición terminal segura antes de vincular
estrategias.

La bóveda REAL v13 conserva un diario testnet separado con ID de cliente,
huella de pedido, límites, estado de POST y parada por subcuenta; no guarda
clave, cookie ni firma. `grvt_testnet_submit.py` confirma `POST_CLAIMED` antes
del envío y la recuperación sólo permite observar, nunca reenviar el mismo
ID. La v11 archivó la auditoría saneada de fills y cuenta con huella,
sin crear asientos REAL; lecturas repetidas no duplican el archivo y un fill
archivado no puede cambiar. La v12 archiva el preflight de riesgo antes del
POST y exige su presencia y frescura al reclamarlo. La v13 añade la marca
única de cancelación testnet, sin convertir la parada en cierre contable.
La auditoría guarda el
delta de posición y saldo frente a ese preflight, pero lo etiqueta sólo como
compatibilidad observada, no como causalidad. Las migraciones conservan copias
cifradas recuperables. La auditoría no verifica liquidación, causalidad del saldo ni
permisos de Trade.
No aceptar una clave de producción como sustituto de credenciales testnet.
`POST /real/exchanges/grvt/testnet/signals/submit` usa únicamente los endpoints
testnet fijos del cliente. Requiere una acción `AUTO_PAPER` reciente, firma
EIP-712 para chain ID 326, clave testnet dedicada introducida en esa solicitud,
bóveda local desbloqueada y `ENVIAR_GRVT_TESTNET`. El límite solicitado jamás
puede superar 100 USDT; el servidor revalida el nocional exacto antes de un
login externo. Antes de transmitir la orden, ORBIS consulta el instrumento
público testnet y recupera localmente el firmante EIP-712 de la orden exacta:
un cambio en precio, tamaño, lado, reduce-only, builder o nonce invalida la
firma. Esto no verifica que GRVT haya concedido permisos Trade a ese firmante
ni acredita aceptación o fills. La respuesta conserva `execution_unverified=true`; un error
puede ocurrir después de `POST_CLAIMED`, así que consultar el registro por ID
es obligatorio y repetir la llamada no reenvía esa orden.

`POST /real/exchanges/grvt/testnet/signals/sign-and-submit` evita preparar la
firma fuera de ORBIS: recibe **en una sola solicitud local** una clave API
Trade testnet y su clave privada EIP-712 dedicada (hexadecimal de 32 bytes),
la dirección esperada, el builder, tamaño y precio límite. Consulta las reglas
públicas testnet y el bid/ask **testnet** de hasta 5 s; exige límites explícitos
`max_spread_bps` y `max_slippage_bps` (0–100 cada uno). Compara el precio
límite con ese mercado antes de firmar y vuelve a consultar la cotización
después del doble preflight, antes de reservar el ID y reclamar el POST.
Una cotización vencida, cruzada, incompleta o fuera de tolerancia en el
guardia previo bloquea antes de reservar ese ID. Si vence durante la escritura
del preflight, el intento puede quedar `PREPARED` sin POST y necesitar cierre
local antes de crear otro. Firma un IOC de 60 s, verifica la firma y delega al mismo
puente PAPER, límite de 100 USDT, preflight y diario de un único POST. Exige
`FIRMAR_Y_ENVIAR_GRVT_TESTNET`; no reutiliza la conexión View de mainnet ni
sus endpoints. ORBIS no guarda estas claves ni las devuelve, pero durante la
petición existen en memoria del proceso: usar sólo una clave **dedicada a
testnet**, no la clave de una wallet patrimonial. Hay UI de firma/envío manual,
no ejecución autónoma; esta ruta no convierte una señal PAPER en rentabilidad real.
El guardia de precio acota el límite elegido, no garantiza liquidez, ejecución
ni precio final: la orden IOC puede caducar o llenarse parcialmente.
Si supera el guardia, el preflight cifrado archiva fuente testnet, instrumento,
hora del ticker, bid/ask, tamaños, lado, precio límite y máximos de spread y
deslizamiento. Antes de reclamar el POST, ORBIS revalida esa evidencia contra
el intento exacto; registros anteriores sin guardia siguen siendo legibles.

La API de parada
`POST /real/exchanges/grvt/testnet/subaccounts/{id}/stop` bloquea nuevas
órdenes localmente, pero **no** asegura cancelación de órdenes externas.
`POST /real/exchanges/grvt/testnet/orders/audit` consulta y archiva orden,
fills y cuenta sólo para un ID testnet ya reclamado. Si existe cancelación
reclamada, audita esa rama. `POST /real/exchanges/grvt/testnet/orders/stop-cancel`
escribe primero la parada local y reclama en disco un único intento de
cancelación antes de llamar a GRVT. Un fallo de red o login deja la parada
activa; si se reclamó el intento, el operador debe auditar el mismo ID y
nunca repetir la cancelación.
Si el login falla antes de reclamar la cancelación, la respuesta distingue
«no se envió» de «resultado incierto»; la parada local sigue activa.
GRVT devuelve sólo un acuse de cancelación; ORBIS consulta luego la orden
por ID y conserva `OPEN`/`PENDING` si aún no es terminal. Si la consulta
falla, el resultado queda incierto y la marca de cancelación impide otro POST.
Ambas rutas requieren bóveda desbloqueada, clave testnet proporcionada en
la solicitud y confirmación literal. Ni la auditoría ni una respuesta
`CANCELLED` convierten automáticamente fills en patrimonio REAL.
Faltan credenciales Trade testnet, ensayo contra el proveedor, custodia segura
persistente y conciliación terminal antes de automatizar. La UI
local de recuperación sí permite auditar, parar y cancelar por ID testnet
archivado; borra la clave introducida tras la petición y no envía órdenes
nuevas.

### Uso del envío GRVT TESTNET desde la interfaz

En «Patrimonio registrado → Recuperación GRVT → Enviar señal · TESTNET»:

1. Desbloquear LAB, GRVT PAPER y la bóveda REAL. Crear/probar la estrategia
   LONG o SHORT en `AUTO_PAPER` con histórico GRVT público verificable.
2. Configurar subcuenta **testnet**, unidades, límite IOC y tope nocional
   (hasta 100 USDT), tolerancias de spread/deslizamiento en bps y credenciales
   Trade dedicadas de testnet. La clave de firma es la generada para la API,
   nunca la clave privada de MetaMask/Trust Wallet. Firmante y builder son
   explícitos; la comisión builder permanece fijada a cero.
3. Seleccionar una acción reciente y confirmar «Firmar y enviar · TESTNET».
   El selector muestra cuenta de origen, instrumento, LONG/SHORT, entrada o
   cierre, BUY/SELL, ID estable y caducidad. No propone el tamaño, saldo ni
   apalancamiento ficticios como capital testnet; las unidades son explícitas.
4. Consultar el registro cargado tras el intento y auditar el ID si hace falta.
   Una respuesta perdida se muestra incierta, nunca como compra, venta ni fill.
   La misma selección queda consumida en pantalla; el diario duradero bloquea
   también el replay tras bloquear, recargar o reiniciar.

El selector lee como máximo las últimas 100 sesiones automáticas. Sólo muestra
la última acción ejecutada de cada una; no certifica su replay. Su endpoint
`GET /real/exchanges/grvt/testnet/signals/candidates` requiere REAL y GRVT
PAPER desbloqueados, no llama a GRVT, no crea operaciones ni reservas y no
renueva la sesión.
La lectura se repite cada 1,5 s sólo mientras el panel está abierto. No cambia
la acción elegida ni envía automáticamente; una acción caduca a los 20 s y
se vuelve a validar durante la firma/preflight y antes del POST. Un origen
alterado, detenido o no reproducible sigue bloqueado por el puente autoritativo.
En cierres se deriva el lado contrario y `reduce_only=true`, y el preflight
exige una posición testnet compatible; el cierre PAPER no acredita que exista.

Las credenciales se borran del formulario al iniciar el intento, al cerrar el
panel, remontarlo o bloquear REAL. No se guardan en almacenamiento web ni en
el diario. Esto no protege frente a un navegador o sistema comprometido.
Se mantiene la recuperación independiente sin credenciales de envío. Las
órdenes pendientes bloquean otros intentos en la misma subcuenta, incluso
si llega una señal posterior; ahora pueden cerrarse localmente mediante la
conciliación acotada descrita abajo. No hay rearme automático ni prueba causal
general del saldo. La UI no convierte este tramo en un
bot continuo con salidas protectoras colocadas en GRVT.

Decisión: exponer el puente existente sin ampliar a mainnet, sin heredar la
conexión View y sin almacenar claves Trade. Conserva la separación de
[entornos y autenticación documentada por GRVT](https://api-docs.grvt.io/api_setup/)
y el [dominio de firma](https://api-docs.grvt.io/) (revisados el 30-10-2026).
El protocolo real y los permisos efectivos siguen necesitando ensayo humano
testnet. DCA no se ha conectado a este envío GRVT: su vía REAL es MEXC Spot.

### Conciliación acotada y circuito de entrada/salida GRVT TESTNET

«Recuperación GRVT» ofrece **Conciliar · TESTNET** tras dos auditorías elegibles.
No basta con que el exchange devuelva `FILLED`. Se exige:

- Orden terminal `FILLED`, `CANCELLED` o `REJECTED`, con instrumento, lado,
  tamaño, ID, actualización y fills coincidentes con el intento persistido.
- Dos últimas auditorías separadas al menos 5 s; la última tiene como máximo
  30 s. Todas las lecturas conservan sus huellas y los fills previos no pueden
  desaparecer ni cambiar. Cuenta posterior fresca y coberturas completas de
  los historiales desde el preflight, con cursores agotados.
- Ningún fill ajeno, liquidación, financiación no vinculada, transferencia,
  depósito, retirada o interés detectado. La financiación propia admisible se
  define más abajo. También se rechaza actividad externa observada
  en una auditoría anterior, aunque otra posterior dejara de devolverla.
- USDT, margen simple y modo de activo único, sin colateral no nulo en otras
  divisas ni órdenes abiertas. Las auditorías antiguas sin esos metadatos
  permanecen consultables, pero no se certifican retroactivamente.
- `posición_post = posición_pre + fills_firmados` y
  `saldo_post = saldo_pre + P/L_realizado_reportado − comisiones_USDT − financiación_pagada_USDT`, con
  Decimal y precisión 100, sin tolerancias o ajustes escondidos. Los rebates
  negativos se conservan. Builder fee debe ser cero, los fills respetan el
  precio límite y una ampliación de posición no puede declarar P/L realizado.
  Equity se contrasta con saldo + P/L no realizado; una cuenta plana no puede
  mantener margen o P/L pendientes.

El significado de las unidades, P/L y rebates se contrasta con el
[esquema de fills de GRVT](https://api-docs.grvt.io/schemas/api_fill_history_response/)
y la relación de equity con el
[resumen de subcuenta](https://api-docs.grvt.io/schemas/api_sub_account_summary_response/).
Se usa el P/L que reportan los fills; este recibo no sustituye un cálculo
independiente del coste histórico, no prueba ausencia de omisiones del
proveedor y conserva expresamente `causal_proof=false`.

`POST /real/exchanges/grvt/testnet/orders/reconcile` requiere autenticación
local y `CONCILIAR_GRVT_TESTNET`, no claves externas. Guarda el recibo y cambia
el intento a `RECONCILED` en una sola transacción. Sus replays devuelven el mismo
recibo tras recalcularlo; una respuesta perdida no crea otro. La migración a
bóveda v30 añade `grvt_testnet_reconciliations`, con copia cifrada previa y
comprobación de conservación del historial. No crea eventos de cartera REAL,
no transmite órdenes, no quita STOP y no conserva firmas ni credenciales.

La financiación liquidada de una reducción puede conciliarse si `tx_id`
corresponde al `trade_id` de un fill propio de esa orden, en el mismo
instrumento y USDT. El pago debe ser posterior o simultáneo al fill y anterior
al corte de la consulta y a la cuenta posterior. No se usa una tasa pública
para inferir el cargo. Es la relación documentada por
[FundingPayment de GRVT](https://api-docs.grvt.io/schemas/funding_payment/):
importe positivo pagado, negativo cobrado; un movimiento de transferencia o
liquidación no se convierte por ello en funding de una orden propia.

Se recomputan contadores y totales con Decimal (precisión 100), se rechazan
duplicados y se conservan pagos ya observados en todas las auditorías. Las
dos últimas deben coincidir. Un pago con efecto no nulo requiere una reducción
de posición conocida, sin giro ni aumento; un evento propio de importe cero
se conserva también al abrir. Cargos/abonos ajenos o compensados sin vínculo,
pagos anteriores al fill y pagos desaparecidos siguen bloqueando.

Los recibos sin pagos mantienen v1. Cuando hay filas propias, el recibo v2
añade `funding_paid_usdt` y `net_realized_pnl_usdt` (P/L de los fills menos
comisiones y financiación **de esa orden**, no de las anteriores). «Financiación»
permite inspeccionar estos importes sin añadir un panel permanente. El informe
de recuperación devuelve `funding_summary`; un vínculo a fills no equivale por
sí solo a saldo conciliado. STOP permanece activo tras conciliar si estaba
activo. Las lecturas y backups revalidan los recibos y el historial.

Una señal nueva puede solicitar otro intento en la misma subcuenta una vez
conciliado el anterior. El envío sigue siendo explícito y vuelve a comprobar
firma, mercado, frescura, saldo, margen, tamaño y límites. Antes del login, de
reservar y de reclamar el POST se verifican los recibos anteriores; antes de
reclamar se exige además que el saldo/posición del preflight continúen el
último recibo. Diferencias de saldo/posición o retroceso de reloj bloquean el circuito.
Usar una subcuenta testnet de uso exclusivo: esa continuidad de snapshots no
demuestra ausencia de actividad externa compensada durante los intervalos
entre lecturas. Los nuevos preflights v2 añaden una revisión de historial entre
el recibo anterior y un corte fijo anterior a la siguiente reserva:

- Se vuelve a consultar la orden conciliada y se agotan fills de toda la
  subcuenta, financiación, transferencias, retiradas, depósitos e intereses
  desde su preflight anterior. Todos los historiales usan el mismo corte
  superior explícito; el inicio se solapa un nanosegundo en la petición para
  conservar eventos del límite. Cada lector admite hasta 20 páginas de 500
  registros y exige agotar el cursor.
- Los fills propios anteriores no pueden cambiar o desaparecer. Fills ajenos
  o liquidaciones y cualquier movimiento nuevo no explicado bloquean, aunque sus importes
  se compensen y saldo/posición sigan iguales. Los movimientos de la cuenta
  principal que no puedan atribuirse con seguridad tampoco se ignoran.
  La financiación ya incluida en el recibo anterior debe conservarse exactamente:
  el historial v2 la enlaza sin volver a restarla del saldo. Un pago nuevo o
  tardío que no figure en ese recibo exige revisión; no se reescribe la historia.
- Tras revisar el historial se consulta de nuevo el riesgo. La cuenta debe ser
  posterior al corte; si no lo es, se rechaza antes de consumir el ID de la
  nueva señal. El corte tiene una antigüedad máxima de 5 s al registrar,
  reclamar y comprobar de nuevo el envío. STOP y el diario siguen vigentes.
  En órdenes reduce-only pueden variar P/L no realizado y márgenes entre las
  dos lecturas: cada una debe pasar su guardia y cuadrar equity con saldo + P/L.
  Saldo, posición, modo, órdenes abiertas y reglas siguen teniendo que coincidir.
  Las aperturas mantienen la comparación estricta de todos esos campos.
- La evidencia enlaza el recibo exacto anterior, su huella, el inicio/corte,
  las páginas agotadas y los fills conservados. Se guarda cifrada junto al
  preflight, se devuelve en `history_continuity` del informe de recuperación y
  se revalida al leer, conciliar y verificar copias de seguridad. El formato
  del documento cambia a v2; la bóveda permanece en v31. Los preflights v1
  históricos no reciben retroactivamente esta evidencia.

El corte precede a las consultas: la revisión no prueba ausencia de actividad
posterior al corte, atomicidad de los endpoints, retención completa ni ausencia
de omisiones del proveedor. Se mantiene `causal_proof=false`; esta revisión
acotada no habilita mainnet ni sustituye la conciliación causal general.
Conciliar una entrada no cierra su posición: el siguiente cierre debe tener
su propia señal reciente, orden reduce-only, auditorías y recibo.

La paginación y los límites temporales se contrastaron con los esquemas
oficiales de [fills](https://api-docs.grvt.io/schemas/api_fill_history_request/),
[financiación](https://api-docs.grvt.io/schemas/api_funding_payment_history_request/),
[transferencias](https://api-docs.grvt.io/schemas/api_transfer_history_request/),
[depósitos](https://api-docs.grvt.io/schemas/api_deposit_history_request/),
[retiradas](https://api-docs.grvt.io/schemas/api_withdrawal_history_request/) e
[intereses](https://api-docs.grvt.io/schemas/api_interest_payment_history_request/).

Alcance pendiente: financiación sin vínculo propio, pagos tardíos posteriores
al recibo y otros movimientos no vacíos requieren una conciliación ampliada;
por ahora bloquean y deben revisarse en GRVT, donde
podría ser necesario cerrar manualmente una posición. No hay órdenes stop
protectoras colocadas por este panel ni ejecución autónoma/24 h. El ensayo con
credenciales Trade testnet reales y la conciliación causal general siguen
pendientes para este panel legado. La custodia y autorización del
[bot MAINNET v54](GRVT_BOT_MAINNET.md) se implementan en un panel independiente,
sin ampliar los permisos de este recorrido. Las pruebas controladas
sí recorren entrada → auditorías → conciliación → cierre en la misma subcuenta,
para LONG y SHORT, con saldo compartido, sin llamar al exchange real.

Hay un [panel manual TP/SL TESTNET](GRVT_PROTECCIONES_NATIVAS.md#panel-manual-y-api-local) para
preparar, revisar, firmar y cotejar órdenes GTT reduce-only con disparador LAST.
Sus rutas locales protegidas usan el motor nativo, no POST directos. El diario OCO v34 permite
un bulk nativo, preflight, dos IDs permanentes y lectura tras reinicio; v35
añade cancelación expresa con STOP y una sola concesión duradera. Un acuse
o dos estados terminales no son conciliación financiera. V36 añade dos
auditorías conjuntas, recibo explícito y continuidad desde el saldo/posición
posteriores al OCO, sin asientos REAL ni eliminación de STOP. La subcuenta
sólo puede superar este bloqueo con el recibo verificado; otra orden vuelve
a requerir preflight, límites e historial completo. Faltan el ciclo completo
de entrada/salidas y emergencia, autorización automática y piloto. No cambia el alcance del envío IOC anterior.
Los disparadores son metadatos no firmados por GRVT; se contrastan por
separado y no se describen como una garantía trustless de ejecución.

La [entrada FOK/OTOCO](GRVT_PROTECCIONES_NATIVAS.md#entrada-fok-y-otoco--núcleo-interno-en-desarrollo)
dispone de compilación, firma, transporte de tres órdenes y reserva v37.
V38 añade coordinador con replay causal de entrada LAB/PAPER, dos controles
de riesgo, historial, expiración acotada a la señal, concesión irreversible y
recuperación de estados por los tres IDs tras reinicio. No ofrece aún
colocación HTTP/UI ni activa una estrategia: faltan emergencia
y reglas completas de salida. Los SL/TP revisados no
se presentan como una estrategia automáticamente desplegada. El transporte
nunca sustituye esas puertas; observación y acuse no liberan la subcuenta.
V39 incorpora la cancelación explícita del grupo con STOP, consentimiento,
concesión única persistente y recuperación de los tres IDs. No compra, vende
ni cierra exposición, y tampoco levanta el bloqueo financiero del grupo.
V40 incorpora la conciliación financiera conjunta desde el preflight plano,
dos auditorías estables, recibo inmutable y continuidad v4 hacia el siguiente
IOC. Se contrastan las tres órdenes, fills, comisiones, financiación y saldo;
el P/L sigue siendo el reportado por el proveedor, sin prueba causal general.
Las discrepancias posteriores bloquean otra operación conservando el recibo
histórico. No borra STOP ni crea asientos REAL. Este avance no incorpora aún
la integración de emergencia, worker automático ni colocación OTOCO en HTTP/UI.
V41 añade una reducción de emergencia interna con STOP y ámbito cifrado,
ligada al grupo y al recibo exacto. Reutiliza el diario y la conciliación IOC:
un parcial conserva la exposición y exige recibo más otro ID para seguir.
No es una vía para abrir posiciones con STOP ni habilita todavía ese control
en HTTP/UI o en un bot automático. Véase la [guía de emergencia](GRVT_PROTECCIONES_NATIVAS.md#reducción-de-emergencia-del-grupo-otoco--v41).

El corte manual posterior ya ofrece «Entrada protegida · TESTNET» y rutas
locales para revisión, un envío, recuperación, recibos y emergencia. Conserva
causalidad de entrada LAB/PAPER y protección manual revisada; no ejecuta todavía
todas las reglas de salida de la estrategia ni activa un trabajador. Permite
validar el circuito desde la UI antes de avanzar a esa autoridad. Las revisiones
caducan, se vinculan a sesión y no guardan credenciales ni crean asientos REAL.

1. **Conexión y observación:** MetaMask/Trust Wallet, MEXC Spot y GRVT en
   modo lectura; saldos, órdenes/transferencias históricas, conciliación,
   revocación y errores. Importar y valorar saldo MEXC previo sin fabricar
   compras ni costes. Sin credenciales de trading o firma.
2. **Ejecución controlada:** empezar por MEXC Spot, un par y cuenta a la vez;
   política REAL
   explícita, límites y parada, credencial de trading acotada, órdenes mínimas
   en entorno de prueba/sandbox cuando exista. Verificar parciales,
   reintentos, caída del proceso y discrepancias antes de capital real.
3. **GRVT y DEX delegado:** validar primero LONG/SHORT GRVT en PAPER local y
   en testnet para el instrumento, firma y límites elegidos. Los perpetuos
   exigen margen, liquidación y financiación verificados. Probar Uniswap y,
   en cada wallet/red, la
   delegación exigida para automatizar. Luego depósitos wallet→MEXC, espera
   de abono, caducidad de señal y retorno MEXC→wallet con permisos de retiro
   separados. El regreso desde GRVT incluye el estado del puente/retiro.
   No se promete automatización para combinaciones incompatibles.
   El corte [DEX manual BNB](WALLET_SWAP_BNB.md) ya permite cotizar, autorizar
   un importe exacto, intercambiar USDT↔WBNB y observar el recibo; cada acción
   se confirma en la extensión. Está probado con wallet controlada y consulta
   pública de pools, no con una wallet humana en mainnet. No contabiliza
   automáticamente swaps ni sustituye la autorización delegada para bots.
   Su registro explícito de compra/venta y gas ya está disponible: se revisa
   en la cuenta vinculada a la wallet y enlaza asientos existentes sin
   duplicarlos. No crea capital ni supone una conciliación independiente del RPC.
4. **Activación real:** sólo después de auditoría de seguridad, ensayo con
   importes mínimos y consentimiento específico para estrategia, venue,
   activos, redes, destinos, límites y caducidad. PAPER continúa aislado.

Pruebas obligatorias: doble señal y reintento no duplican orden/retiro; orden
parcial no se trata como completa; reorg revierte confirmación; red o contrato
erróneo bloquea; se rechazan depósitos sin memo o red confirmada; dos
estrategias no exceden límites agregados; parada persiste tras reinicio;
desconexión no desencadena órdenes retrospectivas; saldo REAL reconstruido
coincide con fills/recibos y comisiones; cero semillas/contraseñas en ficheros,
logs, respuestas y backups.

## Fuentes técnicas a verificar al implementar

- [MetaMask Connect](https://docs.metamask.io/metamask-connect/) y
  [Smart Accounts Kit/permisos avanzados](https://docs.metamask.io/smart-accounts-kit/).
- [Trust Wallet: extensión](https://developer.trustwallet.com/developer/develop-for-trust/browser-extension/evm)
  y [WalletConnect móvil](https://developer.trustwallet.com/developer/develop-for-trust/mobile).
- [Binance Spot REST y permisos](https://developers.binance.com/en/docs/products/spot/rest-api),
- [MEXC Spot API](https://www.mexc.io/api-docs/spot-v3/introduction) y
  [cambios de API](https://www.mexc.io/api-docs/spot-v3/change-log): la
  disponibilidad del retiro por API debe verificarse de nuevo antes de usarlo.
- [GRVT: API de trading](https://api-docs.grvt.io/trading_api/) y
  [modelo de cuentas/permisos](https://api-docs.grvt.io/learn/),
  [claves API y firma](https://help.grvt.io/en/articles/9614688-what-are-api-keys).
- [GRVT: testnet](https://help.grvt.io/en/articles/9588408-getting-started)
  y [programa de recompensas](https://help.grvt.io/en/collections/15495825-grvt-airdrop),
  cuyas condiciones cambian y no forman parte del rendimiento de una estrategia.
- [Uniswap: Universal Router](https://developers.uniswap.org/docs/protocols/universal-router/overview)
  y [aprobaciones Permit2](https://developers.uniswap.org/docs/trading/swapping-api/concepts/permit2).

La documentación externa evoluciona; APIs, redes, permisos y restricciones
legales se revisarán de nuevo antes de cualquier integración operativa.

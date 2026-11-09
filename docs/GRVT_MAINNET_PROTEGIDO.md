# GRVT MAINNET — entrada protegida

Estado: 03-11-2026, bóveda v56 (núcleo protegido v50–v52). La API y el panel local «Operar GRVT · MAINNET»
conectan revisión, entrada FOK con SL/TP nativos reduce-only, seguimiento y cierre
explícito mediante IOC acotadas. El recibo financiero se exige antes de liberar
la reserva. Las pruebas usan API, firmas y SQLCipher reales, con HTTP externo de
GRVT controlado. **No hay piloto Trade con el proveedor.** El
[bot automático MAINNET](GRVT_BOT_MAINNET.md) utiliza este núcleo desde v54, con
revisión y autoridad propias.
No se han enviado órdenes reales ni utilizado credenciales o fondos del usuario.

## Contrato y separación

`grvt_mainnet_bracket.py` es el contrato de MAINNET. Firma EIP-712 en chain
325 y verifica las tres firmas contra la misma definición. TESTNET mantiene
su wrapper fijo en 326. Sólo el codec de tipos y el resumen de estados son
compartidos; endpoints, permisos y diarios son independientes.

Cada grupo tiene una entrada FILL_OR_KILL con vigencia de 20 segundos y dos
salidas GOOD_TILL_TIME reduce-only, con disparador LAST y plazos de 120 a
86.400 segundos. LONG y SHORT usan lados contrarios al cerrar. El redondeo
por tick no amplía el deslizamiento autorizado. Los tres IDs se reservan
juntos y los tres mínimos de tamaño/nocional se comprueban antes de firmar.

El techo permitido es 1.000 USDT de nocional por entrada y 10.000 por salida.
El importe autorizado sigue siendo el que introduzca el usuario: esos techos
no asignan capital, no aumentan permisos anteriores y no garantizan ejecución.
No se exime automáticamente a reduce-only de los mínimos publicados.

## Preflight, emisión y evidencia

`grvt_mainnet_transport.py` sólo permite hosts de producción fijados en el
código. La sesión privada exige cookie, cuenta y subcuenta coherentes. Clave
API, clave de firma, cookie, dirección completa del firmante y firmas r/s/v
no se persisten. Las referencias de cuenta y firmante usan HMAC con sal local.

`grvt_mainnet_preflight.py` obtiene dos estados de cuenta y dos lecturas de
margen más libro actual. Exige subcuenta SIMPLE_CROSS/SINGLE_ASSET aislada,
liquidación USDT, sin posiciones ni órdenes previas, saldos/equity coherentes,
colchón de equity de al menos 2 veces el nocional, reglas estables y límites
de precio, spread, liquidez y antigüedad. Se comprueba la cronología local:
una observación posterior al claim no sirve como prueba previa al envío.
Esta política inicial no reproduce un motor general de margen del exchange.

`grvt_mainnet_submit.py` comprueba confirmación MAINNET propia, permiso RAM
revocable y generación de bóveda. `grvt_mainnet_journal.py` confirma en la
base cifrada el claim antes de emitir el único POST. Una respuesta perdida
deja POST_CLAIMED y no permite reenviar con el mismo ID ni con otro. El STOP
persiste. Un acuse queda como ACKNOWLEDGED: no prueba fills ni saldo plano.

`grvt_mainnet_observation.py` consulta los tres IDs originales, coteja contrato,
firmas históricas en 325 y digests, y añade observaciones encadenadas. Rechaza
contratos alterados, IDs de otro grupo, regresiones de fills/estado y truncado
del historial. La posición derivada de órdenes continúa etiquetada como no
verificada contra la cuenta. Un resultado incierto no se transforma en beneficio.

La bóveda v50 añade `grvt_mainnet_brackets`, `grvt_mainnet_legs`,
`grvt_mainnet_controls` y `grvt_mainnet_observations`. La migración usa una
copia cifrada, conserva preimagen y contrasta los registros anteriores. El
verificador de backup incluye los nuevos claims y cadenas de observación.
No se restaura autoridad de envío al abrir una copia.

## Comprobación contra el proveedor

La consulta pública real del 03-11-2026, sin credenciales, confirmó en ambas
redes que `margin_rules` devuelve directamente `instrument`,
`max_position_size` y `risk_brackets`. El lector anterior de riesgo TESTNET
esperaba erróneamente un sobre `result`; sus dobles de HTTP reproducían esa
misma suposición. El nuevo decoder común admite el DTO directo y, por
compatibilidad, sólo un sobre `result` inequívoco. Rechaza errores y respuestas
con ambas representaciones. La [documentación oficial del DTO](https://api-docs.grvt.io/schemas/api_get_margin_rules_response/)
respalda ese formato.

El instrumento BTC_USDT_Perp devolvió mínimo nocional 100 USDT, mínimo base
0,001 y tick 0,1. El antiguo techo de 100 USDT impedía configurar un grupo
completo cuando una de las salidas quedaba por debajo del mínimo. Se permite
ahora un límite explícito mayor; no se aumenta la cantidad ni el presupuesto
para resolverlo por cuenta del usuario. Estos mínimos son metadatos del
proveedor y se vuelven a consultar; no se fijan a 100 en el motor.

Evidencia pública local: `/tmp/orbis-grvt-public-margin-probe-v50-20261103.json`.
Las pruebas de firma usan eth_account y una estructura EIP-712 independiente.
Las pruebas privadas sustituyen HTTP en la frontera: no acreditan aceptación
Trade, fills, comisiones o retiradas del proveedor real.

## Pendiente para habilitar el circuito real

- Concesión nueva y explícita tras vencer el plazo o agotarse los intentos de
  recuperación, conservando los intentos anteriores. Repetir la preparación
  actual no amplía su permiso; los casos no recuperables quedan en revisión.
- Continuidad y límites agregados del bot real, además de la entrada manual.
  Un recibo nativo libera su reserva pero no concede permiso para otra orden.
- Enlace con estrategia versionada, límites del bot y STOP; no copiar una
  autorización TESTNET ni activar MAINNET desde un resultado histórico.
- Piloto del proveedor con credenciales Trade aportadas en la interfaz local,
  alcance limitado y decisión explícita del usuario antes de mover fondos.
- Verificación real de fondos wallet/exchange y retirada, además del alcance
  general pendiente de ORBIS. La entrada manual no completa el bot MAINNET.


## v51 — conciliación de una salida nativa completa

`grvt_mainnet_finance.py` consulta fills, financiación, transferencias,
retiradas, depósitos e intereses de la subcuenta con el mismo corte temporal.
Agota hasta 20 páginas de 500 registros por historial. Otro movimiento no vacío,
un cursor repetido o una página incompleta impiden el recibo, aunque el saldo
neto aparente cuadrar. Los datos desconocidos de esos movimientos, incluidos
destinos, no se archivan.

Sólo acepta los tres contratos terminales y exposición cero. Contrasta cada
fill con ID, lado, instrumento, tamaño, precio límite, precisión base y nueve
decimales de precio del contrato. Rechaza liquidaciones, comisiones en otra
divisa, builder fees no nulas y ejecuciones anteriores al fill completo del
padre. La suma de fills coincide con el estado de cada orden.

Para un ciclo plano calcula, con Decimal y precisión 100:

```
LONG:  PNL bruto = suma(cantidad × precio de salidas) − suma(cantidad × precio de entrada)
SHORT: PNL bruto = suma(cantidad × precio de entrada) − suma(cantidad × precio de salidas)
Neto = PNL bruto − comisiones − financiación pagada
Saldo final = saldo inicial + neto
```

El ciclo plano evita dividir un VWAP periódico y redondearlo. El cálculo se
contrasta exactamente con el PNL reportado por el proveedor, no sólo con el
saldo. Si el proveedor aplica un redondeo que aún no se ha modelado, se conserva
el bloqueo; no se añade una tolerancia silenciosa. Rebates y financiación
recibida conservan su signo. La financiación debe enlazar su `tx_id` con un
fill propio del mismo instrumento y moneda. Referencias de semántica:
[fills](https://api-docs.grvt.io/schemas/fill/) y
[financiación](https://api-docs.grvt.io/schemas/funding_payment/).

Cuenta y equity deben cuadrar con USDT, posición y margen cero. Cada auditoría
conserva su observación de contratos, seis historiales normalizados, snapshot
de cuenta y resultado recalculable. La recolección completa tiene un límite
de frescura de 5 segundos. La confirmación exige dos auditorías estables,
separadas al menos 5 segundos y con la última de hace como máximo 30 segundos.
No se admiten nuevas observaciones o auditorías sobre un recibo ya sellado.

La bóveda v51 añade `grvt_mainnet_audits`, `grvt_mainnet_audit_heads`,
`grvt_mainnet_reconciliations` y `grvt_mainnet_settlement_controls`. El ancla
por grupo detecta auditorías truncadas; los recibos forman otra cadena por
subcuenta, con contador y ancla final. El backup reejecuta el cálculo y verifica
los enlaces. Recalcular un hash tras cambiar una cifra no la vuelve válida.
La migración desde v50 preserva intentos activos sin inventar un recibo.

Sólo el recibo plano libera, en una misma transacción, `active_slot` y la
referencia pendiente. Conserva el STOP y no restaura claves o autorización.
Otra entrada necesita una acción nueva, preflight fresco y un contrato nuevo;
el historial previo se valida antes del login. Conciliar no importa operaciones
ni ganancias al libro REAL automáticamente.

El recibo es un contraste acotado de respuestas REST; mantiene
`causal_proof=false`. No acredita atomicidad del exchange ni descarta actividad
posterior a su ventana. Se ha probado con HTTP privado controlado y base
cifrada real, no con una cuenta Trade del proveedor. El bot
MAINNET automático sigue pendiente. El núcleo v52 añade el cierre acotado del residuo;
la integración manual actual se describe al final de este documento.


## v52 — cancelación, IOC reduce-only y recibo de recuperación

`grvt_mainnet_recovery.py` conserva una petición explícita con plazo de 30 a
300 segundos, entre 1 y 3 intentos y los topes de salida, spread y deslizamiento
de la entrada original. No puede ampliarlos ni renovarse al repetir la acción.
La cabecera y los eventos son evidencia; `authorization=false` permanece al
reabrir la bóveda. Cada llamada requiere además un permiso RAM revocable.

La cancelación guarda su claim antes de emitir un solo bulk con `orders=[]`,
`order_i_ds=[]`, `client_order_i_ds=[id propio]` y TTL de 5.000 ms. Una respuesta
perdida se recupera consultando los tres IDs, nunca con otro POST. Un acuse no
permite reducir si los contratos siguen activos. El contrato oficial define la
[cancelación de un grupo por uno de sus miembros](https://api-docs.grvt.io/schemas/api_bulk_orders_request/).
Se corrigieron también los nombres de esas listas en la creación MAINNET.

Antes de cada IOC se exigen dos snapshots de contratos terminales, fills, seis
historiales y cuenta. La cantidad restante debe coincidir exactamente con la
posición observada. Se conserva el último cambio material de una orden, además
de la última lectura: volver a leer un estado idéntico no invalida por sí solo
la separación de cinco segundos. Un parcial nuevo sí exige dos snapshots nuevos.

Los snapshots tienen alcance `REDUCTION_ONLY_ACCOUNT_MATCH`: el PNL parcial
reportado se contrasta con el saldo, pero no se declara como rentabilidad
independientemente verificada. Se reduce exposición; no se libera capital ni
se certifica beneficio hasta cerrar por completo y calcularlo desde todos los
nocionales, comisiones y financiación.

`grvt_mainnet_reduction_order.py` construye un contrato IMMEDIATE_OR_CANCEL,
reduce-only, sin builder fee, en chain 325. La firma caduca en 5–20 segundos al
prepararla y nunca supera el plazo de recuperación. El tamaño es el residuo
verificado; el precio usa bid/ask actuales y redondeo que no amplía slippage.
Se comprueban tick, precisión, mínimos del proveedor y tope nocional de la orden
y cotización. Un residuo inferior al mínimo no se redondea al alza: queda para
revisión; no se presupone una exención reduce-only sin comprobarla en GRVT.

`grvt_mainnet_reduction.py` reserva un CID global y un intento antes del POST.
Firma con el mismo firmante de la entrada y autentica la misma subcuenta.
Separa claim, acuse y estado observado. La respuesta perdida y el reinicio no
permiten volver a emitir el intento. Después de un parcial IOC se recalcula el
residuo; el siguiente intento necesita evidencia nueva y presupuesto de intentos.
Un STOP o una revocación RAM no convierten el archivo en permiso de envío.

Las auditorías v2 enlazan el snapshot de recuperación y las IOC firmadas con el
grupo original. El recibo v2 sólo acepta exposición cero y saldo exacto. Los
recibos nativos v1 conservan su formato y verificación. La liberación sigue
siendo transaccional y no quita STOP ni importa resultados al libro REAL.

Tres tablas nuevas conservan petición, cadena de eventos y reservas IOC:
`grvt_mainnet_recoveries`, `grvt_mainnet_recovery_events` y
`grvt_mainnet_reductions`. Las reservas de CID se cotejan entre entradas y
reducciones. La migración v51 preserva recibos sellados y no crea autoridad.

`grvt_mainnet_remote_order.py` admite sólo diferencias de representación del
mismo contrato, como ceros decimales o casing del firmante/hex. Se reconstruye
la representación revisada, se comprueba la firma y se coteja el digest original.
No cambia cantidades, precios, dirección, vencimiento ni cadena, y no modifica
los hashes de archivos anteriores. Un cambio real sigue bloqueando el cotejo.

Estado de validación: HTTP privado controlado, criptografía y SQLCipher reales;
no se han probado órdenes con una cuenta Trade del proveedor. Siguen pendientes
piloto del proveedor y la validación real del circuito de fondos
wallet/exchange/retirada. La renovación de cierres vencidos o agotados se
incorpora en v55 con una revisión independiente. El coordinador del bot real
se incorpora en v54; su autoridad y revisión se documentan por separado.


La recepción del cancelado admite Ack `true` booleano y el texto `"true"`
mostrado en los ejemplos de la [API oficial](https://api-docs.grvt.io/trading_api/).
Se normaliza sólo ese acuse; cualquiera de los dos falsos sigue sin confirmar
recepción. Ninguna representación sustituye la lectura de contratos terminales.


## API e interfaz local — esquema v52

`grvt_mainnet_session.py` conserva claves sólo en RAM durante una sesión de
hasta 15 minutos. La conexión está ligada al token REAL, generación de bóveda,
subcuenta e identidad autenticada. Se usa una API key generada en la Trading
Account de GRVT y su firma dedicada; una clave View no concede permiso Trade.
No se acepta una URL remota ni una red seleccionable por el cuerpo HTTP.

`/real/exchanges/grvt/mainnet` expone historial paginado; `connect` y
`disconnect` gestionan la sesión, `sizing` calcula con Decimal y `preview`
prepara una revisión de términos exactos, HMAC y caducidad de 120 s. Sólo
`send`, con revisión original, revisión de permiso vigente y confirmación
explícita, consume esa revisión y permite un único intento. Un error o una
respuesta perdida no renueva la revisión: se consulta el ID ya conocido.

`groups/{parent}/live` consulta contratos y puede conciliar una salida nativa
completa desde dos auditorías estables. No cancela órdenes ni envía IOC.
`groups/{parent}/recover` necesita una autorización explícita de cierre:
cancelación única, observación de contratos terminales, dos snapshots estables,
IOC reduce-only del residuo y conciliación final. El trabajo termina al vencer
su plazo, agotar intentos, perder sesión o requerir revisión; no se reanuda
al arrancar ni amplía un permiso anterior. La solicitud repetida durante el
trabajo se rechaza. No se calcula beneficio verificado desde un simple acuse.

`stop` revoca firma/revisión/trabajos antes de persistir STOP. Mantiene la
lectura durante la sesión para inspeccionar posiciones existentes; **no las
cierra por sí solo**. Reconectar no borra STOP. `rearm` exige firma reconectada,
revisión exacta de STOP, reserva liberada y todos los grupos conciliados.
El STOP originado por un estado de ejecución que requiere revisión también
se conserva después del recibo hasta rearmarlo expresamente en la operativa
manual. Un bot v54 sólo puede retirar su propio STOP por observación una vez
conciliado el ciclo; nunca retira un STOP manual o desconocido.
Sólo habilita preparar una nueva revisión, nunca envía una orden.

La interfaz borra los secretos al pulsar Conectar, invalida revisiones al
cambiar términos y exige autorización separada para entrada y cierre. Los
polls pasivos no prolongan el desbloqueo. Bloqueo, vista protegida y reautenticación
revocan la sesión MAINNET. El historial cifrado permanece; secretos y permisos
no se restauran. La vigencia de SL/TP no es un cierre automático: una posición
residual al caducar necesita revisión/cierre explícito.


## Renovación explícita de recuperación — v55

Una recuperación vencida o con sus intentos agotados puede preparar otra
concesión desde «Renovar cierre». No modifica la petición inicial y no restaura
claves. La renovación v55 conserva salida/spread/slippage; la revisión v57
descrita debajo admite cambiar expresamente el tope de salida. Requiere conexión de firma
vigente de la misma entrada, bot detenido y ningún cierre local en ejecución.
La revisión de un uso (120 s) fija grupo, hash de concesión anterior, ancla de
eventos, plazo de 30–300 s y 1–3 intentos nuevos. Cambiar datos invalida la revisión;
STOP, bloqueo, otra sesión y origen externo impiden aplicarla.

`RECOVERY_GRANT` se añade a la cadena existente con esquema v55. Conserva el
hash de la petición inicial y enlaza el permiso efectivo anterior. Cada concesión
registra su primer intento y techo acumulado; los IOC mantienen numeración e IDs
únicos y todos los fills de intentos anteriores participan en la conciliación.
La cancelación sólo se puede reclamar una vez por concesión; renovar no repite
un POST automáticamente. La concesión archivada mantiene `authorization=false`.

La revisión consulta contratos e IOC anteriores. Una IOC sin estado terminal
impide renovar, incluso tras vencer su firma. Cada intento nuevo exige dos
snapshots financieros posteriores a su concesión; no reutiliza los anteriores.
Sólo el residuo cotejado puede enviarse como reduce-only. Una respuesta local
perdida se consulta por el mismo grupo y nunca repite la renovación. Los backups
repiten la cadena y rechazan cambios de límite aun con hashes recalculados.
La migración v54→v55 usa copia cifrada y no añade concesiones ni autoridad.

Los residuos bajo mínimos siguen requiriendo revisión externa. Un cierre que
supere el tope original puede revisar otro tope mediante el flujo v57 siguiente.
La API e interfaz se verifican con HTTP externo controlado, sin operaciones
privadas del proveedor ni uso de claves o fondos del usuario.


## Revisión del tope de salida — v57

«Límites de cierre» permite revisar un tope diferente si el precio actual hace
insuficiente el anterior, aun sin haber agotado plazo o intentos. «Renovar cierre»
ofrece el mismo campo cuando ya están agotados. El importe se introduce como
decimal exacto, debe ser positivo y no puede superar 10.000 USDT. La revisión
muestra tope anterior/nuevo y exige otra confirmación; cambiar el formulario la
invalida. El bot no puede ampliar sus límites automáticamente.

La concesión `orbis.grvt.mainnet.recovery.grant.v2` enlaza por hash la anterior y
registra `previous_exit_cap` y motivo `EXIT_LIMIT_CHANGE` o `EXHAUSTED`. La entrada
firmada y la petición inicial conservan sus topes. Sólo las IOC posteriores a la
concesión utilizan el nuevo importe; spread, slippage, firma original, restricción
reduce-only, dos snapshots nuevos y rechazo de IOC anteriores sin resolver siguen
siendo obligatorios. La migración v56→v57 no crea concesiones ni restaura autoridad.

Si SQLCipher está ocupado y se presenta la última instantánea del bot, el estado
RAM del cierre manual vigente prevalece sobre el estado antiguo del bot, también
al terminar. La instantánea financiera sigue siendo la última verificada, con
`pending=true`; un trabajo completado no inventa por sí solo un recibo financiero.

Las lecturas locales de grupo, historial y sugerencias tienen un plazo de 15 s.
Una consulta sin respuesta se cancela y libera los controles para que el sondeo
vuelva a consultar el registro; no reenvía una renovación ni una orden. Bloquear
o desmontar el panel cancela también las lecturas pendientes.


## Ejecuciones en el gráfico — v56

«Ver gráfico» inspecciona sólo fills propios verificados del grupo, sin conectar
una clave privada ni consultar al proveedor para recuperar los fills guardados.
La API paginada exige sesión REAL local; fija una revisión de la instantánea y
rechaza páginas de otra revisión, duplicados y referencias de otro grupo.
Precios/cantidades/comisiones/nocionales se entregan como cadenas decimales;
sólo las coordenadas visuales se convierten a números JavaScript.

La entrada del bot se conserva en `grvt_mainnet_entry_fills` al verificar una
entrada FOK completamente ejecutada. El registro enlaza claim y observación,
fills completos, tamaño/nocional y último fill. Se guarda antes de presentar
WATCHING; no utiliza el precio límite como si fuera fill. Es una evidencia de
entrada, sin autoridad ni rentabilidad conciliada. Backups verifican cantidad,
notional, cronología y hashes. v55→v56 crea la tabla en copia cifrada y no
transforma órdenes firmadas en ejecuciones ni rellena históricos inexistentes.

Para cierres o parciales se utilizan los historiales ya conservados y verificados
en auditorías/snapshots del grupo. Las salidas archivadas anteriores a v56 pueden
aportar la entrada validada sin fabricar una nueva fila. Sólo un recibo financiero
verificado permite mostrar resultado neto conciliado. «Fills observados» y
«Conciliado» son estados distintos; un acuse no crea una marca.

Las velas provienen exclusivamente del mercado público GRVT MAINNET del mismo
instrumento. El usuario puede cambiar intervalo, rango/fechas, zoom y cargar
histórico anterior con los controles reutilizados. Si falta OHLC, se conserva el
fill como punto real y se indica la falta de cobertura. Otra procedencia de velas
se rechaza. Bloquear/ocultar el patrimonio destruye la instancia del gráfico.
La implementación usa el bundle local Lightweight Charts 5.0.9, sin instalar ni
suponer typings de node_modules; sus exports se comprueban en el navegador.

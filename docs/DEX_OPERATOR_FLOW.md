# Operador DEX — recorrido en integración

Decisión de trabajo, 4 de noviembre de 2026. El asistente del propietario v65
está verificado. La generación y las copias de operadora ya están implementadas
en v66 y verificadas mediante API y navegador. La financiación está verificada
en v67; el enlace de firma, diario causal y trabajador están implementados en
desarrollo hasta v70. Sus controles de activación API/UI siguen pendientes;
este documento conserva también sus requisitos. No hay activación automática.

## Recorrido

1. Conectar y verificar la wallet propietaria en BNB Chain.
2. Crear una cuenta operadora dedicada en memoria. Descargar y comprobar su
   copia cifrada antes de financiarla. Nunca pedir la clave o semilla principal.
3. Configurar desde la wallet la política del ejecutor con esa cuenta pública.
4. Vincular la estrategia probada y su presupuesto de gas a la política finalizada.
5. Revisar y confirmar en la wallet una transferencia BNB acotada para el gas
   de esa operadora. Registrar recibo e importe real; no acreditar el presupuesto
   teórico como saldo recibido.
6. Conectar la firma temporal y activar expresamente el bot. Mostrar estado,
   próxima evaluación, decisiones, posiciones propias y gas disponible.
7. Detener: retirar la capacidad local de nuevas entradas y gestionar STOP
   según el estado del envío. No confundir retirada local, STOP en cadena,
   liquidación de una posición y conciliación de su resultado.

El ejecutor devuelve las salidas del swap a la wallet propietaria. El capital
USDT/WBNB no necesita depositarse en la operadora, cuya función es firmar y
pagar gas. La cuenta operadora y el gasto del propietario son registros distintos.

## Clave y copia

- Generar una clave nueva; no ofrecer un campo de importación de claves en claro.
  El borrador es temporal, vinculado a cuenta REAL, proof EOA y generación de bóveda.
- Guardar la clave activa sólo en memoria. Borrar el borrador al caducar, sustituirlo
  o bloquear la bóveda; una respuesta tardía no puede reconectarlo.
- Exportar un archivo cifrado para el usuario, con formato de recuperación
  identificable de Orbis y keystore Ethereum v3 dentro. No escribir secretos
  descifrados en SQLite, localStorage, sessionStorage ni diagnósticos.
- Restaurar sólo ese formato, con tamaño y parámetros KDF acotados antes de
  descifrar. Rechazar archivos externos arbitrarios y no ejecutar su contenido.
- Comprobar una recuperación del archivo antes de marcar la copia como válida.
  Una casilla sin esa prueba no demuestra que el usuario podrá recuperar la cuenta.
- El núcleo existente `DexOperatorSession` sigue siendo el único firmante de
  llamadas compiladas. Conectar no activa ni repone un presupuesto; otra cuenta
  o revisión de política invalida el permiso anterior.

La dependencia instalada `eth-account 0.14.0` ya ofrece `Account.encrypt` y
`Account.decrypt`. Se verificaron sus firmas localmente y la documentación
oficial. El KDF debe fijarse expresamente; no depender de una variable de entorno.
El trabajo criptográfico se hará fuera de los cerrojos REAL/bóveda, con exclusión
para evitar importaciones concurrentes costosas y revalidación de sesión al
terminar. Se midieron 0,344 s de cifrado, 0,333 s de descifrado y 298,8 MiB de RSS
pico del proceso en la máquina de desarrollo. Son observaciones locales; no un
compromiso de latencia para otros equipos.
[Referencia de la biblioteca](https://eth-account.readthedocs.io/en/latest/eth_account.html).

## Transferencia de gas

Implementada en v67: «Gas · BNB» prepara una transferencia nativa para un vínculo
guardado. Exige la operadora vigente cuya copia cifrada se comprobó, la política
actual en bloque finalizado, presupuesto restante y saldo del propietario. Se
vuelve a comprobar todo antes de conceder el único envío desde la wallet.

El diario `real_dex_gas_funding_intents` (esquema 63) conserva importes enteros en
wei y referencias HMAC, sin direcciones, firmas o hashes de transacción en claro.
Comparte la exclusión de nonce con MEXC, swaps, despliegue y controles del ejecutor.
Una respuesta perdida bloquea otro envío hasta observar el nonce consumido; eso
no acredita el importe. Sólo el recibo firmado, enlazado y finalizado acredita
BNB transferido y comisión real. El recibo puede archivarse tras caducar/retirar
la operadora o cambiar la política; no depende de conservar su clave.

La API privada `/real/accounts/{id}/dex-gas-funding` ofrece historial,
preparación, estado, concesión, registro de hash/rechazo y observación. Sólo lee
RPC público; la confirmación de la wallet es quien envía. Las lecturas de wallet
del panel caducan a los 8 s; una solicitud de envío sin respuesta permite consultar
el historial y comprobar el original, sin conceder un segundo envío.

El importe transferido se conserva como capital interno observado. La comisión
del propietario queda en el recibo, separada del gas consumido por el operador.
No se crean asientos de compra, beneficio o gasto automático en la cartera manual.
La financiación no conecta el firmante ni activa un trabajador.

La preparación debe fijar cuenta, vínculo, ejecutor, wallet propietaria,
operadora autorizada, revisión de política, cantidad BNB exacta, nonce y comisión
máxima. Sólo la wallet propietaria puede confirmar el envío. No es una API de
transferencias a cualquier destino.

La concesión debe participar en la guarda común de nonces. Un resultado incierto
no habilita un segundo envío. El recibo debe acreditar remitente, destinatario,
valor, firma, red, nonce, bloque finalizado y gas. El importe transferido no es
beneficio, compra de WBNB ni gasto de gas ya consumido: permanece como saldo BNB
externo observado de la operadora.

Un depósito externo directo también puede servir, tras observar el saldo real.
Financiar no conecta una clave, no inicia el bot y no permite superar el
presupuesto duradero. La reserva STOP sigue separada del gas disponible para
intercambios.

## Activación y decisiones

Revalidar fuente, vínculo, política, permisos, gas y cuenta operadora antes de
activar. Crear un registro durable de activación y conservar decisiones causales
con su vela cerrada, versión y estado previo. El diario debe comprobar esa
procedencia al reservar y al conceder una emisión; no basta una referencia libre.

No adoptar firmas o intentos anteriores a la vinculación. No autorarrancar tras
reiniciar. Un rearme debe conservar posiciones propias, costes, incertidumbres
pendientes y presupuesto consumido; no reutilizar una política para borrar pérdidas.

Los estados del recorrido deben distinguir preparación, envío incierto,
seguimiento activo, parada local, STOP confirmado y revisión necesaria. PAPER y
REAL conservan capitales, ejecuciones y resultados separados.

## Pruebas de aceptación siguientes

- Generar, exportar, restaurar y caducar la cuenta sin exponer secretos; rechazar
  una contraseña errónea, un KDF excesivo y una sesión revocada durante el trabajo.
- Transferencia BNB local EVM firmada por la wallet, con rechazo, respuesta perdida,
  replay, sustitución y recibo revertido; sin duplicar importe ni nonce.
- Una sesión conectada puede firmar únicamente el plan del vínculo/política actual.
  Reconectar no repone presupuesto. STOP conserva su reserva.
- Una decisión LONG/DCA posterior a la activación produce una única operación;
  se inspeccionan la señal, entrada, salida, comisiones y resultado reales.
- Bloqueo, reinicio y reapertura conservan el diario; no recuperan autoridad
  automáticamente. Se muestran y resuelven intervalos sin observación.
- Recorrido completo con proveedores/cadena controlados antes de un piloto humano.

## Enlace interno del firmante — v68 en desarrollo

`DexOperatorConnection` acepta sólo una preparación verificada y el estado
actual del vínculo/concesión ya guardados. El presupuesto de firma es como
máximo el menor entre gas autorizado restante y saldo BNB observado, con reserva
STOP. No repone el diario ni adopta intenciones anteriores sin resolver.
El replay de una conexión conserva su caducidad; una solicitud terminada no
reinstala silenciosamente la clave. La preparación y el firmante comparten
revocación mediante referencias débiles y una revisión específica de la sesión.
El ciclo de vida REAL retira el enlace junto a las dos copias RAM de la clave.

Esta capa no concede por sí sola intercambios a un vínculo. v69 incorpora la
prueba causal persistente de `require_active_binding_decision`, comprobada al
reservar y al emitir, con fuente versionada, velas cerradas, posición/coste y
límites reales. Un estado guardado sin trabajador vivo se
presentará como rearme necesario. Cambiar la política del contrato conserva la
posición/coste pero reinicia contadores de concesión; el rearme debe conservar
además el gasto y pérdidas acumulados del bot para impedir su reposición implícita.

## Decisiones causales LONG — v69 / esquema64 en desarrollo

Las sesiones y decisiones SQLCipher se enlazan con la fuente LAB/PAPER probada,
la concesión existente y la firma revocable. La semilla sólo calienta las reglas:
la primera entrada exige otra vela cerrada después de la activación. Un hueco de
observación/cobertura pausa el plan; no se compran señales retrospectivas.

El planificador puro reproduce LAB con todo el histórico disponible y registra
su evidencia. Usa referencia pública MEXC BNB/USDT para señales, stop/objetivo y
trailing; esto no es el precio recibido en Uniswap. Las salidas usan cantidades
propias y coste de recibos DEX, con continuidad de la intención de cierre cuando
un límite exige vender por partes. Tras cerrar no vuelve a entrar en la misma vela.
El beneficio real se deriva exclusivamente de los recibos; las comisiones nativas
se conservan aparte y no se inventa una conversión neta USDT.

Cada ejecución debe coincidir exactamente con la última decisión activa: acción,
importe, estado observado, cotización simulada, nonce, costes y plazo. Se valida
al reservar, registrar la firma y justo antes de conceder el único POST. La parada
local del run bloquea continuaciones ya preparadas; no implica vender posiciones
ni emitir un STOP de contrato. El controlador deberá retirar además la firma.

Una sesión ACTIVE en disco carece de trabajador por sí sola. La reapertura sólo
recupera información y no devuelve el claim RAM perdido ni reautoriza órdenes.
DCA, rearme, API/UI y optimización de replay aún se integrarán;
este núcleo no se presenta en la interfaz como un bot activado.

## Trabajador continuo — v70 en desarrollo

`DexBotWorker` consume la referencia pública MEXC y sólo una nueva vela cerrada
continua. Comprueba las dos velas anteriores para detectar revisiones. El estado
del ejecutor, la cuota de salida parcial y la simulación se fijan al mismo
bloque; las lecturas de estado y cotización usan clientes acotados separados
para respetar el máximo de 32 consultas, sin ampliar los límites del RPC.

Cada ciclo previsualiza y vuelve a validar la decisión al escribirla. Una compra
o venta requiere después la receta exacta, reserva durable, firma de la revisión
actual y una única concesión de emisión. Esa concesión comparte un cerrojo corto
con la parada: STOP anterior impide emitir; una emisión ya concedida sigue en
curso y puede confirmarse después de retirar la firma. El POST no sostiene ese
cerrojo, la bóveda ni el firmante.

El hash original sólo vive en RAM. Una respuesta perdida retira permiso y pausa
nuevas operaciones, conserva el intento y busca únicamente ese recibo. No se
reenvía ni se deduce una ejecución del nonce. Antes de la siguiente decisión se
exige el recibo finalizado, validado por el diario. Tras parar se permite observar
un envío en curso durante un máximo de dos minutos; al cerrar el proceso no se
reconstruye el hash ni se autoriza un reenvío. La resolución posterior del envío
incierto requiere recuperar su hash por el recorrido de conciliación.

Se abandonan únicamente reservas sin concesión de emisión. Si falla el archivo
de la parada, la firma se retira igualmente y el estado marca revisión necesaria;
no asegura una escritura que no ocurrió. Un trabajador viejo no puede revocar
una revisión de firma posterior.

`DexBotSupervisor` mantiene el registro de hebras en memoria. Construirlo o leer
una sesión ACTIVE guardada no inicia nada. Sin la hebra y su concesión original
se muestra `REARM_REQUIRED`. El arranque es explícito y único; la parada es de la
cuenta propietaria y no espera consultas de mercado o POST bloqueados.

En v70 este controlador aún no estaba conectado a `RealAccess`, rutas de
activación o interfaz. La integración posterior se describe a continuación.

## Activación desde la aplicación — v71 verificada y cargada

Wallet → Bot DEX → «Bot · Activar» permite elegir un vínculo LONG preparado,
revisar sus límites, vigencia y gas y autorizar órdenes automáticas. No sustituye
la política, aprobaciones ni financiación de gas confirmadas previamente. La
wallet de control debe estar verificada en BNB Chain; la firma dedicada permanece
exclusivamente en el servidor local, sin clave o claim enviados al navegador.

La API privada `/real/accounts/{id}/dex-bots/runs` ofrece historial, detalle,
activación, parada y comprobación de recibos originales. La huella inmutable de
solicitud ata vínculo, preparación, vigencia y opciones: repetir el ID devuelve
el estado existente, sin conectar la firma ni iniciar otra hebra; cambiar los
parámetros con ese ID se rechaza. La huella es opcional para conservar los
archivos causales internos anteriores, sin nueva migración de esquema.

La preparación inicial descarga sólo datos públicos y completa el histórico
con solapes verificados. No opera durante ese calentamiento. El arranque exige
una nueva vela posterior. Los RPC/HTTP se hacen fuera de los cerrojos REAL y
bóveda; al volver se revalidan token, proof EOA, generación y cancelación.
Una cancelación puede llegar antes que la petición original o durante la consulta:
el registro RAM impide que una respuesta tardía arranque el bot.

«Detener» funciona sin wallet conectada. En un trabajador conocido se comprueba
su cuenta en RAM y se retira la firma antes de leer el archivo; si éste falla se
confirma la parada local con estado durable desconocido y revisión necesaria.
La interfaz no conserva en ese caso una etiqueta «Activo» ni presenta la posición
como verificada. La parada local no vende ni cancela una transacción ya emitida.

La vista muestra posición/coste derivados sólo de recibos, compras/ventas
confirmadas y gas nativo separado. Ante respuesta perdida y reapertura se puede
aportar el hash original: se coteja su referencia, transacción, firma, contrato y
recibo finalizado antes de actualizar posición. Comprobar no autoriza ni reenvía.
El hash no se guarda en claro ni en almacenamiento del navegador.

El ciclo de vida REAL retira el trabajador al bloquear, proteger la vista,
reautenticarse o cerrar el servidor. El sondeo pasivo no renueva el desbloqueo
del navegador o del backend. Una sesión guardada no se autorarranca.

**Límites actuales:** LONG, vigencia máxima de 15 minutos y bóveda desbloqueada.
El rearme tras operar se identifica como pendiente y se deshabilita en el
selector. DCA sólo mantiene preparación/vinculación; su activación no se ofrece.
Permiso prolongado, rearme conservando pérdidas/gasto/posición, DCA causal,
devolución de BNB residual, marcadores DEX e integración contable general siguen
pendientes. No se afirma un piloto humano en mainnet ni finalización global.

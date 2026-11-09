# Emisión de cancelaciones nativas — v81 cargada

v81 está verificada y cargada en el servidor local. Añade emisión de cancelaciones
de devoluciones BNB al verificador de reemplazos finalizados de v80. La
cancelación wallet → operadora sigue pendiente. QA utiliza únicamente bóvedas,
firmas y fondos propios de EVM desechables, sin operaciones mainnet.

## Decisiones de implementación

- Esquema cifrado 69: `real_dex_gas_cancellation_intents`, formato nuevo
  `orbis.dex.gas.cancellation.v1`. No modifica los formatos publicados de
  devolución, resolución nativa o contabilidad. Plan y estados sellados con HMAC,
  vínculo al prefijo BCAST original y sin direcciones, claves, hashes completos
  ni bytes firmados persistidos. Migración con preimagen cifrada verificada.
- `CancellationPlan` exige BNB Chain, EOA propietaria/operadora distintas, valor
  cero, destino propio, calldata vacía, nonce original y exactamente 21.000 gas.
  El firmador previo de devoluciones mantiene su requisito de valor positivo.
- Precio mínimo: mayor entre `base + 1` y `ceil(base × (100 + aumento) / 100)`.
  La base es el máximo entre el precio original y todos los intentos de
  cancelación con concesión de POST previa. Coste entero wei y límite revisado.
  Se contrastan saldo y nonce finalizado/último/pendiente y se relee el nonce
  actual. Una cola que avance más de un nonce no autoriza la cancelación.
- Una reserva viva por devolución. Retirar una revisión PREPARED y descartar
  RESERVED/SIGNED nunca liberan el nonce del original. Tras BCAST no se descarta
  el envío incierto. La continuación es no copiable y consume una concesión
  durable antes del único POST; no hay firma ni rePOST al recuperar la app.
- Después de un POST incierto puede revisarse **otro** intento explícito al
  mismo nonce, con precio superior a todos los concedidos. Tiene otro ID y su
  propia confirmación/coste. No es un reintento automático ni cambia de nonce.
- El transporte relee EOA, saldo y nonce antes de conceder el POST. La red nunca
  cruza cerrojos privados ni SQL. Cambios de sesión, copia, generación, parada,
  expiración o abandono impiden una concesión tardía. Una carrera de cadena
  después de la concesión puede rechazar la cancelación: sigue siendo incierta
  hasta contrastar el recibo.
- Si gana el original, se verifica con gas-return v1 y se conserva su efecto.
  Si gana una cancelación concedida, se verifica y archiva con native-resolution
  v1, sin reescribir el original. El recibo y el cierre del intento se guardan
  en una misma transacción SQL. Sólo el ganador es fuente de gasto. Los intentos
  anteriores apuntan a ese ganador y la interfaz evita repetir su comisión.
  Un reemplazo ajeno a los intentos concedidos necesita la revisión v80 explícita.
- Endpoints privados bajo `/real/accounts/{id}/dex-gas-cancellations`: GET de
  historial/detalle, POST de revisión, `/{id}/submit`, `observe`, `cancel` y
  `abandon`. Body estricto, sesión, origen, prueba EOA y no-store; sin RPC URLs,
  claves, transacciones o evidencia aportadas por el cliente. `submit` requiere
  `CANCELAR_DEVOLUCION_BNB`, la revisión exacta y la copia verificada.
- UI integrada en «Gas · Recuperar BNB»: «Cancelar devolución», revisión de gas,
  confirmación separada, observación y nuevo intento explícito. Hashes sólo en
  RAM; cambio de wallet/sesión borra revisiones. Una respuesta perdida se
  recupera con «Actualizar devoluciones», sin repetir el POST.

## Evidencia focalizada

Antes de la última presentación del historial aprobaron 32 casos nuevos:
3 de plan/firma, 9 de diario/migración, 8 de outbox, 8 de transporte y 4 HTTP.
No son la suite general completa. El navegador completó POST sin llegada,
nueva revisión más cara al mismo nonce, llegada real, respuesta HTTP perdida,
GET, enlace al único ganador, reapertura sin autorun, siguiente devolución y
una única contabilización del recibo de cancelación. Se ajustó después sólo la
etiqueta del intento perdedor para no mostrar su comisión otra vez.

La suite completa aprobó **3.840 casos / 339 archivos y 26 recorridos de
navegador**. Recompilación y 26 pruebas del contrato aprobadas. Después de esa
tanda se corrigió el formulario de recuperación: debe recuperar precio,
aumento y máximo originales, además del digest/coste. La misma prueba que
fallaba antes aprobó después; se repitieron los tres navegadores afectados.
Sólo cambiaron ese archivo JS y su prueba de navegador; todo el backend, pruebas
unitarias y contratos siguen idénticos a la tanda completa. El informe conserva
ambas procedencias y no declara una segunda suite general.
Publicación: `/home/rich/.local/state/orbis/verification/v81-cancellation-ui-r3/verification.json`.
PID 322010, 30 rutas privadas anónimas 401, bóveda cerrada y sin operativa.

Para estas carreras se añadió la dependencia de pruebas fijada
`@foundry-rs/anvil@1.7.1`. La EVM Ganache 7.9.2 del fixture admitía ambas firmas
con el mismo nonce al reanudar el minado; se conservó su reproducción en
`v81-cancellation-dev/ganache-pending-probe.log` y no se usa como evidencia de
sustitución. Anvil recibe firmas reales de las cuentas efímeras identificadas,
sin impersonación, fork ni acceso a fondos externos. El adaptador de pruebas
iguala `finalized` a `latest` para finalidad inmediata de desarrollo: demuestra
ejecución y mempool, no consenso mainnet. Los otros fixtures siguen en Ganache.

## Base verificada

El código público actual de BSC configura un aumento inicial del 10 % para
sustituir operaciones en su pool legacy, pero permite modificarlo en el nodo.
Su inserción comprueba tanto el límite de comisión como la propina de la
transacción anterior. Por tanto, no basta con aumentar sólo una de ellas y no
puede prometerse aceptación en cualquier proveedor.
Fuentes: [configuración BSC](https://github.com/bnb-chain/bsc/blob/master/core/txpool/legacypool/legacypool.go)
y [sustitución en el pool](https://github.com/bnb-chain/bsc/blob/master/core/txpool/legacypool/list.go).

La documentación de [MetaMask](https://support.metamask.io/manage-crypto/transactions/how-to-speed-up-or-cancel-a-pending-transaction)
también distingue un intento de cancelar una operación pendiente de una
cancelación ya confirmada. La finalidad de un recibo seguirá siendo el criterio
de ORBIS para liberar el bloqueo, mediante el verificador v80.

## Primer recorrido: operadora → propietario

La devolución original tiene firma propia exacta, nonce y precio conocidos en
su diario. Primero abordar su cancelación: el usuario no suele tener esa
operadora abierta en MetaMask. El firmador de devoluciones actual exige valor
positivo al propietario y debe conservar esa restricción.

1. Usuario elige una devolución incierta y restaura la misma copia de operadora.
2. Comprobar propietario, sesión, generación, copia verificada, bots detenidos,
   conexión de trading retirada y ausencia de otro intento de cancelación vivo.
3. Leer cadena, bloque finalizado, último/pending nonce, saldo y código EOA.
   Consultar el original cuando se disponga de su hash y contrastar su identidad
   HMAC y bytes firmados. Un hash ajeno no sustituye a esa prueba.
4. Si existe recibo, conciliarlo; no emitir una cancelación de algo ya minado.
   Si el nonce ya se consumió sin conocer el efecto, conservar revisión pendiente.
5. Preparar valor cero, calldata vacía y destino propio inmutable; nonce exacto
   del original y 21.000 unidades de gas. No permitir dirección, valor o llamada
   arbitrarios. Precio superior al anterior dentro de un máximo revisado, con
   enteros wei y política de aumento explícita/configurable.
6. Mostrar coste máximo y confirmar por separado. La cancelación no vende,
   devuelve capital ni reactiva el bot. Su gasto no repone presupuesto del bot.
7. Reservar atómicamente una única cancelación para el origen, sin liberar su
   reserva original para otras acciones. Leer de nuevo antes de conceder firma.
8. Firmador específico y continuación no copiable: clave y bytes sólo en RAM,
   revisión/generación vigentes y concesión durable anterior al único POST.
   Revalidar bloqueo, revocación, copia y sesión inmediatamente antes de concederlo.
9. Red fuera de cerrojos y SQL. Error, timeout, respuesta perdida o hash distinto
   conservan incertidumbre. No rePOST, reenvío ni aumento automático de gas.
10. Verificar el recibo que ganó la carrera. Si ganó el original, conservar su
    efecto real; si ganó la cancelación, resolver el origen y contabilizar sólo
    el gas del ganador. No atribuir un gasto a ambos hashes del mismo nonce.

La autorización y el archivo necesitan entidades/formatos propios; no añadir
estados o cambiar el significado de gas-return v1, native-resolution v1 ni
bookings v1–v3. La resolución final se vinculará al verificador publicado, con
proveniencia independiente del intento de cancelación y sin guardar hashes
completos, firmas, claves o direcciones en la base de datos.

## Wallet → operadora

La firma corresponde a la wallet. No asumir que su precio real coincide con el
solicitado: la wallet puede haberlo cambiado. Si se conoce el hash, comprobar la
transacción firmada para revisar el aumento. Si falta, no presentar una tarifa
inferida como garantía de sustituirla. Ese caso necesita un recorrido explícito
de revisión de incertidumbre. También debe fijar destino propio, cero, calldata
vacía, nonce del origen y una concesión única antes de invocar el proveedor.

## Aceptación

EVM propia con minado controlado: original pendiente, cancelación aceptada y
recibo; original gana antes de firmar o antes del POST; sustitución rechazada;
precio insuficiente; saldo insuficiente; nonce ajeno/consumido; bloqueo durante
lectura/firma/concesión; dos pestañas; respuesta de POST perdida con y sin
llegada; reapertura sin autorun; una única comisión en contabilidad. Comprobar
un nuevo envío normal con el nonce posterior sólo después de resolver el efecto.

No usar fondos del usuario para desarrollar o verificar este recorrido.

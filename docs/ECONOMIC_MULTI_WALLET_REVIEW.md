# Revisión contable de varias wallets

Estado: cargado y verificado en v114. Cobertura de 5.208 casos / 435 archivos
por identidad y 27 recorridos sobre la misma aplicación. La tanda inicial pasa
5.207 y detecta un montaje Git; se corrige sólo ese test y se repiten los 18
casos del módulo, conservando el fallo original. No es otra tanda completa
con cuatro salidas 0. Las 1.275 fuentes finales sólo difieren en ese test.
Los 123 focalizados (incluidos 19 nuevos) forman parte del total. Esquema 75,
sin nuevas migraciones ni cambios en ocho motores/puertos financieros. La carga
y la comprobación pasiva mantienen REAL cerrado y los seis archivos intactos.
Evidencia: `~/.local/state/orbis/verification/v114-economic-multi-wallet-r1/verification.json`.

La revisión se mantiene anclada a una wallet principal y al cierre transitivo
de cuentas unidas mediante traslados propios. Otras wallets pueden aportar sus
recibos a esa misma revisión. No se cambian los motores de cálculo, formatos de
aceptación, originales, autoridad operativa ni esquema SQL.

## Descubrimiento privado

`POST /real/accounts/{context}/economic-review/sources` admite `include_wallets:
true` y la lista transitoria `wallets: [{context_account_id, address}]`. Cada par
necesita su prueba EOA vigente en la misma sesión REAL. Probar una wallet en un
contexto no demuestra su control en otro contexto ni concede permiso de gasto.

El servidor comprueba que todas las wallets proporcionadas tengan una cartera
propia dentro del cierre. Rechaza wallets fuera del cierre, contextos sin probar,
listas duplicadas y límites excedidos. La petición anterior, sin activación,
conserva su respuesta anterior y su catálogo de una sola wallet.

La respuesta optativa añade `wallet_scope`, con esquema
`orbis.portfolio.economic.wallet.scope.v1`. Sus descriptores contienen ID local
del vínculo, cuenta, nombres y `provided`; no contienen direcciones ni sus HMAC.
`provided` significa que se aportó una dirección probada en la petición actual;
no acredita saldo ni permiso para operar. Los recibos de una wallet ausente no
se incorporan al catálogo.

`missing_receipts` identifica los recibos ya registrados que siguen necesitando
un testigo y una wallet. Los testigos de criterios históricos aceptados conservan
su validez sin reconectar la dirección antigua. No se pueden omitir recibos
necesarios del cierre en la aceptación, aunque alguien manipule la interfaz.

Los candidatos a reutilización se buscan en la cartera de cada wallet, evitando
atribuir a una cuenta los asientos coincidentes de otra. Cada fila optativa
incluye `wallet_link_id` y `wallet_account_id`; conserva el contexto operativo
original y su estado de verificación independiente.

## Conexión explícita

La capacidad booleana `economic_review_wallets` habilita el selector. El usuario
elige un proveedor descubierto, solicita sus cuentas autorizadas y selecciona
una cuenta. No se elige la primera cuenta como identidad supuesta de la wallet.
Se comprueba BNB Chain y la cuenta autorizada antes y después de la firma EOA.
El catálogo autenticado debe confirmar que corresponde al vínculo elegido.

Las firmas son exclusivamente `personal_sign` del desafío local de control.
El asistente no solicita órdenes, transacciones ni permisos de tokens. Los
recibos envían al servidor la dirección correspondiente a cada wallet, y cada
contexto original pendiente se prueba por separado con su proveedor.

La wallet principal permanece como ancla del diálogo. Un cambio de proveedor,
cuenta, red, sesión o generación de esa ancla descarta la revisión. También se
vigilan `accountsChanged`, `chainChanged` y `disconnect` de los proveedores
adicionales. Cambiar un proveedor adicional descarta el diálogo y sus respuestas
tardías. Cerrar el diálogo retira los listeners y vacía el selector de wallets.
Una solicitud ya preparada puede permanecer en memoria hasta iniciar otra
revisión, cambiar contexto o bloquear la sesión; no se escribe en storage.
Un proveedor adicional debe permitir vigilar y retirar dichos listeners.

Conectar otra wallet invalida selecciones de recibos y opciones de orden para
que se revisen con el catálogo nuevo. Las direcciones no se guardan en storage
del navegador ni en la definición contable. Una confirmación perdida se consulta
por digest, sin reenviar una aceptación ni una operación económica.

## Verificación y límites

Las pruebas usan SQLCipher propio y recibos firmados de Anvil. Cubren wallets
con cuentas separadas, traslados propios, contexto de origen distinto de la
cartera, reutilización de criterios históricos, nueva wallet y aceptación
conjunta. No se utilizan claves, wallets, bases ni fondos del propietario.

El recorrido completo de socket abre la pantalla REAL, descubre dos proveedores
EIP-6963 y elige explícitamente la segunda cuenta autorizada del proveedor
adicional. Revisa tres recibos firmados, reutiliza asientos, comprueba unidades,
coste y gas con Decimal independiente, acepta cinco asientos nuevos de forma
conjunta y recupera la respuesta perdida sin repetir la aceptación. Bloquear y
reabrir conserva el criterio sin reconectar la dirección secundaria. Se
comprueban escritorio/móvil, ausencia de RPC de gasto y storage sin direcciones.
Dos casos adicionales de Chromium utilizan un puente a la API ASGI real y
carteras separadas con traslado propio, incluyendo reapertura de una copia
cifrada fría. Las pruebas de componente cubren cambios y respuestas tardías.

La primera preparación del recorrido completo detectó un reloj de fixture que
quedaba detrás de sus bloques minados. Se sincroniza con la cadena local; no se
relajan las comprobaciones temporales de la aplicación. Se conserva su informe
fallido y la repetición correcta en `economic-multi-wallet-main-ui-dev-r2`.

El proveedor debe exponer las cuentas que el usuario autorice. El asistente no
puede sustituir una cuenta oculta por una dirección supuesta ni abrir una wallet
móvil sin una integración de transporte. Los nombres EIP-6963 son informativos.
La integración no constituye un piloto mainnet ni completa el objetivo global.

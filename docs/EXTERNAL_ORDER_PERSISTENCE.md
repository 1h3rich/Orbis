# Persistencia del orden de una llamada externa

Estado: desarrollo, esquema REAL 77. La instancia original conserva v114 y su
código de esquema 75. Este tramo guarda hechos de orden; no acepta los efectos
contables, no cierra una estrategia y no crea permiso de trading.

## Registros y controles

`real_dex_external_order_proofs` es un registro inmutable, con FK único a la
observación externa. Tiene identidad determinista y unicidad de transacción,
firmante/nonce y posición de bloque. El payload lleva sello HMAC, digest de la
observación y certificado complementario versionado. Al leer se verifican otra
vez origen, contexto completo, actores, firma atestada, coordenadas y tiempos.
No necesita direcciones ni red para recuperar un certificado ya guardado.

Los tres perfiles de orden (trade 73, gas 75 y externo 77) comparten admisión y
verificación de archivo. Tanto el escritor nuevo como los anteriores rechazan
recibos/posiciones duplicados, nonces contradictorios y versiones distintas de
un bloque. El perfil externo conserva su esquema propio; no se almacena como
trade conocido ni como gas sin principal. Las coordenadas se guardan como texto
decimal canónico, sin convertirlas a INTEGER/float de SQLite.

La migración 76→77 crea la tabla sin reescribir observaciones, solicitudes ni
asientos. Conserva una preimagen cifrada; la validación rechaza registros nuevos
ocultos bajo un `user_version` anterior. Los fixtures antiguos eliminan también
la tabla nueva vacía antes de simular el origen, para reproducir el esquema real
de esas versiones sin referencias artificiales.

## Puerto privado

Base: `/real/accounts/{account_id}/dex-call-resolutions/{kind}/{identifier}/external-order`.

- `POST /preview`: dirección/hash transitorios, sesión local y control EOA del
  contexto exacto. Consulta el recibo y reconstruye la firma fuera del mutex y
  lease de REAL. Vuelve a comprobar generación, contexto y observación antes de
  devolver el digest revisable. No recibe certificados/evidencia del cliente.
- `POST /save`: confirmación `GUARDAR_ORDEN_EXTERNA` y digest exacto. Trabajador
  del operador inactivo, parada vigente, origen revalidado y admisión global bajo
  `BEGIN IMMEDIATE`. La sesión de lectura se cierra antes de abrir la de escritura.
- `GET` de la base: recuperación por origen, con token y cuenta correctos, sin
  dirección, RPC, clave de operador ni nueva prueba de wallet. No prolonga sesión.

El reintento de un guardado idéntico devuelve el registro existente sin otra
consulta ni escritura. Otro hash/digest/cuenta se rechaza. Avanzar el extremo
finalizado no invalida el mismo recibo. Bloquear o sustituir la sesión descarta
la respuesta tardía, también ante un fallo del proveedor. Los errores no revelan
el endpoint autenticado. Todas las respuestas son privadas y `no-store`.

El certificado tiene `saved=true`, pero mantiene `applied_to_ledger=false`,
`resolves_intent=false` y `trading_authority=false`. La observación continúa en
`EFFECTS_PENDING`; ni la concesión ni el nonce se liberan por completar este paso.

## Evidencia actual

- Primera tanda: 18 casos de cuatro módulos, con guardado concurrente, rollback,
  corrupción con sello exterior recalculado, copia cifrada fría, migración,
  autenticación y descarte tras bloqueo durante RPC/criptografía.
- Segunda tanda: cuatro casos. Incluye una operación de bot enlazado real de la
  EVM propia junto a una llamada externa de wallet en el mismo bloque, admitidas
  en ambos órdenes de escritura; además, migraciones de 73/74 al destino vigente.
- Recorrido privado adicional del operador: certificado firmado, reserva de
  trabajador rechazada antes/después del RPC, un solo registro, recuperación tras
  respuesta perdida y reapertura sin wallet ni clave de operador. Siete
  comprobaciones de orden, junto a once de observación; no son 18 tests pytest.
- Verificación integrada: **255 casos de 17 módulos**, dos salidas `[0,0]`,
  identidades contrastadas con la colección y fuentes congeladas sin cambios.
  Incluye **14 casos nuevos de dos módulos**; no se suman las tandas anteriores.

Evidencia: `~/.local/state/orbis/verification/external-order-storage-dev-r1/`,
`external-order-mixed-history-dev-r2/`, `external-order-operator-http-dev-r1/` y
`external-order-storage-integrated-dev-r3/`.

## Siguiente tramo

Autenticar todos los vínculos de cuentas/wallets y guardar declaración, nuevos
asientos o vínculos y criterio externo en un solo commit. Debe reproducirse en
frío y conservar el resultado manual como distinto del fill original. La
integración incluye nuevas operaciones conocidas después del criterio externo:
los planificadores de recibos y el linaje no pueden perder fuentes ni fallar al
encontrar claims externos previos. También faltan el puerto de resultado efectivo
para bots y el recorrido visual completo antes de publicar.

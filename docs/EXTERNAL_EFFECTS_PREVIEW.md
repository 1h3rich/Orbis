# Previsualización privada de efectos externos

La captura y propuesta descritas aquí se verificaron inicialmente en esquema 77.
La instancia publicada conserva v114. El desarrollo 78 añade la
[aceptación financiera atómica](EXTERNAL_FINANCIAL_ACCEPTANCE.md), en verificación.
La previsualización no guarda asientos y conserva `saved=false`; ahora informa
`acceptance_available=true` y `can_accept` según revisión y bloqueo operativo.
Ninguno de estos puertos concede autoridad para operar.

## Captura de custodia e historial

`economic_external_capture` autentica observación y certificado persistidos,
el contexto original y cada vínculo REAL de wallet. La API exige control EOA en
la sesión vigente para cada pareja dirección/contexto suministrada. Las direcciones
son transitorias: la captura separada sólo conserva referencias HMAC, IDs locales
y la base de custodia. No se crea una sal ni se consulta RPC durante esta revisión.

El contexto de una solicitud puede ser distinto del ledger de su wallet. Se usa
el vínculo local real; no un account_id arbitrario del cliente. Las wallets deben
estar en cuentas propias separadas de la cuenta vinculada a MEXC. No se permite
repetir una dirección con contextos diferentes para ampliar o confundir su custodia.

Un operador delegado ya atestado por el origen/concesión puede compartir la
custodia del propietario. En ese caso el gas y el principal pertenecen al mismo
ledger, y un retorno entre esos actores no inventa capital ni una compraventa.
Para usar un ledger separado del operador se exige además su EOA recién probada
y su vínculo local. El cliente no asigna roles: OWNER/SIGNER/PROVEN_WALLET se
derivan comparando las referencias con la observación autenticada.

Se captura la **unión** de los cierres originales de todas las wallets. Dos
wallets todavía sin un traslado registrado pueden participar en una pareja nueva;
consultar únicamente el cierre del propietario perdería el inventario previo de
la otra. También se capturan todos los recibos ya contabilizados y su gas, las
fuentes selladas anteriores y los criterios vigentes de cada componente. Nada
puede desaparecer al unir los historiales. Esta captura prepara el perfil externo;
no habilita un criterio 3 a través de los escritores anteriores.

## Propuesta de asientos

`economic_external_plan` recibe referencias a originales o propuestas tipadas.
Los IDs nuevos y sus request_id se derivan de un review_id y claves locales;
ambas piernas de una pareja comparten una clave explícita. El tiempo financiero
de los nuevos asientos lo fija el bloque observado. El tiempo de registro no
puede preceder al certificado ni a los originales capturados. La API rechaza
fechas de registro futuras.

Todas las unidades observadas, gas, permisos y cobertura desconocida pasan por
el núcleo de conciliación existente. Las nuevas parejas de tokens se calculan
primero con coste provisional cero y se normalizan con el coste real del inventario
previo. Se repite el cálculo sobre esos originales definitivos propuestos.
Los asientos existentes conservan sus bytes y sólo pueden recibir contexto
económico derivado en un criterio futuro.

Un índice de valores económicos y tiempos detecta originales semejantes sin
recorrer el historial completo por cada propuesta. Debe reutilizarse el original
o confirmarse expresamente otro movimiento. Diferencias en notas o en un coste
provisional de traslado no esconden un duplicado. No se permite omitir nuevos
asientos de la asignación del recibo.

## Contrato HTTP

`POST /real/accounts/{account_id}/dex-call-resolutions/{kind}/{identifier}/external-effects/preview`.
`kind`: `WALLET_SWAP` u `OPERATOR`, sin adaptar otros orígenes a estos perfiles.

- Requiere sesión REAL local y prueba EOA del propietario/contexto y de todos
  los participantes suministrados. El operador requiere trabajador inactivo.
- Cuerpo estricto: `address`, `wallets`, `review_id`, `recorded_at` opcional,
  `new_events`, `members`, `allocations`, `annotations`, `manual_effects`,
  `asset_scales` y `tie_orders`. No recibe pruebas ni roles del cliente.
- Cada referencia de evento usa exactamente `existing_id` o `new_key`.
  Las propuestas nuevas incluyen cuenta, tipo, divisa, importes, nota y una
  `pair_key` para traslados. El coste de una pareja nueva lo calcula el servidor.
- Importes y unidades se reciben como texto decimal, con límites y sin floats.
  El cliente no recibe el certificado interno de proyección con enteros grandes.
  Saldos, costes e importes devueltos son cadenas; los recuentos son acotados.
- El cálculo financiero y la reproducción de los criterios previos suceden fuera
  del mutex y lease de REAL. Antes de devolver se revalidan sesión/generación,
  EOA, custodia, observación, orden, originales, criterios y reservas de trabajador.
  Un bloqueo gana también ante errores del cálculo; no se publica un saldo tardío.
- Devuelve digest revisable, IDs de propuestas, comparativas actuales/propuestas
  de todas las cuentas y estado de revisión manual. `UNKNOWN` mantiene incompleta
  la propuesta. Todos los resultados son privados y `no-store`.

En desarrollo 78 se añaden `POST /confirm` y recuperación con `GET` de la base,
según el contrato de aceptación enlazado. La observación original permanece
congelada: el resultado efectivo de la aceptación aún necesita su integración.
El nonce sigue consumido y el bot no se reinicia. Una declaración aceptada tampoco
prueba efectos internos no observados ni un fill de la estrategia original.

## Verificación

La tanda inicial conserva 15 casos correctos y una expectativa de test incorrecta:
el traslado no produce beneficio USDT, pero el gas sí es un gasto BNB y el informe
lo incluye en resultado realizado. Se corrige la expectativa sin cambiar el motor.
Se añaden pruebas de coste de tokens pareados, selección de gas existente, fechas
y custodia del operador compartida o separada. La segunda tanda pasa **45 casos
de dos módulos**, incluidos **21 nuevos**, con salida 0 e identidades cotejadas.
Una tanda de regresión separada pasa **52 casos de siete módulos**, salida 0.
Las dos usan las mismas 1.295 fuentes congeladas: son **97 identidades sin
solapamiento en nueve módulos**, no una batería de aceptación de todo Orbis.
Las nuevas declaraciones y sus costes se prueban con recibos firmados en Anvil,
SQLCipher, API privada real y comprobación de ausencia de escrituras/RPC.

Evidencia: `~/.local/state/orbis/verification/external-effects-http-dev-r2/`,
`external-effects-http-regression-dev-r3/` y
`external-effects-http-final-dev-r4/verification.json`.
La primera tanda fallida se conserva en `external-effects-http-dev-r1/`.

Los 97 casos citados son evidencia histórica de la captura/previsualización en
esquema 77. La aceptación, sus migraciones y regresiones tienen evidencia propia
en el contrato de esquema 78. Quedan resultado efectivo, interfaz y publicación.

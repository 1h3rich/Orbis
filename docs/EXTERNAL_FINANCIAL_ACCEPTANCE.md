# Aceptación financiera de una llamada externa

Estado: desarrollo aislado, esquema REAL 78. Aceptación financiera verificada en
el alcance descrito al final;
sin publicación en la instancia v114 del propietario. No prueba que se ejecutara
el bot original ni reenvía una solicitud cuyo nonce ya está consumido.

## Un solo registro de criterios

Se reutilizan `real_economic_acceptances` y sus miembros, con nuevo perfil de
fuentes `dex-trades-gas-and-external-3` y revisión `economic.review.v3`.
El linaje sólo puede avanzar 1→2→3. Los formatos anteriores se conservan y no
aceptan un claim externo como operación conocida o como gas sin principal.

Drafts nuevos: v5 para revisar un criterio externo ya existente, v6 para añadir
recibos conocidos después de un criterio externo y v7 para la primera aceptación
de una declaración externa. Cada uno lleva su contrato explícito. Un prefijo sin
asientos sólo se permite bajo perfil 3, con testigo auténtico de una observación;
no se inventa un evento para satisfacer los formatos anteriores.

## Registros nuevos y ausencia de ciclos

- `real_dex_external_bookings`: declaración manual, plan, observación y certificado
  ligados por digest, sello exterior y sellos independientes de custodia/declaración.
- `real_dex_external_booking_members`: miembros originales de varias cuentas,
  con papel GAS/EFFECT y ordinal. Cada asiento se asigna una sola vez; los lectores
  y escritores de recibos anteriores también excluyen esas asignaciones.
- `real_dex_external_booking_accounts`: participantes de custodia, incluso si un
  recibo no produce asientos. Añade relaciones al cierre contable sin necesitar
  un traslado ficticio. Sus índices se autentican contra el booking completo.
- `real_dex_external_acceptance_links`: vínculo separado al primer criterio
  aceptado, con digest y sello propios. Se escribe después del criterio en el
  mismo commit para evitar un hash circular. Un archivo frío con booking sin
  este vínculo se rechaza.

El lector del booking autentica fuente, certificado, custodia y todos los
originales, y repite la conciliación de unidades sin reproducción financiera.
No consulta el criterio activo, por lo que puede usarse durante la captura de
ese criterio sin recursión. La reproducción financiera completa de la primera
declaración sucede después de soltar SQL, desde el prefijo exacto archivado.
Los balances guardados son comparativas que deben reproducirse; nunca saldos
autoritativos usados para omitir los originales.

## Comando atómico

La previsualización captura propiedad, vínculos, fuentes, originales y cabezas
en una instantánea. El plan puro calcula unidades, gas y costes. Una transacción
corta materializa provisionalmente originales/booking/testigo y captura el
prefijo completo; siempre revierte. El criterio se calcula fuera del lease.

Al confirmar se vuelve a comprobar la sesión, todas las EOA, las reservas de
trabajador, la captura completa y los precedentes, bajo `BEGIN IMMEDIATE`.
Se insertan los mismos originales, booking, miembros, criterio y vínculo de cierre
y se confirma una sola vez. Un fallo en cualquier frontera revierte el conjunto.
Un segundo intento devuelve el primer resultado inmutable, sin nuevos asientos.

El criterio declara origen manual de precios y efectos no observados. El informe
mantiene `effects_proven_complete=false`, `strategy_executed=false`,
`resolves_intent=false` y ausencia de autoridad de trading. La integración del
resultado efectivo es un paso posterior: el origen congelado continúa intacto,
el nonce no se reutiliza y ninguna operadora se reinicia automáticamente.

## API privada

Base: `/real/accounts/{account_id}/dex-call-resolutions/{kind}/{identifier}/external-effects`.

- `POST /preview`: revisión tipada y cálculo, sin guardar. Devuelve el digest del
  draft completo, el tiempo de registro que debe conservar el cliente y los
  bloqueos operativos. Una anotación UNKNOWN impide aceptar. El contrato indica
  expresamente si la aceptación está implementada y si puede efectuarse ahora.
- `POST /confirm`: misma selección, `recorded_at`, digest exacto y confirmación
  `ACEPTAR_EFECTOS_EXTERNOS`. Requiere prueba EOA de cada dirección/contexto de
  la selección, vínculos actuales y trabajador inactivo. Una revisión nueva
  caduca a los quince minutos; una respuesta ya guardada conserva su identidad.
- `GET` de la base: recuperación privada por origen y contexto, después de
  reabrir sin wallet ni RPC. Reproduce el criterio archivado y diferencia su
  aceptación histórica de que siga siendo el criterio vigente.

La selección se vincula mediante un digest de parámetros tipados y referencias
HMAC de las direcciones, sin persistir direcciones crudas. Una confirmación
repetida con otra declaración se rechaza. Un bloqueo/generación nueva descarta
respuestas tardías. No hay consultas RPC, firma ni envío en estos tres puertos.

## Comprobaciones y pendiente

Primera tanda interna: diez casos pasan sobre Anvil y SQLCipher. Incluye commit
único, fallos inyectados en criterio/vínculo/commit, dos conexiones simultáneas,
originales o custodia alterados, corrupción con sello exterior recalculado,
ausencia del vínculo, ocultación de esquema y copia cifrada fría.
Se amplían recorridos HTTP, prefijo vacío real, migración 77, linaje posterior y
compatibilidad de nuevos recibos. Una segunda tanda pasa 59 casos y otra dirigida
cuatro; no se suman como cobertura independiente.

La batería integrada recoge **692 identidades / 53 módulos**: **691 correctos y
un fallo**, salidas `[0,1]`. El fallo corresponde al montaje manual de `_Scope`
de una prueba MEXC, que no incluía el nuevo campo `external`. Se añade solamente
un `frozenset()` vacío a ese constructor y se repiten los **siete casos de su
módulo completo**, salida 0. Se verifica por AST que no cambian sus aserciones,
otros tests ni helpers; todas las fuentes de aplicación permanecen idénticas.
La cobertura final de 692 identidades combina esa repetición con la tanda amplia;
no es una segunda tanda completa limpia. Incluye **16 casos nuevos en dos módulos**
respecto al tramo anterior: 14 de aceptación y dos nuevos de API, además de ampliar
las comprobaciones de los dos casos de operador ya existentes.

Las 1.299 fuentes actuales y la evidencia quedan en
`~/.local/state/orbis/verification/external-financial-final-dev-r6/verification.json`.
Se conservan la tanda inicial (`external-financial-integrated-dev-r4`) y su
corrección (`external-financial-fixture-correction-dev-r5`). No se modifica
`verification/latest.json`, que sigue identificando la publicación v114.

Quedan el resultado efectivo en las decisiones de bots, la liberación controlada
de intentos posteriores, la interfaz completa y la verificación antes de publicar.
Otros orígenes de nonce siguen necesitando sus adaptadores; no se presentan como
resueltos por pasar sus datos a través de WALLET_SWAP/OPERATOR.

## Siguiente frontera: resultado efectivo

`dex_operator_outcomes.OperatorOutcome` aún admite únicamente los resultados
anteriores. `dex_bot_journal._settled`, `_position` y sus testigos causales no
consumen esta aceptación manual. `dex_external_observation.require_actor_available`
continúa reteniendo al firmante. Son fronteras pendientes deliberadas; aceptar
la contabilidad no debe convertir una llamada desconocida en EXECUTED.

La integración necesita un resultado externo explícito y testigo versionado:
identidad del recibo, primer criterio aceptado y su tiempo. Una proyección con
`as_of` anterior no puede adoptar un hecho aceptado después. El gas se conserva
una sola vez y no se incrementan entradas, compras ni beneficios del bot por una
declaración manual. Antes de una siguiente orden se requiere una nueva admisión,
nonce EOA nuevo y estado físico del contrato compatible; el nonce consumido se
prohíbe siempre. El presupuesto de la concesión y la parada no se restablecen.

También falta admitir conjuntamente varias observaciones externas pendientes
del mismo cierre. La admisión actual sólo exime de bloqueo los orígenes incluidos
en testigos sellados de esa revisión; otro pendiente sigue bloqueando el cambio
de criterio. No se permite ignorarlo para completar una aceptación parcial.

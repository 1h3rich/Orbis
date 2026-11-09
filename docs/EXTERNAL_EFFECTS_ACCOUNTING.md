# Revisión contable de una ejecución externa

Estado: aceptación financiera y reapertura en verificación, desarrollo REAL 78.
No hay interfaz nueva ni publicación; la instancia del propietario sigue en v114.
Se conserva el certificado del esquema 77 y el diario v1 introducido en 76.
El objetivo sigue siendo cerrar el recorrido completo, sin enviar otra vez
la solicitud cuyo nonce ya se consumió.

## Decisión de arquitectura

Una llamada puede mover activos entre varias cuentas propias. Los perfiles
anteriores agrupan un trade y su gas en una sola cuenta. Se añade por separado
`ExternalOrderClaim` y `portfolio-economic-order-3.0`: un nodo físico de red
con miembros de distintas cuentas. La cronología compartida reconoce ese nodo
en los empates de todas sus cuentas y conserva las dependencias de traslados.
Cada traslado propio contiene ambas piernas, salida antes de entrada. El coste
derivado se propaga por el motor existente; no se editan importes originales.

Los perfiles 1/2 conservan sus tipos, fuentes y resultados. No admiten este
origen como gas puro ni como operación conocida. La revisión/aceptación antigua
rechaza el claim externo. El nuevo contrato usa revisión 3 y drafts explícitos
dentro del mismo registro de criterios.

## Unidades y declaración

`economic_external_effects` compara los movimientos declarados con cada flujo
observado y el gas exacto, por cuenta y activo. Exige asignar todos los flujos;
no reutiliza asientos de otro recibo. Las transferencias entre cuentas propias
necesitan asientos pareados, sin registrarlas como aportación, beneficio o salida
del patrimonio. Permite declarar compras/ventas y sus precios expresamente,
traslados, ajustes justificados, gastos y movimientos sin cambio de inventario.
Los precios y el significado económico conservan procedencia manual.

Un evento Transfer no define una compra ni un precio, y Approval representa un
permiso. Los metadatos de token son opcionales según [ERC-20](https://eips.ethereum.org/EIPS/eip-20).
El registro observado mantiene sus identidades de contrato/versiones. Para un
activo adicional se declara precisión manual; no se sustituye USDT/WBNB/BNB del
registro. Toda conversión de unidades es exacta, con Decimal y contexto propio.

Cada permiso y log no interpretado requiere anotación; también hay una revisión
explícita de efectos no observados. `UNKNOWN` mantiene incompleta la propuesta.
Un efecto adicional, por ejemplo un retorno nativo interno no observado, conserva
su motivo, unidades y procedencia manual. Aceptar una revisión manual nunca
convierte esos efectos en hechos de red probados ni en un fill de la estrategia.

## Gas y custodia

En el perfil 3, los gastos de gas se aplican antes de los efectos declarados.
Es un criterio contable explícito para consumir inventario previo, no una traza
de movimientos internos de la EVM. EIP-1559 contempla cobertura previa del gas
y devolución del gas no usado; aquí se contabiliza sólo la comisión efectiva
del recibo, sin registrar el máximo reservado como gasto. [EIP-1559](https://eips.ethereum.org/EIPS/eip-1559).

El pagador es el firmante de la prueba. Una wallet de usuario y su operador
mantienen roles distintos aunque sus activos se registren bajo una misma cuenta
de custodia. Si se usan cuentas separadas, el principal se registra pareado.
La aplicación no inventa una aportación para completar un saldo insuficiente;
debe registrarse su financiación real. Un recibo de comisión cero no genera un
asiento de gasto ficticio. Un recibo sin miembros financieros conserva un nodo
de orden sin crear inventario ni capital.

## Orden y privacidad

El certificado complementario `orbis.dex.external.order.proof.v1` mantiene intacto
el formato de observación v1. Vuelve a verificar la firma y los hechos observados,
rechaza hashes duplicados en el bloque y fija índice, tamaño y referencia HMAC de
la lista real. Comparte los dominios de bloque/emisor de los certificados previos.
Su sello vincula el contexto completo de origen y la observación persistida.
Avanzar el extremo finalizado no cambia la identidad de un recibo fijo. Sigue
dependiendo del RPC configurado, sin pretender una prueba de consenso/Merkle.

Las nuevas descripciones rechazan direcciones completas, hashes de transacción
y secretos reconocibles. Las referencias de asientos incluyen su digest canónico
y metadatos originales; no copian el texto libre de notas anteriores. No se
persisten claves, direcciones, calldata, logs crudos ni balances derivados.
La identidad de la propuesta incluye todo el historial original, claims previos,
límites, empates y orden externo, además del digest de los inputs efectivos de
la proyección. Cambiar una financiación o coste anterior invalida esa propuesta.
Reordenar la presentación de eventos/vínculos no cambia su identidad.

## Trabajo pendiente para habilitar el recorrido

1. Certificado complementario y migración implementados; ver
   [persistencia y verificación](EXTERNAL_ORDER_PERSISTENCE.md).
2. Captura y previsualización privada implementadas: cada vínculo EOA/cuenta y
   custodia del operador, unión de historiales y costes de propuestas nuevas.
   [Contrato y verificación](EXTERNAL_EFFECTS_PREVIEW.md). La futura aceptación
   debe repetir estas comprobaciones al escribir; el preview no es un permiso.
3. Aceptación financiera versionada implementada, en verificación: declaración,
   originales, booking, criterio y enlace en un solo commit, con recuperación.
   [Contrato y alcance](EXTERNAL_FINANCIAL_ACCEPTANCE.md).
4. Derivar el resultado efectivo sin llamar EXECUTED a la estrategia original,
   sin reenviar su nonce ni reactivar automáticamente una operadora.
5. Completar el recorrido visual, prueba Chromium/API, regresiones y publicación
   inactiva verificada. El piloto mainnet del usuario continúa separado.

La primera tanda del núcleo tuvo 57 casos correctos y un fallo de normalización
del tipo de error por fondos insuficientes. Se conserva la evidencia inicial;
el adaptador normaliza ese error sin permitir el gasto. La segunda tuvo 69 casos
correctos y una carrera al consultar el recibo de preparación de Anvil. El helper
propio espera el recibo con límite, sin reenviar ni forzar minado; se comprueba
contra una transacción realmente pendiente y un vencimiento controlado.

La tercera tanda pasa **100 casos / seis módulos**. Después se añade la identidad
del historial completo y su prueba de revisión obsoleta. La batería financiera/DEX
de **641 casos / 49 módulos** termina con **639 correctos y dos fallos** de destino
de migración fijado todavía en 75. Se conservan las salidas `[1,0]`. Se corrigen
sólo esas pruebas para usar el esquema vigente y comprobar también la nueva tabla
vacía, simulando orígenes antiguos que todavía no la contienen. Sus **33 casos de
dos módulos completos pasan**; las fuentes de aplicación y los helpers compartidos
no cambian. Se comprueba por AST que los otros tests y helpers de esos archivos
conservan su definición. La cobertura final de **641 identidades** se arma con
esa corrección, sin afirmar una segunda tanda completa limpia. Las tandas previas quedan en
`~/.local/state/orbis/verification/external-effects-core-dev-r1/`, `-r2/` y `-r3/`.
La tanda amplia y su corrección están en `external-effects-financial-regression-dev-r4/`
y `external-effects-migration-fixture-correction-dev-r5/`.
Informe final: `external-effects-core-final-dev-r6/verification.json`.
Fuentes finales: 1.287. La instancia original conserva las 1.275 fuentes publicadas
de v114 y no indica reinicio pendiente; no se abren bases o credenciales del usuario.

Una medida separada del motor puro sintético reproduce 1.000/10.000 transacciones
entre dos cuentas, con 3.003/30.003 originales y 2.000/20.000 piernas de coste
derivadas. Tiempos observados: 0,057413/0,619171 s; originales, cantidades, costes y
gas exactos. Son inputs sintéticos en un host compartido; no prueban RPC, SQL,
linajes de archivo ni rendimiento de UI. Evidencia:
`external-effects-projection-scale-dev-r1/result.json`.

## Contrato de persistencia e integración pendiente

El siguiente tramo necesita estas fronteras, no sólo guardar el JSON del cálculo:

- Certificado externo complementario en tabla propia, FK único a la observación,
  identidad por nonce/recibo/posición y sello de origen. Se integra en el archivo
  común de orden: los escritores anteriores también deben rechazar colisiones y
  bloques contradictorios con la nueva familia. Las migraciones se verificarán
  sobre copias cifradas propias, incluyendo rechazo de registros ocultos al bajar
  `user_version`.
- Booking externo sellado que liga declaración, certificado y miembros originales
  de todas sus cuentas. Su lector valida esos hechos sin consultar el criterio
  activo, para evitar ciclos de verificación. Un vínculo separado a la aceptación
  prueba el cierre efectivo; guardar una observación o certificado no lo prueba.
- Criterio externo versionado, distinto de los drafts/reviews 1/2. El linaje puede
  avanzar de 1 a 2 a 3, nunca perder fuentes al retroceder. El catálogo de fuentes
  requeridas debe incluir todos los bookings externos de las cuentas enlazadas,
  también los de cero miembros financieros. Un prefijo vacío sólo es válido bajo
  el contrato externo explícito, con una observación/certificado reales y sin
  inventar un evento para satisfacer los formatos anteriores.
  La aceptación guarda comparaciones financieras verificables y las reproduce
  desde originales; el digest de inputs efectivos del núcleo no sustituye ese
  archivo ni convierte sus snapshots en saldos autorizados. El contrato HTTP
  codificará cantidades y coordenadas grandes como cadenas canónicas para evitar
  pérdida de precisión en el cliente JavaScript.
- Captura de la unión de cuentas participantes y sus cierres de traslados. Antes
  de materializar una nueva pareja, se normaliza su base de coste con el resultado
  calculado, se repite el cálculo y se vinculan esos inputs originales definitivos.
  Asientos ya existentes conservan sus bytes y reciben únicamente contexto
  derivado. IDs deterministas y asignaciones únicas impiden crear o usar dos veces
  un movimiento. La aceptación compara también el criterio vigente y sus padres.
- Admisión privada con control EOA de cada wallet y vínculo real al ledger destino.
  La custodia del firmante delegado deriva de su fuente/concesión autenticadas;
  no equivale a otra wallet de usuario sin demostrar. Las direcciones son
  transitorias. Antes del commit se revalidan token, generación, vínculos, fuente,
  estado inactivo y revisiones; la criptografía/RPC/reproducción financiera se
  mantienen fuera de los leases largos.
- Un solo commit para nuevos originales, booking/vínculos, criterio y cierre.
  Reintento por ID recupera el resultado histórico sin exigir otro recibo ni otra
  escritura. Lectura fría reproduce ese criterio sin volver a conectar direcciones.
  El puerto efectivo distingue revisión manual aceptada de estrategia ejecutada,
  retiene el nonce consumido y no reactiva bots ni restaura presupuestos por sí solo.
- Integración en decisiones y posiciones de bots enlazados a estrategias reales,
  además del operador manual. El pendiente original y su decisión causal deben
  poder reproducirse con un resultado externo explícito, sin contar un fill que
  no se demostró. Hace falta verificar LONG/DCA, parada, reapertura y autorización
  de un siguiente intento separado. Los otros orígenes de nonce (ingresos,
  financiación/devolución de gas, control y creación) requieren adaptadores/perfiles
  explícitos; no se consideran cubiertos pasando sus datos por WALLET_SWAP.

El núcleo puro actual prepara esas operaciones; no sustituye la implementación
ni la verificación de estas fronteras. Su resultado sigue con `saved=false`,
`accounting_acceptance_active=false`, `resolves_intent=false` y sin autoridad.

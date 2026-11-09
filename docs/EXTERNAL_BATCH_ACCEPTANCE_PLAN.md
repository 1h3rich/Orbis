# Revisión conjunta de llamadas externas pendientes

Estado: propuesta, almacenamiento 79 y API privada verificados en desarrollo
aislado. Este recorrido no está habilitado en la instancia publicada. El
objetivo incluye aceptación atómica, recuperación, resultado efectivo e interfaz;
esta última todavía falta.

## Problema y contratos existentes

El manifiesto financiero v7 describe un solo booking. Además,
`RealDexExternalAcceptanceLink.acceptance_id` es único. Confirmar por separado
dos pendientes relacionados deja al otro como bloqueo operativo; también puede
impedir calcular el coste de un traslado que depende del primero. Repetir
criterios artificiales para enlazar una sola confirmación no representa el
primer criterio aceptado de cada fuente.

## Propuesta conjunta pura

- Capturar todas las observaciones/certificados y las mismas carteras probadas
  dentro de una única lectura SQL. Cada captura conserva sus referencias de
  origen/custodia y el estado real anterior, sin direcciones persistidas.
- Crear declaraciones separadas por recibo: miembros, movimientos propuestos,
  asignaciones, gas, permisos, desconocidos y escalas. Los IDs de evento,
  solicitud, revisión y certificado deben ser únicos donde corresponda.
- Construir primero todos los eventos y claims propuestos, comprobar por recibo
  sus unidades y después calcular la cronología completa con el motor 3 ya
  existente. Normalizar juntos los costes derivados de los nuevos traslados.
  Un segundo traslado puede usar el inventario recibido por el primero.
- Mantener los originales existentes intactos. Detección de duplicados contra
  originales reales; cada nuevo evento se asigna una sola vez. Gas y efectos de
  un recibo no pueden compartirse con otro. UNKNOWN impide aceptar el conjunto.
- Repetir cada revisión con los eventos definitivos y todos los claims. La
  salida tiene identidad de grupo, referencia a sus inputs y planes individuales
  de perfil nuevo; sigue sin autoridad para guardar, firmar o reactivar bots.

## Persistencia y API

La implementación en desarrollo añade una migración aditiva 79: grupo financiero sellado y enlaces de miembro
con booking único y aceptación compartida. La tabla de enlaces individuales se
conserva. Un manifiesto v8 autenticará el conjunto completo; los lectores v7
mantienen su interpretación. El diseño evita ciclos entre hash del criterio,
grupo y enlaces: el manifiesto fija los planes y la identidad de grupo; los
enlaces posteriores sellan su pertenencia y el criterio definitivo.

La aceptación debe revalidar todas las capturas, insertar eventos/bookings,
guardar un único criterio y enlazar todos sus miembros en un solo commit.
Staging revierte todo; el cálculo financiero ocurre fuera del lease. Sólo los
pendientes presentes en el conjunto autenticado quedan exentos del bloqueo de
aceptación. Otros pendientes o un trabajador activo continúan bloqueando.

Recuperar por identidad de grupo o por fuente no repite RPC ni movimientos.
Cada resultado efectivo conserva su recibo, gas y primer tiempo de aceptación;
EXTERNAL_REVIEWED sigue separado de una ejecución de estrategia. El gas cuenta
una vez y continuar exige nueva autorización y estado físico compatible.

## Verificación y orden de trabajo

1. Propuesta pura con dos recibos firmados de una misma EVM: ida/vuelta de saldo,
   traslado de activos con coste dependiente, venta posterior, gas de ambas
   wallets, rechazo de duplicados/conflictos y ausencia de escrituras.
2. Captura común, esquema cifrado, manifiesto y aceptación/reintento atómicos;
   pruebas de fallo en cada frontera, corrupción y reapertura sin claves RPC.
3. API privada y guardas de sesión, contexto EOA, concurrencia y trabajadores.
4. Interfaz de revisión con etiquetas breves y recorridos firmados de navegador,
   incluidos respuesta perdida, varios proveedores y bloqueo/reapertura.
5. Regresión y publicación. El rendimiento con linajes extensos, los adaptadores
   restantes y la aceptación global siguen pendientes; no se sustituyen por el
   resultado de este tramo.

Se reutilizan dependencias instaladas. Las pruebas usan exclusivamente archivos,
wallets y fondos propios de fixtures; ninguna migración se ejecuta sobre datos
del propietario.

## Primer tramo verificado: propuesta pura

`economic_external_batch` conserva las capturas reales anteriores, declara los
movimientos de cada recibo y normaliza los nuevos traslados contra la cronología
completa. Su resultado y sus planes individuales de perfil v3 son propuestas;
ese corte sólo admitía planes individuales v2 en persistencia. El tramo 79
posterior añade el almacenamiento conjunto descrito abajo. No hay endpoint ni
botón de aceptación conjunta todavía.

Dos reemplazos firmados en una misma Anvil permiten comprobar ida y devolución.
El segundo traslado aislado no dispone del inventario recibido por el primero;
la revisión conjunta sí deriva ambos costes. La venta posterior combina una
posición previa a 200 USDT con lo recibido a coste 100: coste medio 150, retorno
de coste 0,3, inventario restante 0,006/coste 0,9 y resultado realizado 0,3.
Los originales permanecen intactos. Dos wallets vinculadas a una sola cartera
producen sólo sus dos gastos reales de gas, sin aportación/retirada ficticia.

La primera fixture omitía la nota obligatoria del gasto; se añadió sin cambiar
la validación. La primera implementación tenía un error de sintaxis en el módulo
nuevo; se corrigió antes de probar el cálculo. La expectativa de aportaciones se
ajusta para conservar el BNB original que financia el gas de la segunda wallet.
Los fallos iniciales se conservan en la evidencia, sin contarlos como aprobados.

Pasan **18 casos nuevos** y la regresión final de **164 identidades/14 módulos**,
cuatro salidas 0 e igualdad de las 1.307 fuentes. Incluye conservación del puerto
individual, su aceptación/reapertura y las guardas de bots anteriores. Las tandas
focalizadas no se suman de nuevo. Informe:
`external-joint-pure-final-dev-r8/verification.json`.

## Almacenamiento conjunto 79 en desarrollo

Se conservan las tablas y restricciones anteriores. El grupo tiene identidad
propia y un criterio único; cada booking tiene un enlace de grupo único y su
ordinal. La relación individual anterior mantiene su aceptación única. Un
booking presente en ambas familias se rechaza. No se fabrican varios criterios
para enlazar los recibos de una sola confirmación.

El manifiesto de criterio v8 fija los planes v3 y certificados de todos los
miembros. Sus hashes preceden al grupo sellado y a los enlaces posteriores,
evitando referencias circulares. Los lectores v7 sólo admiten planes v2. Los
costes y las fórmulas siguen usando el motor financiero 3 ya existente.

La preparación materializa todos los originales y bookings dentro de una
transacción que siempre revierte. El cálculo financiero se ejecuta fuera del
lease. Confirmar recaptura todas las fuentes, compara la materialización y
guarda originales, bookings, un criterio, grupo y enlaces en un commit. La
recuperación por grupo o por cualquiera de sus fuentes reproduce el mismo
prefijo, sin RPC ni nueva ejecución. Cada resultado causal conserva el tiempo
de primera aceptación y el gas de su propio recibo.

La admisión conserva en su alcance los contextos de todas las wallets de la
custodia autenticada. Las direcciones transitorias sólo añaden alias si coinciden
con sus referencias selladas; no se guardan. La exención de pendientes sigue
limitada a los IDs de fuente seleccionados, nunca a toda una wallet. Dos casos
nuevos reprodujeron un pendiente de una tercera wallet cuyo contexto no era
el ledger; ambos fallaron antes de ampliar el alcance y pasan después.

El primer intento de persistencia detectó un enlace ORM pendiente al sellar el
siguiente. Los enlaces se sellan ahora antes de añadirlos juntos; no se relajó
la prohibición de verificar con cambios sin flush. Los informes fallidos se
conservan en `external-joint-storage-dev-r1` y `external-joint-guard-red-dev-r3`.
La regresión conjunta final pasa **220 identidades/18 módulos**, incluidas
las 21 nuevas, con cuatro salidas 0 e igualdad de las 1.310 fuentes. Se cotejan
las identidades XML contra la colección, sin sumar de nuevo las tandas
focalizadas. Incluye las rutas individuales anteriores, migración con un
criterio v7 ya aceptado, revisión normal posterior, resultados causales de cada
recibo y guardas de bots/vault. Informe:
`external-joint-storage-integrated-dev-r6/verification.json`.

Faltan API, interfaz y medición con historiales grandes. La instancia publicada
continúa en v114. Este tramo no completa la recuperación ni el objetivo global.

## Puerto HTTP conjunto en desarrollo

Rutas privadas bajo
`/real/accounts/{ledger_id}/external-effect-groups/{group_id}`:

- `POST /preview`: declaraciones por fuente/contexto y revisión, fecha conjunta
  y orden manual global. Reutiliza el parser del puerto individual sin cambiar
  sus fórmulas. Devuelve saldos/costes anteriores y propuestos, recibos, gas,
  número de originales y admisión operativa; no guarda movimientos.
- `POST /confirm`: misma selección y fecha, hash de preview y confirmación
  `ACEPTAR_EFECTOS_EXTERNOS_CONJUNTOS`. Revalida las fuentes y guarda el grupo
  atómicamente. Un reintento consulta la selección ya aceptada; cambiar cualquier
  declaración se rechaza, sin repetir movimientos.
- `GET`: recuperación privada por ledger e identidad de grupo. No necesita
  volver a conectar las wallets ni consultar un proveedor; autentica/reproduce
  los originales guardados. No permite recuperar el grupo bajo otro ledger.

Todos los modelos rechazan campos extra y fuentes/revisiones duplicadas. La
fecha y los empates sólo se indican en el nivel conjunto. La selección canónica
conserva el contexto y referencias HMAC de cada wallet; invertir el orden de las
declaraciones no cambia su identidad. No se persisten direcciones completas.

Preview y confirmación comprueban todas las pruebas EOA/contexto antes de leer.
El cálculo queda fuera del bloqueo de acceso y del lease SQL. Se comprueban
después generación, contexto, nombres, vínculos y originales. Confirmar vuelve
a comprobar autorización/idle bajo la guarda del commit. Cerrar la bóveda durante
el cálculo o inmediatamente antes del commit gana y devuelve 401, sin aceptación
parcial. UNKNOWN devuelve preview visible con aceptación deshabilitada.

La regresión integrada final pasa **249 identidades/21 módulos**, incluidas las
21 nuevas de almacenamiento y las 15 nuevas de API, con cuatro salidas 0 e
igualdad de las 1.312 fuentes. Se coteja cada identidad XML con la colección;
las tandas focalizadas no se vuelven a sumar. Informe:
`external-joint-http-integrated-dev-r9/verification.json`.

La prueba conjunta HTTP usa dos recibos WALLET_SWAP firmados de la EVM propia
y SQLCipher. La fixture rechaza cualquier RPC desde este puerto. La ruta acepta
el tipo OPERATOR y aplica su guarda de trabajador; falta el recorrido conjunto
firmado de fuentes mixtas, además de sus regresiones individuales conservadas.
Todavía no se publica ni hay interfaz de grupo. La integración de interfaz,
las fuentes mixtas y los recorridos firmados de navegador son el siguiente
trabajo, antes de la publicación. Rendimiento ampliado y aceptación global
siguen pendientes.

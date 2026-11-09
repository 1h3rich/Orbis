# Registro y aceptación conjunta de recibos DEX

Estado: API, servicios e interfaz de revisión en desarrollo verificado.
La confirmación se coordina con la operativa activa. No se ha reiniciado ni
migrado la instancia v111 del propietario; falta la verificación general antes
de publicar este conjunto.

## Flujo implementado

La revisión recibe fuentes `WALLET_SWAP`, `OPERATOR` y `CALL_RESOLUTION`, con su
contexto, wallet transitoria y selecciones de asientos existentes. Sólo admite
compras/ventas finalizadas con prueba de bloque conservada. No acepta saldos,
precios ni eventos arbitrarios suministrados por el cliente.

`economic_receipt_source` fija una vista SQLite de originales, cierre de
transferencias, fuentes, pruebas, vínculos y precedentes. El destino procede
del vínculo actual de la wallet y debe ser una cuenta propia del cierre,
separada de MEXC. Los testigos antiguos ya aceptados conservan su vínculo
histórico sellado: incorporar otra wallet no exige disponer de la dirección
completa de la primera. Las capturas no crean sal, eventos ni registros.

Se exige el prefijo conocido de ejecuciones de cada wallet: no se puede elegir
una ejecución posterior y omitir otra anterior pendiente. Cuando falta el
índice de otra ejecución del mismo bloque, se solicita su comprobación antes
de inferir cuál ocurrió primero. Una operación posterior pendiente puede quedar
para un lote posterior.

`economic_receipt_plan` ordena el grafo completo fuera de SQL. Usa el mismo
planificador y transición financiera de `economic_projection`, extraídos sin
cambiar la construcción del grafo, su recorrido ni las fórmulas (comparación
AST y mismos 19 casos antes/después). Las decisiones de orden de red y los
desempates manuales se aplican también a los recibos nuevos.

El gas se materializa en su punto de ejecución: descuenta primero el gas
existente seleccionado, después utiliza BNB en caja y finalmente posiciones
BNB, priorizando la divisa base y el orden de divisas del criterio anterior.
No convierte automáticamente entre monedas ni crea aportaciones para cubrir
un déficit. La suma exacta en wei debe conservar el gas del recibo. Los eventos
seleccionados mantienen IDs, inputs y fechas originales; las fechas económicas
de red se presentan como metadatos separados.

Una operación/comisión cercana compatible exige selección o confirmación
explícita de que se trata de otro hecho. El lote no duplica silenciosamente
una anotación manual. Las selecciones no pueden reutilizar un evento reclamado
por otro recibo o fill. Los eventos nuevos tienen IDs y requests deterministas
del lote y fechas de registro fijadas en la revisión.

`economic_receipt_stage` materializa temporalmente eventos y registros en una
transacción corta. Comprueba los mismos formatos de recibo publicados y captura
los testigos/prefijos autenticados. **La preparación siempre revierte** antes
de devolver los inputs, sin adoptar el plan puro como prueba de persistencia.
El replay financiero completo se ejecuta después de cerrar esa sesión.

`economic_receipt_acceptance` vuelve a calcular la revisión y, al confirmar,
revalida la preimagen, fuentes, vínculos, materialización y precedentes bajo
`BEGIN IMMEDIATE`. Sólo entonces guarda recibos, eventos y aceptación en el
mismo commit. Un fallo después de insertar la aceptación revierte todo. El
bloqueo durante el replay cambia la generación e impide el commit.

## Archivo y recuperación

El esquema SQL sigue en 74. La definición JSON de la aceptación admite
`orbis.portfolio.economic.acceptance.draft.v2`, manteniendo la lectura de v1.
La nueva definición añade un manifiesto de lote versionado, sin direcciones ni
hashes completos de transacción. Liga fuentes, selecciones, propietario
seudónimo, cuenta destino, prueba, registro, recibos y miembros a los originales.
Los formatos de evento y contabilización de recibos conservan sus versiones.

La cabecera comprueba ese manifiesto contra fuentes, registros, testigos, IDs
deterministas y fechas reales de los eventos. El archivo y las copias vuelven
a reproducir resultados desde originales; un saldo guardado no es autoridad.
El escritor de aceptaciones se comparte con el camino anterior y no hace
commit por su cuenta.

La confirmación recibe ID de lote, fecha de registro y digest de revisión. Una
revisión nueva caduca a los 15 minutos. Un reintento ya confirmado recupera su
prefijo exacto incluso después de nuevos movimientos, sin repetir asientos ni
convertirlo en una revisión nueva. Comprueba de nuevo la wallet y su vínculo;
no da permiso para operar. `events_created` vale cero al recuperar una respuesta,
mientras `new_events_count` conserva el tamaño histórico del lote.

## API de revisión y admisión operativa

El contexto es `/real/accounts/{context_account_id}/economic-review`. Todas
las rutas son POST con cuerpo estricto; necesitan sesión local y prueba EOA
vigente para cada wallet/contexto proporcionado. La cuenta destino se deriva
del vínculo de la wallet. No se envían direcciones por URL ni se devuelven en
los resultados. Las respuestas usan `Cache-Control: no-store`.

- `sources`: catálogo pasivo de recibos de la wallet, sus contextos, pruebas
  conservadas y candidatos manuales compatibles no reclamados dentro de dos
  minutos. La búsqueda temporal usa un índice ordenado. No consulta la red ni
  modifica registros; los candidatos no se eligen automáticamente.
- `preview`: prepara un lote nuevo o revisa recibos ya registrados. Devuelve
  cantidades resumidas, estado operativo y comparación por cuenta. No expone
  el borrador completo ni crea asientos persistentes.
- `confirm`: necesita `ACEPTAR_CRITERIO_CONTABLE`, digest y definición exactos
  de la revisión. Recalcula y revalida antes del único commit.
- `result`: recuperación pasiva por digest, con replay desde originales y
  `is_current`. Una aceptación antigua válida puede dejar de ser la vigente.

`current` representa el libro actual antes del lote; `network` representa el
resultado propuesto. Se capturan los originales y el método actual en la misma
vista SQLite y se calculan después de liberar SQL y el cerrojo de la bóveda.
Si el método actual falla, `current_error` informa del problema sin sustituirlo
por el orden original. `registration` es una comparación auxiliar del orden
original **incluyendo los recibos propuestos**, nunca el saldo actual. Ninguna
de estas cifras afirma ser una conciliación con el saldo de red/exchange.

La admisión revisa el cierre de cuentas y propietarios/contextos autenticados:
bots activos MEXC/DEX, órdenes reservadas o de envío desconocido, solicitudes
de wallet y movimientos pendientes. Una aceptación nueva se bloquea mientras
exista esa actividad; la revisión y recuperación siguen siendo de lectura.
El informe no detiene trabajadores, cancela órdenes ni modifica protecciones
nativas. Las cuentas ajenas continúan operando. GRVT utiliza su diario separado
de subcuenta y no depende de este libro patrimonial.

Se vuelve a comprobar la admisión dentro de `BEGIN IMMEDIATE`. En HTTP se toma
primero el cerrojo de acceso y después el de bóveda durante el commit corto.
Proteger la vista revoca la confirmación pendiente aunque la generación
criptográfica siga abierta por operativa autorizada. El cálculo pesado no
retiene esos cerrojos.

La comprobación de bloques admite `X-Orbis-Passive: 1` para una secuencia
iniciada por el usuario sin prolongar repetidamente el desbloqueo. El cliente
compartido conserva ese indicador. La capacidad `economic_review` describe el
backend disponible; v111 no la anuncia y aún no carga esta revisión.

## Interfaz

En Wallet, `Revisar cuenta` abre un diálogo con los recibos de la wallet
conectada. El botón sólo aparece cuando el backend anuncia `economic_review`
y necesita control EOA verificado en BNB. Los contextos adicionales solicitan
su prueba EOA por separado: el mensaje se presenta antes de `personal_sign` y
su respuesta debe afirmar que no autoriza gasto.

Los recibos nuevos se pueden seleccionar. Una anotación manual compatible
exige elegir su ID o declarar expresamente otro hecho; el gas permite elegir
varios asientos y reconocer un gasto distinto. La lista limita el DOM a 50
recibos por página, conservando las selecciones de las demás páginas. Antes
de preparar, se comprueban los bloques financieros que faltan; después de la
primera observación, las restantes continúan en modo pasivo.

La revisión muestra únicamente `Actual` y `Propuesto · red`, por cuenta,
incluidos efectivo, aportaciones, retiradas, resultado realizado, comisiones,
unidades y coste. En móvil los dos valores de cada concepto permanecen visibles
en columnas, sin necesitar desplazamiento horizontal. Los importes son texto
decimal: incluso la notación científica se expande sin convertir el importe a
coma flotante. La ayuda conserva el dato exacto; ocultar importes borra también
esas ayudas y deshabilita la aceptación.

Una confirmación sin respuesta no se reenvía automáticamente. Se conserva la
solicitud exacta en memoria de la pestaña y `Comprobar estado` consulta sólo el
digest. Cerrar borra los datos monetarios del diálogo, conservando la recuperación
pendiente en esa sesión. Un resultado todavía ausente permite comprobar de
nuevo o iniciar expresamente una revisión fresca; la revalidación transaccional
del núcleo sigue impidiendo registros duplicados si la primera solicitud acaba
después. Desconectar, cambiar wallet/cuenta/red/generación o perder la prueba
borra el estado y descarta respuestas tardías. No se guarda en web storage.

El diálogo nunca solicita firmas de transacción, envía fondos, para trabajadores
ni cancela órdenes. Una revisión bloqueada por actividad permite volver a los
controles existentes para resolverla y preparar una revisión actualizada.

## Verificación inicial

Pruebas con Anvil propio y SQLCipher: compras/ventas recuperadas, wallet y
operadora en un bloque, recibo anterior al prefijo, wallet nueva, selección de
asiento manual, gas parcial/caja/posición, transferencia y venta dependientes,
conservación de originales y aportaciones, copia cifrada/reapertura, respuesta
perdida tras nuevos movimientos, dos conexiones SQL independientes, reloj,
caducidad, cambios de selección y fallo tras la aceptación. El bloqueo durante
el replay se provoca desde otro hilo para comprobar que no queda un cerrojo SQL.

## Pendientes antes de publicar

- Asistente de desempates manuales explícitos y catálogo simultáneo de wallets
  nuevas aún no aceptadas. La API admite órdenes manuales y contextos, pero la
  interfaz actual no incluye editores para esas dos situaciones avanzadas.
- Tratamiento conjunto de comisiones de fuentes sin compra/venta (permisos,
  controles y otros movimientos de gas) anteriores a un prefijo aceptado.
  El lote financiero actual no las admite ni las representa como operativas.
- Rendimiento con muchos prefijos/linajes y lotes grandes, suite general y
  recorridos completos de la nueva aceptación antes de cargarla.

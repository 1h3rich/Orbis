# Recuperar una llamada externa exitosa

Estado: desarrollo aislado después de v114. Ningún control nuevo está habilitado
en la instancia del propietario. El objetivo es recuperar el flujo completo;
el verificador de observaciones es sólo el primer tramo, no una resolución final.

Implementados verificador puro, previsualización privada y diario complementario
`orbis.dex.external.journal.v1`. Desarrollo REAL en esquema 78; la instancia del
propietario conserva v114/código de esquema 75. El diario se introduce en 76 y
la persistencia del certificado complementario en 77 y la aceptación financiera
complementaria en 78. La migración aditiva se
prueba únicamente en archivos propios, con preimagen cifrada y preservación del origen.

El puerto `external-observation/save` vuelve a consultar y verificar la evidencia
fuera del lease, revalida fuente/generación/contexto y guarda con confirmación
explícita del digest revisado. `external-observation/saved` recupera el registro
por origen sin RPC ni volver a conectar una dirección. Un reintento idéntico de
guardado devuelve el registro existente sin repetir consultas ni escrituras.
No son endpoints de resolución: devuelven `EFFECTS_PENDING`, nonce consumido,
contabilidad pendiente y ninguna autoridad de ejecución.

Verificación final: **191 casos / 13 módulos**, en dos procesos aislados con
salidas `[0,0]`, identidad de casos comparada con la colección y las mismas
1.282 fuentes congeladas. Incluye **65 casos nuevos / cuatro módulos**; no se
suman de nuevo. La tanda previa de 189 casos se repite después de proteger las
importaciones de bóvedas antiguas y añadir dos casos para esquemas 70/75.

Recorrido privado adicional del operador, con transacción firmada en Anvil:
11 comprobaciones de concesión detenida, reserva de trabajador antes/después
del RPC, gas real, origen intacto, idempotencia sin RPC y reapertura sin wallet
ni clave de operador. Son comprobaciones del recorrido, no once tests pytest.
Fuentes iguales a la tanda final. Evidencia autoritativa del diario:
`~/.local/state/orbis/verification/external-observation-journal-final-dev-r3/verification.json`.
Recorrido: `external-observation-operator-http-final-dev-r2/result.json`.
La revisión contable y el recorrido UI siguen pendientes; no se publica todavía.

Después del diario se incorpora el núcleo puro de declaraciones y proyección
externa entre cuentas, junto con verificador y persistencia complementarios de
orden ([tramo 77](EXTERNAL_ORDER_PERSISTENCE.md)). En esquema 78 se implementa
la [aceptación contable y reapertura](EXTERNAL_FINANCIAL_ACCEPTANCE.md), sin UI
ni resultado efectivo nuevos todavía. La cobertura financiera histórica
ampliada verifica 641 identidades con corrección de dos pruebas antiguas de
migración y repetición de sus módulos completos. No es otra tanda completa limpia.
Ver [decisiones, evidencia y siguientes fronteras](EXTERNAL_EFFECTS_ACCOUNTING.md).

## Problema reproducido

Una transferencia USDT de 0,005 en Anvil consume el nonce reservado de un SWAP,
finaliza correctamente y paga 103.240.000.000.000 wei. El verificador v1 conserva
el pendiente porque no es el swap esperado ni una cancelación. Eso evita inventar
una operación, pero falta un recorrido que permita revisar y cerrar el incidente.
Evidencia propia: `external-call-effects-probe-r1/result.json`.

## Contrato y límites

1. Reconstruir y recuperar la firma de los bytes de la transacción, comprobar
   emisor, cadena, nonce, hash y pertenencia al bloque finalizado observado.
   Una respuesta RPC que sólo diga `from` no prueba el firmante.
2. Conservar por separado el resultado de ejecución y su interpretación. Un
   recibo exitoso prueba el estado de la llamada según el nodo, no que se haya
   ejecutado la estrategia. La finalidad sigue dependiendo del RPC configurado;
   este componente no es un cliente de consenso ni una prueba Merkle del recibo.
   El extremo finalizado se valida al observar, pero no forma parte de la identidad
   persistida: avanzar de bloque no cambia el recibo fijado ni su revisión.
3. Observar gas, valor nativo superior y eventos de contratos de tokens conocidos.
   Transferencias y permisos son hechos distintos. No deducir un precio, compra,
   venta, rentabilidad o cancelación a partir de un evento `Transfer`.
4. Mantener referencias HMAC de destino, interlocutores, calldata y logs. No
   guardar direcciones completas, txHash, calldata ni logs crudos. Metadatos de
   proveedor adicionales no pueden cambiar el digest de hechos normalizados.
5. Marcar cobertura parcial: los logs de tokens conocidos y el valor superior
   no reconstruyen transferencias nativas internas ni efectos de protocolos
   desconocidos. Logs no interpretados se cuentan y conservan por referencia.
   No truncar un recibo para aparentar cobertura completa.
   El contrato incluye cobertura nativa/de tokens y pagador de gas explícitos.
   La identidad del token conserva referencia de contrato y registro versionado;
   un símbolo solo no basta para reinterpretar un recibo antiguo.
6. El primer perfil admite EOA, cadena 56 y transacciones legacy/EIP-1559 sin
   access list, como el recuperador actual. Otros perfiles requieren ampliación
   explícita; no se adaptan a un formato soportado ni se declaran resueltos.

## Módulos y registros

- `live_execution.dex_external_observation`: verificador puro, sin RPC, firmas,
  escritura, permiso ni efectos sobre el pendiente. Esquema propio
  `orbis.dex.external.observation.v1`. Reutiliza recuperación criptográfica y
  comprobación temporal, conservando el verificador publicado de llamadas v1.
- Puerto de captura: obtiene la observación completa antes de tomar el lease de
  REAL. Revalida token, generación, fuente y contexto antes de publicar/guardar.
- Diario complementario: observación sellada, identidad de origen, nonce,
  referencias, pruebas de parada y revisión explícita. Nuevo registro, sin
  añadir resultados desconocidos a la enumeración financiera de v1.
  El registro inmutable `real_dex_external_observations` tiene claves únicas
  por fuente, firmante/nonce y recibo, además de exclusión con resoluciones
  conocidas/nativas/creación y los originales de las siete familias al guardar.
  No modifica la solicitud pendiente. Los mutadores del origen rechazan cambios
  posteriores; su wallet queda retenida hasta una revisión posterior de efectos.
  El gas real observado del operador consume su presupuesto una sola vez,
  incluso con sobrecoste; no se marca la estrategia como ejecutada ni se libera
  el permiso por guardar el recibo. Antes de guardar un operador se comprueban
  trabajador inactivo y concesión/ejecuciones detenidas. Los lectores y backups
  vuelven a validar forma, aritmética, atestación, sello, origen y parada.
- Revisión contable: el usuario identifica sus movimientos y gastos, o señala
  lo que todavía desconoce. Una declaración manual permanece identificada como
  tal; no se transforma en un fill probado de la estrategia. Registro, vínculos
  y criterio contable deben aceptarse de forma conjunta o revertirse completos.
- Puerto de resultado efectivo: distingue nonce consumido, efectos pendientes,
  revisión aceptada y operación original ejecutada. Una fuente consumida nunca
  se reenvía. Resolver el nonce no reactiva bots ni concede otra autorización.

Los contratos y motores financieros existentes se conservan. La integración de
orden/gas debe respetar sus perfiles: una llamada exitosa desconocida no se
camufla como fallo, swap o recibo de gas sin principal. Si se precisa un perfil
financiero adicional, tendrá versión y aceptación propias.

## Secuencia y aceptación

1. Verificador y pruebas firmadas: transferencias, permisos, valor nativo,
   creación, tipo legacy/1559, campos alterados, límites y privacidad. El lector
   de archivo revalida estructura, aritmética y atestación con origen exacto.
2. Diario/puerto privado: preview, aceptación, recuperación por ID, bloqueo,
   concurrencia y exclusión con todas las familias de nonce/tx ya publicadas.
   Operadoras detenidas; ningún envío ni firma de transacciones.
   La siguiente migración será aditiva sobre copias propias. El registro de
   observación y su revisión son entidades distintas: un nonce observado como
   consumido no equivale a efectos contables aceptados. Los lectores/guardas
   existentes deberán reconocer esa distinción sin devolver un resultado v1
   falsificado ni permitir asignar el mismo nonce/recibo a otra familia.
3. Revisión de efectos y contabilidad: originales intactos, declaración manual
   explícita, gas obligatorio cuando proceda, evento no reutilizado dos veces,
   asientos/criterio/vínculo atómicos y copia cifrada fría reproducible.
4. Interfaz: recorrido completo desde el pendiente, detalle de movimientos y
   límites, aceptación, respuesta perdida, reapertura y nuevo intento permitido
   sólo cuando corresponda. No habilitar un botón que sólo observe y parezca
   haber resuelto el incidente.
5. Regresión de los resultados conocidos, MEXC/DEX/GRVT y permisos. El piloto
   mainnet del propietario sigue separado y requiere su configuración efectiva.

## Siguiente integración contable (pendiente de implementación)

La observación no se editará cuando se revise: una entidad de revisión distinta
referenciará su ID/digest, las declaraciones del usuario, los asientos originales,
sus revisiones y el criterio aceptado. El resultado efectivo se derivará de ambos
registros. `EFFECTS_PENDING` no puede convertirse en `EXECUTED` del perfil de
estrategia conocido; tampoco libera por sí solo el bloqueo de nuevas emisiones.

El libro existente permite gastos, ajustes justificados y transferencias propias
pareadas. El siguiente puerto debe componer sus operaciones dentro de una sola
transacción o enlazar asientos existentes verificados. No puede llamar al helper
de transferencia que abre y confirma su propia transacción desde otro commit.
La aceptación debe conservar los importes originales y distinguir clasificación
manual de evidencia de red. Los logs de permiso no son unidades compradas y una
transferencia a otra wallet propia no es beneficio ni retirada patrimonial.

La revisión presentará todas las transferencias, permisos, gas y referencias no
interpretadas, con anotación de lo todavía desconocido. Para enlazar otra wallet
se comprobarán su control y su referencia local; la etiqueta `OTHER` no autoriza
a atribuirla a una cuenta cualquiera. El gas pertenece al firmante indicado en
la prueba. Las cantidades de red se mantendrán como enteros y se convertirán con
decimales exactos al construir los asientos. No se inferirán precios/costes de
adquisición a partir del símbolo del token o de un `Transfer`.

El orden de recibos de esta nueva familia requerirá contrato financiero propio
versionado y revisión explícita; no se ampliarán silenciosamente las listas de
fuentes ni las fórmulas de los perfiles de orden/gas ya publicados. Se verificará
la atomicidad de prueba, declaración, asientos/vínculos y criterio, con rollback,
respuesta perdida y copia cifrada fría. La UI se incorporará cuando ese recorrido
esté operativo, antes de publicar el cambio en la instancia del propietario.

## Fuentes primarias revisadas

- [EIP-155](https://eips.ethereum.org/EIPS/eip-155): firma legacy vinculada a cadena.
- [EIP-1559](https://eips.ethereum.org/EIPS/eip-1559): sobre firmado tipo 2 y tarifa efectiva.
- [EIP-658](https://eips.ethereum.org/EIPS/eip-658): estado de éxito/fallo de la llamada superior.
- [ERC-20](https://eips.ethereum.org/EIPS/eip-20): eventos Transfer/Approval; los metadatos de token son opcionales.
- [Execution APIs, recibos y logs](https://raw.githubusercontent.com/ethereum/execution-apis/main/src/schemas/receipt.yaml):
  índices, bloque, hash, gas y campos de recibos. Los datos del nodo siguen
  sujetos a la confianza del proveedor elegida en ORBIS.

# Wallet EVM: primer corte de observación

Estado de este documento: conexión y herramientas de observación, sin
sincronización patrimonial automática. El [ingreso USDT/BEP-20](WALLET_FUNDING_BNB.md)
es un módulo separado con plan, permiso durable, confirmación en wallet,
cotejo de recibo/abono View y registro contable explícito;
no se activa al conectar ni al utilizar estas consultas.
El [swap manual USDT↔WBNB en BNB Chain](WALLET_SWAP_BNB.md) es otro módulo
separado con permisos y confirmaciones de gasto explícitos. No modifica la
semántica de observación ni habilita ejecución en el cotizador de Ethereum.

## Referencias comprobadas y siguiente integración

Revisión pública del 6-11-2026, sin conectar wallets o cuentas del propietario.
[EIP-6963](https://eips.ethereum.org/EIPS/eip-6963) confirma la detección mediante
eventos y advierte que la metadata del proveedor no autentica su identidad.
ORBIS utiliza esa detección; el nombre mostrado no acredita una marca.

La [API actual de MetaMask](https://docs.metamask.io/metamask-connect/evm/reference/provider-api/)
distingue las extensiones de las sesiones SDK: en estas últimas el orden de
cuentas no identifica necesariamente la seleccionada y algunos eventos también
forman parte del handshake. [eth_accounts](https://docs.metamask.io/metamask-connect/evm/reference/json-rpc-api/eth_accounts/)
enumera las cuentas autorizadas para el origen. Esta consulta no verifica por
sí sola su control ni autoriza gasto. ORBIS no incorpora MetaMask Connect SDK
en este corte; una integración futura debe comprobar esas diferencias.

El enlace técnico antiguo de Trust Wallet redirige a su portada en la fecha
de revisión. Se conserva como referencia de producto su [extensión oficial](https://trustwallet.com/browser-extension),
sin usar esa página comercial como contrato de métodos RPC.

Para el asistente pendiente de varias wallets se necesitará selección explícita
y prueba de cada contexto. No se deducirá una wallet activa por marca o por la
primera posición de una lista SDK. Un cambio ajeno al flujo revisado seguirá
invalidando borradores; no se conservarán direcciones en almacenamiento web.
Esto documenta decisiones para la integración pendiente, no su implementación
ni una prueba manual con extensiones reales.

## Contrato

- La wallet se elige en «Cartera real» después de desbloquear la bóveda. La
  selección usa anuncios EIP-6963 y `window.ethereum` sólo como compatibilidad
  cuando no hay anuncios. El nombre del proveedor es informativo y no verifica
  que sea MetaMask o Trust Wallet.
- La extensión solicita permiso de cuenta únicamente al pulsar «Conectar
  wallet». Si expone varias cuentas, se elige una en un selector de direcciones
  abreviadas. ORBIS consulta `eth_chainId`, `eth_blockNumber` y
  `eth_getBalance` fijado a ese bloque; no llama métodos
  de firma, aprobación, transferencia u órdenes.
- Las direcciones completas sólo viven en memoria de la pestaña mientras está
  conectada. La consulta de saldos no las envía al servidor ORBIS, SQLite ni
  `localStorage`; sí pasan a la extensión y a su RPC. La prueba EOA optativa
  envía temporalmente dirección y firma al servidor local, sin persistirlas;
  conserva sólo una huella seudónima si se confirma el vínculo.
- El saldo se convierte de wei con `BigInt`, sin coma flotante. Las redes
  conocidas muestran activo nativo con 18 decimales; una red no reconocida
  muestra wei sin atribuirle un símbolo o precisión erróneos. El saldo es
  observado, nunca se incorpora automáticamente al libro REAL. Se muestra la
  altura consultada y se comprueba otra vez la red antes de presentar el saldo;
  si cambió durante la lectura, el resultado se descarta.
- El usuario puede indicar un contrato ERC-20 exacto de la red activa. ORBIS
  obtiene `eth_blockNumber` y consulta `decimals()` y `balanceOf(cuenta)` con
  `eth_call` en ese mismo bloque. `decimals()` es opcional: si no existe o
  devuelve una precisión no representable en la UI, se muestran unidades
  mínimas. No se usa el símbolo del contrato como identidad verificada. El
  contrato y la cuenta codificada en la llamada son visibles para el RPC.
- En Ethereum mainnet, con un ERC-20 de entrada ya consultado y `decimals()`
  disponible, «Uniswap v3 · cotizar» admite contrato de salida, cantidad y
  comisión de un pool directo. Por defecto compara 0,01/0,05/0,3/1 % y elige
  la mayor salida **bruta** entre las respuestas válidas, indicando cuántas
  respondieron; un fallo no cuenta como cero ni garantiza mejor ruta. También
  se puede fijar un pool manualmente. Consulta `decimals()` del destino, saldo
  de entrada y `QuoterV2.quoteExactInputSingle` con `eth_call` en el mismo bloque.
  La cantidad y el mínimo orientativo de 0,5 % usan enteros `BigInt`; el
  resultado caduca a los 30 segundos y se borra al cambiar token, wallet,
  red o campos. Cada lectura DEX vence a los 15 segundos y una respuesta
  tardía tras editar se descarta. El RPC de la wallet conoce ambos contratos y la cuenta para
  `balanceOf`. Si hay cotización, consulta además `allowance(owner, SwapRouter02)`
  del ERC-20 de entrada en ese mismo bloque y señala si cubre esa cantidad.
  La lectura puede fallar sin invalidar la cotización: entonces el permiso se
  indica como no verificable. Es sólo el permiso ERC-20 del SwapRouter02 de
  Ethereum; no comprueba Permit2, Universal Router, gas ni la viabilidad de
  una ruta ejecutable. No se transmite una orden, no se firma ni se solicita
  aprobación. La cotización y el permiso no se guardan en el servidor ni en
  `localStorage`. Contratos de referencia:
  https://developers.uniswap.org/docs/protocols/v3/deployments/v3-ethereum-deployments.
- «Destino MEXC · Ethereum» consulta bajo demanda una dirección de depósito
  ERC-20 sólo cuando la conexión View está vinculada a la cuenta REAL elegida
  y su UID seudónimo sigue coincidiendo. Exige ficha `netWork=ETH` con depósito
  habilitado, contrato único, una sola dirección EVM sin memo y que ese
  contrato sea exactamente el consultado en la wallet Ethereum. La dirección
  completa se muestra un minuto en la pestaña, no entra en SQLite ni
  `localStorage`, y se borra al cambiar wallet, red o token. El servidor local
  y MEXC sí la conocen; el usuario debe verificarla de nuevo en MEXC antes de
  enviar. No hay botón de transferencia, aprobación ni orden.
- Cambiar cuenta o red invalida la lectura anterior y la consulta de nuevo.
  Los saldos del token se invalidan y requieren una consulta nueva; cambiar de
  red descarta también el contrato indicado.
  Desconectar, bloquear o abandonar la vista borra el estado local y los
  listeners. Esto **no revoca el permiso de la extensión**: debe revocarse en
  la propia wallet.
- Al caducar la bóveda REAL por inactividad, la pestaña oculta los importes y
  limpia la conexión observada; el servidor revoca también el token. Una
  respuesta de lectura que llegue después no vuelve a mostrar los datos.
- «Cruzar txId con MEXC» acepta un `txId` EVM temporal. La wallet/RPC
  proporciona transacción y recibo: se exige que los hashes, emisor, bloque,
  red, cuenta expuesta y estado exitoso sean coherentes. En Ethereum mainnet,
  ambos `blockHash` deben coincidir con el bloque canónico consultado por
  número y su altura no puede superar el bloque `finalized` del mismo RPC.
  Si la finalización no está disponible, falta el hash o hay contradicción,
  no se cruza el txId con MEXC. Después, la API local
  busca ese identificador en las capturas cifradas MEXC con UID View
  verificado. Una coincidencia final indica **el mismo txId observado en ambas
  fuentes**. Para Ethereum mainnet y una dirección de depósito EVM sin memo
  declarada por MEXC, ORBIS guarda sólo su HMAC y puede comparar receptor e
  importe de una transferencia nativa simple o un único evento ERC-20 saliente.
  Los receptores candidatos viajan temporalmente al servidor local para
  calcular esas huellas; no se conservan las direcciones completas. Esa ruta
  limita el cuerpo a 4 KiB y devuelve errores sin repetir txId ni direcciones.
  El RPC
  también recibe el `txId` y, para ERC-20, la consulta de `decimals()`.
  Una coincidencia no demuestra que el contrato sea el activo que MEXC
  denomina con ese símbolo, ni que el depósito esté causalmente conciliado.
  Si la clave View publica en `config/getall` un único contrato con depósitos
  habilitados para ese activo y `netWork=ETH`, la captura conserva esa dirección
  de contrato pública y la UI la coteja con el evento ERC-20. Una ficha
  ausente, ambigua, inválida o no disponible deja la identidad sin comprobar;
  la ficha actual tampoco demuestra que el contrato fuera el mismo en la
  fecha histórica de la transacción.
  La consulta comprueba además si esa misma referencia MEXC tiene un traslado
  REAL pareado que el usuario vinculó explícitamente y que sigue íntegro. Un
  vínculo anterior a la prueba de UID o contradicho por capturas posteriores
  se muestra como advertencia, no como prueba. Incluso un vínculo verificado
  es sólo correspondencia contable local, no conciliación de saldos, costes ni
  ejecución. La búsqueda sin wallet en Cartera muestra el mismo estado del libro.
  No se guarda el hash bruto, no se crea un asiento y no se habilita ejecución.
  Cambio de red/cuenta, refresco, desconexión o bloqueo invalida la comprobación.
  Tras un recibo exitoso, el clic de comprobación intenta actualizar primero
  las capturas MEXC de los últimos siete días, sólo si la clave View sigue
  vinculada al libro elegido y su UID local coincide antes y después. Esa
  consulta no envía el `txId` a MEXC. Si no hay conexión o falla la lectura,
  continúa con el archivo cifrado anterior y distingue expresamente la falta
  de actualización cuando no encuentra el movimiento.
- «Retiro a wallet» usa el mismo campo temporal de `txId`, pero exige Ethereum
  mainnet, transacción en bloque finalizado y canónico según RPC con recibo
  exitoso y recepción nativa simple o
  evento ERC-20 dirigido a la cuenta seleccionada. Busca una huella HMAC del
  hash de cadena en capturas MEXC con UID View verificado; si MEXC publicó un
  destino EVM sin memo, compara su huella con la cuenta expuesta. Dos retiros
  que comparten hash, una ruta interna/desconocida, un estado no final o datos
  contradictorios no se presentan como retorno confirmado. La API no guarda
  hash ni dirección completa y nunca devuelve esos valores. La coincidencia
  **no** verifica identidad histórica del token, importe neto, comisión, finalidad de la
  cadena de forma independiente, titularidad de la cuenta ni asiento REAL; no habilita órdenes o
  retiradas.
  Si hay un único movimiento recibido, consulta `decimals()` del contrato ERC-20
  en el bloque del recibo (o usa 18 para ETH nativo) y compara con precisión
  entera las hipótesis `amount` y `amount − transactionFee` (si la tasa se
  cobra en el mismo activo). Una coincidencia
  sólo es numérica: la API MEXC no fija aquí la semántica bruto/neto y el
  contrato ERC-20 sólo se coteja con la ficha MEXC si `config/getall` publica
  una dirección ETH única y con `withdrawEnable` para ese activo. Una ficha
  distinta bloquea la comparación numérica; una coincidente es metadato
  observado al archivar, no identidad histórica o auditoría del código.
  Múltiples movimientos, ausencia de `decimals()` o importe no representable
  impiden esa comparación. La pantalla no muestra valores monetarios en este
  diagnóstico, incluso si la preferencia de ocultación está desactivada.

## Límites abiertos

- No descubre automáticamente contratos ERC-20, ni consulta NFTs, múltiples
  redes simultáneas o historial. La altura identifica la lectura, pero no
  garantiza finalización de la cadena ni que el RPC sea honesto.
- La cotización Uniswap sólo contempla un pool v3 directo en Ethereum, no
  rutas múltiples, v4, UniswapX, MEV, comisiones de gas ni autorización
  delegada. Un pool sin liquidez o un RPC que no soporte la simulación se
  muestra como no verificable; este cotizador de Ethereum no ejecuta swaps.
  La ejecución manual BNB tiene un contrato y módulo separados, enlazados al
  comienzo de este documento.
- No hay conexión móvil WalletConnect, ni validación manual con extensiones
  reales en todos los navegadores. La prueba de navegador usa dos proveedores
  EIP-6963 ficticios y no toca fondos ni RPC externos.
- El cruce de txId no es reconciliación wallet↔cartera: el RPC puede ser
  incorrecto o presentar una vista incoherente. La comprobación de finalización
  es sólo un informe de ese RPC, no una prueba criptográfica independiente;
  además falta
  auditar el código/identidad histórica del contrato ERC-20, costes/descuentos, depósitos
  con varios eventos, otras redes y la conciliación causal completa con el libro. Un evento
  `Transfer` emitido por un contrato no sustituye una auditoría del token.
  No hay prueba equivalente de depósitos GRVT, DEX, permisos delegados
  ni ejecución automática. Esos pasos requieren validación de riesgos,
  límites y parada independientes.

## Verificación

`tests/browser_wallet_readonly_flow.py` comprueba descubrimiento de dos
proveedores, ausencia de petición de cuenta antes del clic, métodos RPC
permitidos, precisión de saldos superior a 53 bits, selección de cuenta,
ERC-20 con y sin `decimals()`, cotización Uniswap manual y automática con
respuesta parcial, saldo insuficiente,
bloque fijo, ausencia de envío, bloque nativo y de token, máscara, descarte de
una lectura cuyo RPC cambió de red incluso sin evento, cancelación de la
lectura de token al cambiar de red, desconexión y bloqueo
sobre una bóveda temporal. La suite `pytest` sigue cubriendo las
operaciones financieras existentes.
`tests/browser_real_idle_flow.py` cubre la renovación del plazo, el bloqueo
automático visible y la revocación del token en una bóveda aislada.
`tests/browser_mexc_transfer_match_flow.py` cubre el cruce de recibo y archivo
MEXC con proveedores ficticios: emisor incorrecto, fallo EVM, coincidencia
de receptor e importe ERC-20, discrepancias, limpieza de entrada, vínculo
REAL explícito recuperado tras reiniciar y desconexión durante una respuesta
demorada. También prueba un retiro externo con hash y destino observado,
manteniendo importe y comisión sin conciliar.
`tests/browser_mexc_deposit_destination_flow.py` cubre consulta temporal con
MEXC y wallet ficticios, coincidencia y discordancia de contrato, limpieza al
cambiar red y ausencia de métodos RPC de envío.
`tests/live_uniswap_quote_smoke.py` comprueba optativamente una lectura pública
USDC/WETH por `eth_call` en Ethereum mainnet, sin wallet ni claves. El
proveedor RPC ve el par consultado; se puede sustituir con
`ORBIS_ETH_RPC_URL`.

## Referencias

- [EIP-6963: descubrimiento multiproveedor](https://eips.ethereum.org/EIPS/eip-6963)
- [EIP-1193: API y eventos del proveedor](https://eips.ethereum.org/EIPS/eip-1193)
- [ERC-20: balanceOf y decimales opcionales](https://eips.ethereum.org/EIPS/eip-20)
- [Ethereum JSON-RPC: saldo en un bloque concreto](https://ethereum.org/developers/docs/apis/json-rpc/)
- [MetaMask: eth_call de solo lectura](https://docs.metamask.io/metamask-connect/evm/reference/json-rpc-api/eth_call/)
- [MetaMask: gestión de cuentas](https://docs.metamask.io/metamask-connect/evm/guides/manage-user-accounts/)
- [Trust Wallet: extensión oficial](https://trustwallet.com/browser-extension) (referencia de producto; no contrato RPC)
- [Uniswap v3: cotización con QuoterV2](https://developers.uniswap.org/docs/sdks/v3/guides/swapping/quoting)
- [Uniswap v3: direcciones oficiales Ethereum](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-ethereum-deployments)

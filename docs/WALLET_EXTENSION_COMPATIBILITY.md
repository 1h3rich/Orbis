# Compatibilidad con extensiones de wallet

En ORBIS v143 se probaron por separado **MetaMask 13.56.0** y **Trust Wallet
26.45.1** en Chromium 153. MetaMask procedía del ZIP de su lanzamiento. El
paquete CRX de Trust Wallet se descargó de Chrome Web Store y se verificó la
firma de la clave que corresponde a su identificador oficial. [Lanzamiento de
MetaMask](https://github.com/MetaMask/metamask-extension/releases/tag/v13.56.0)
y [ficha oficial de Trust
Wallet](https://chromewebstore.google.com/detail/trust-wallet/egjidjbpglichdcondbcbdnbeeppgdph).

| Paso comprobado | MetaMask | Trust Wallet |
| --- | --- | --- |
| Detección del proveedor EIP-6963 en Cartera real | MetaMask | Trust |
| Aprobación de la conexión en la interfaz de la extensión | Sí | Sí |
| Cambio desde Ethereum a BNB Chain desde ORBIS | Sí | Sí |
| Lectura de bloque y saldo nativo en BNB Chain | Sí | Sí |
| Firma de control EOA aprobada en la extensión y verificada por la API local | Sí | Sí |
| Desaparición de los controles de wallet al bloquear la bóveda | Sí | Sí |

El navegador, la bóveda SQLCipher y el servidor RPC se ejecutaron con acceso
exclusivo a loopback. Cada extensión usó su propio perfil y una cartera
desechable. El RPC controlado devolvió saldo cero y se comprobó que no recibió
transacciones. La API devolvió `spending_authorized: false` tras validar la
firma de control.

Para conectar tu wallet, abre ORBIS en el mismo perfil del navegador en el que
está instalada la extensión. Este informe cubre la conexión de escritorio,
el cambio de red y la prueba de control. El piloto wallet → MEXC → wallet
con una cuenta real sigue pendiente.

Evidencia local en
`~/.local/state/orbis/verification/wallet-official-extension-r1/verification.json`.

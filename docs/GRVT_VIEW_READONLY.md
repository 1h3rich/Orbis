# GRVT View: observación de cuenta

Estado: consulta puntual mainnet de resúmenes de cuenta y posiciones, **sin
órdenes, transferencias ni importación contable**. Está en «Cartera real» sólo
para comparar saldos observados con el libro manual; no los suma a ese libro.

## Contrato y seguridad

- Al desbloquear la bóveda, el usuario puede introducir una clave API creada
  con permiso View en GRVT. ORBIS no reutiliza ninguna clave compartida en un
  chat. El login obtiene una cookie `gravity` y un identificador de cuenta en
  memoria; la clave no se conserva después de autenticar. Desconectar,
  bloquear, caducar o reiniciar elimina la sesión local.
- La [guía oficial de autenticación](https://api-docs.grvt.io/api_setup/)
  documenta `POST /auth/api_key/login` y la cookie de sesión. La
  [API de trading](https://api-docs.grvt.io/trading_api/) documenta las tres
  consultas usadas: `aggregated_account_summary`, `get_sub_accounts` y
  `account_summary`. El cliente tiene una lista cerrada de esas rutas y
  rechaza cualquier otra; nunca importa un adaptador de órdenes.
- La respuesta de login no demuestra qué permisos tiene la clave. ORBIS
  señala `permission_verified=false`: el usuario debe verificar View en GRVT.
  No se solicita secret key ni firma EIP-712 para esta observación.
- Las respuestas públicas a la UI eliminan la dirección completa de cuenta,
  cookie y clave. Conservan cadenas `Decimal` exactas, fuente, hora de
  consulta y hora de evento de cada subcuenta. Un evento de trading con más
  de 60 segundos aparece como dato antiguo, no como estado actual.
- Las rutas `/real/exchanges/grvt/*` exigen la bóveda desbloqueada y origen
  local, devuelven `Cache-Control: no-store` y no escriben saldos ni
  posiciones en SQLite. La API externa recibe la clave y la IP del usuario;
  el cifrado local no oculta esa comunicación a GRVT.

## Límites

- No se ha probado una clave personal real. Las pruebas usan transporte y
  credenciales ficticias; la compatibilidad de una clave View concreta se
  comprobará al introducirla localmente.
- La equity agregada incluye subcuentas de trading; **no debe sumarse** de
  nuevo a su equity. Tampoco se combina automáticamente con la cartera
  manual, porque podría representar los mismos activos.
- La observación no reconstruye costes históricos, depósitos, retiros,
  órdenes, fills ni pagos privados de financiación. No habilita estrategias
  reales, aunque una clave tenga permisos superiores a View.

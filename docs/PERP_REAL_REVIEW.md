# Revisión del modelo perpetuo para GRVT REAL

Implementación v107. Es una comparación funcional de originales y configuración
privada, **no un permiso de ejecución**. El ejecutor del modelo nuevo sigue
pendiente; el ejecutor anterior conserva sus contratos y controles.

## Uso

1. Completa la validación del perfil y su prueba PAPER observada.
2. Desbloquea LAB, GRVT PAPER y la vista REAL con tus contraseñas locales.
3. En **Patrimonio → GRVT**, conecta una clave View. Abre **Revisar PAPER →
   REAL**, elige la prueba y la subcuenta y pulsa **Revisar**.
4. La tabla compara margen y apalancamiento. Las condiciones pendientes se
   muestran debajo. No hay botón de activación para este modelo.

No requiere que PAPER siga observando: una prueba cerrada se reproduce sin
arrancarla. No se exige beneficio positivo ni se interpreta una operación como
evidencia estadística suficiente. El recuento identifica la muestra observada.

## Decisiones

- `research.perp_live_review` reproduce el original PAPER completo y la
  validación LAB original, no sus checkpoints o un JSON recibido del navegador.
  Compara referencia, definición y **velas iniciales con el dataset LAB real**:
  recalcular las huellas de un seed alterado no permite sustituir ese histórico.
- La revisión fija sesión, secuencia, head, hashes de seed/origen y referencia
  OOS tipada. Un append durante HTTP conserva el prefijo inspeccionado y se
  indica como avance que exige nueva revisión. Reemplazar originales o cambiar
  una época de acceso invalida la consulta.
- Sólo cuentan operaciones cerradas y velas del flujo observado reproducido.
  GAP, HALTED, LIQUIDATED, muestra vacía y cierre estimado son condiciones
  explícitas. Un cierre estimado nunca suma operaciones a esta muestra.
- `integrations.grvt_position_config` interpreta el `results` del endpoint
  **Get All Initial Leverage**. Requiere configuración explícita del instrumento,
  decimales textuales exactos y límites coherentes. No infiere valores por
  defecto ni acepta duplicados. No reutiliza el modo agregado de account_summary
  como si fuese el modo de margen del instrumento.
- Dos lecturas con términos iguales, orden local coherente y un máximo de cinco
  segundos desde el inicio de la primera consulta. La API no proporciona hora
  de evento de esa configuración: son observaciones locales, no una prueba de
  continuidad ni de que otra aplicación no cambie el ajuste después.
- El cliente View añade únicamente esta lectura a su lista permitida. No
  contiene `set_position_config`, firma, orden ni transferencia. La comparación
  no modifica el apalancamiento del exchange.
- POST `/real/exchanges/grvt/perp-review` exige origen local, token REAL y
  LAB/PAPER cifrados y desbloqueados. Los dos HTTP privados se ejecutan sin
  retener mutex REAL o transacción. Bloquear, desconectar o reconectar invalida
  la identidad antes de publicar. Respuesta sin caché.
- Recibo tipado `orbis.grvt.perp.live.review.v1`, hash canónico, autoridad NONE,
  ejecución no implementada y presupuesto REAL independiente obligatorio. No
  copia el capital ficticio ni crea concesiones o movimientos REAL. No hay
  nueva tabla ni migración.
- UI modular `perp-real-review.js`, estados y paginación, etiquetas breves,
  procedencia desplegable y descarte de respuestas antiguas al desconectar o
  desmontar. Credenciales y resultados no se guardan en almacenamiento web.

## Carrera de conexión encontrada durante la regresión

La carga inicial de estado MEXC podía devolver «desconectado» después de que
**Consultar MEXC** confirmase la nueva conexión. La respuesta antigua ocultaba
los controles y bloqueaba el acceso visual al formulario Trade. El registro del
fallo y un bucle con respuesta inicial retenida reproducen esa secuencia.

La vista REAL mantiene ahora una generación independiente de conexión para
MEXC y GRVT. Cargar, conectar, desconectar o bloquear invalida las respuestas
anteriores. Las respuestas de refresco verifican la misma conexión y el nodo
de pantalla antes de pintar. La prueba MEXC del bot y la nueva prueba GRVT
liberan deliberadamente un «desconectado» antiguo después de conectar y
comprueban que los controles actuales siguen disponibles. No cambia permisos,
claves ni decisiones de los motores financieros.

## Verificación

`test_grvt_position_config.py`, `test_perp_live_review.py` y
`test_perp_live_review_api.py`: formato oficial, precisión decimal, ámbito,
ausencia de configuración, duplicados, reloj/plazo, discrepancias, reproducción
fría, GAP/cierre estimado, originales alterados con hashes recalculados, cambios
de sesión/fuente y revocación durante HTTP.

`perp_live_review` en `scripts/verify_orbis.py`: navegador y API reales, bases
cifradas propias, parser privado real con transporte controlado. Compara,
detecta discrepancias, descarta una respuesta tardía, limpia al bloquear y se
verifica en escritorio/móvil. No usa credenciales ni fondos del propietario.
La evidencia y el alcance de regresión se registran en el informe de la entrega.

## Lo siguiente para ejecutar este modelo

La coincidencia de perfil no basta para autorizar trading. Quedan fuente de
señales nueva sin falsificar filas del motor anterior, presupuesto/tamaño REAL
independientes, contrato versionado de autorización RAM, preflight actual por
instrumento antes de cada entrada y conciliación de fills, financiación,
salidas, pérdida de conexión, STOP y recuperación. El perfil experimental puede
contener apalancamiento o supuestos no ejecutables; no se adapta silenciosamente.
El piloto humano mainnet permanece separado y pendiente.

Referencias oficiales consultadas el 5 de noviembre de 2026:
[lectura de configuración](https://api-docs.grvt.io/trading_api/#get-all-initial-leverage),
[campos](https://api-docs.grvt.io/schemas/initial_leverage_result/) y
[escritura firmada independiente](https://api-docs.grvt.io/trading_api/#set-position-config).

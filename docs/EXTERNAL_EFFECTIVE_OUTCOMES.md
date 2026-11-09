# Resultado efectivo de una revisión externa

Desarrollo sobre REAL 78, verificado en el alcance indicado. Sin publicación ni uso de
cuentas, fondos o claves del propietario. El objetivo completo permanece abierto.

## Semántica y procedencia

`dex_external_outcomes` autentica observación, certificado, booking, enlace al
primer criterio y su prefijo exacto. Su testigo `external.outcome.witness.v1`
conserva todos esos IDs/digests y el tiempo de aceptación financiera. No necesita
RPC, direcciones, claves ni reproducción de saldos dentro del lease para exponer
estos hechos. No usa las cifras guardadas como un saldo autoritativo.

El resultado es **EXTERNAL_REVIEWED**, distinto de EXECUTED, CANCELLED y REVERTED.
Tiene gas y recibo reales, pero `filled=false`, ningún efecto de estrategia y
ningún permiso de trading. Un `as_of` anterior a la aceptación continúa pendiente.
Los datos presentes se autentican incluso si son posteriores a ese corte; la
corrupción no puede esconderse como ausencia de un hecho futuro.

`operator_outcome` y `outcome_witnesses` requieren opt-in `include_external`.
Los lectores históricos anteriores conservan su gramática. El testigo nuevo usa
una rama `external_review`, no finge ser una resolución de llamada conocida.
El estado original y sus decisiones causales permanecen inmutables.

## Admisión y permisos

La guarda de actor exige que todas sus observaciones tengan aceptación válida.
Prohíbe siempre el nonce consumido y los anteriores; sólo permite comprobar uno
posterior. Las guardas de recibo/nonce únicos siguen impidiendo reasignar la misma
transacción a otra solicitud. Una revisión no concede firma ni reinicia nada.

La concesión conserva gas realmente pagado, exceso y presupuesto remanente,
contándolo una vez. Distingue observaciones pendientes y revisadas. Su estado
STOPPED no se modifica. Un bot detenido necesita una nueva conexión limitada,
una nueva autorización y estado físico finalizado compatible para continuar.
La conexión conserva sólo identificadores escalares fuera de su sesión SQL.

## LONG y DCA

Nuevos perfiles de continuidad en la misma asignación:

- `orbis.dex.bot.long.v7`.
- `orbis.dex.bot.dca.v4`.

Ambos vinculan el testigo externo a la continuación y a sus futuras decisiones,
sin reescribir formatos anteriores. El estado físico del ejecutor debe coincidir
con los recibos verificados; una declaración manual no suministra contadores de
posición. El gas externo se conserva, sin aumentar entradas, compras, coste ni
beneficios del bot. El nonce de contrato tampoco aumenta por una llamada ajena.

DCA reserva la clave de la activación observada, aunque el bot no haya comprado.
EXTERNAL_REVIEWED no es ABANDONED_UNSENT: no libera esa clave ni reinicia límites,
caídas extra o presupuesto mensual. Sólo los fills FINALIZED consumen importe de
compra. El recibo externo aporta su gas y tiempo reales. Los formatos DCA 1/2/3
rechazan ese estado; sólo el nuevo perfil lo admite.

LONG v7 se reproduce íntegramente mientras su caché no tenga todas las
dependencias del archivo financiero. No se conserva un resultado memorizado que
pueda ocultar una aceptación, original o enlace alterados. El rendimiento con
linajes largos requiere su medida antes de considerar terminado el objetivo.

## Lectores actuales y límites

El supervisor muestra estado revisado y deja de presentar el recibo como pendiente,
manteniendo apagada la ejecución hasta la nueva autorización. El catálogo de fills
cuenta revisiones y gas externos por separado; no crea un marcador BUY/SELL falso.
El historial manual de swaps identifica el origen EXTERNAL_OBSERVATION y la
aceptación contable de la custodia correcta.

La renovación a otra política/asignación usa después perfiles nuevos de control,
rollover y binding, con LONG v8/DCA v5: [decisión y verificación](EXTERNAL_POLICY_RENEWAL.md).
El supervisor ofrece la revisión; configurar y continuar siguen requiriendo el
recibo y permiso nuevos. Faltan aceptación conjunta de varios pendientes,
interfaz completa, rendimiento ampliado y verificación de publicación.

## Evidencia en curso

La primera tanda tuvo 13 casos correctos y dos fallos al reconectar: una captura
de archivo expiraba atributos ORM y se leía `parent.grant_id` después de cerrar
SQL. Se retiene el escalar dentro del lease. La segunda tanda pasa sus 15 casos.
Incluye reemplazos firmados de bots reales de la EVM propia, aceptación financiera,
LONG/DCA detenidos y rearmados con permiso nuevo, sin fills ficticios y con copia
cifrada fría. La ampliación actual prueba operativa posterior y corrupción de
testigos junto con las regresiones financieras. Su primera tanda integrada
terminó con 52 correctos y un fallo DCA; una repetición aislada confirmó el fallo.
Dos sondas de diagnóstico localizaron el rechazo en `finalized_block`: el recibo
propio estaba 31 segundos por delante del reloj congelado del test, mientras
Anvil avanzaba con el tiempo real del replay financiero. El test observa ahora
la hora minada antes de verificar el recibo. La validación productiva queda igual.

La prueba de corrupción vuelve a encadenar también los hashes de decisiones y
cabecera, manteniendo alterado el testigo de aceptación independiente. La tanda
`external-effective-regression-dev-r8` termina con **182 casos / 22 módulos**,
cuatro salidas 0, identidades cotejadas y las mismas 1.302 fuentes congeladas.
Incluye los cinco casos del módulo nuevo y la compra DCA posterior. Es evidencia
del alcance listado; no es una tanda completa del proyecto ni prueba mainnet
con fondos del propietario.

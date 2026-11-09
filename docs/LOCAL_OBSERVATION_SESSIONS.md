# Plazos locales de investigación y PAPER

En **Ajustes → Privacidad y seguridad**, cada almacén LAB, DCA, Spot PAPER y
GRVT PAPER ofrece **Plazo** al crear o desbloquear su archivo cifrado:
15 minutos, 1/4/24 horas y 7/30 días. El valor inicial siempre es quince minutos;
elegir uno no cambia los demás. PAPER manual y REAL conservan sus controles.
Si aparece «sin cifrar», usa **Proteger legado** y tu propia contraseña antes
de desbloquearlo; un estado `unlocked: false` no demuestra que esté cifrado.

| Trabajo | Almacenes necesarios |
| --- | --- |
| Investigar o reproducir originales históricos | LAB |
| Preparar una prueba Spot | LAB y Spot PAPER |
| Observar una sesión Spot ya creada | Spot PAPER; LAB para consultar su procedencia/comparación |
| DCA programado | DCA **y LAB** al procesar cada compra: se verifican los originales |
| Preparar una reserva de derivados | LAB y GRVT PAPER |
| Observar derivados después de activarlos | GRVT PAPER; el origen LAB ya quedó fijado/verificado al activar |

Para DCA diario, selecciona un plazo que cubra las próximas compras tanto en
LAB como en DCA. Tener sólo DCA desbloqueado durante siete días no basta si
LAB caduca antes. Si falta una clave, no se compra ni se omite la verificación.
Si se pierde la ventana de ejecución, la sesión pasa a GAP sin compras
retrospectivas.

El vencimiento es fijo y monotónico desde cada desbloqueo. Consultar, navegar
o la actividad del observador no lo renueva. El estado muestra el tiempo
restante. La contraseña vive sólo en memoria y se borra del formulario al
solicitar el desbloqueo; no se guarda una preferencia que seleccione de nuevo
un plazo largo. Cerrar el proceso elimina las claves y reabrir empieza bloqueado.

**Bloquear** cancela también los desbloqueos pendientes: una comprobación
antigua de contraseña no puede volver a publicar la clave después del bloqueo.
Una solicitud de desbloqueo posterior sustituye a las anteriores.
El bloqueo impide nuevas lecturas/escrituras; un trabajo que ya abrió una
conexión puede terminar según el contrato del almacén. No vende posiciones.
**Parar** una estrategia es otra acción: conserva fondos, operaciones y estado
final. Las sesiones STOPPED no se reactivan al desbloquear. Spot y DCA mantienen
la política previa de seguimiento de sesiones ACTIVE y sus ventanas/GAP;
el nuevo motor de derivados observado requiere otra activación explícita y
no reanuda sesiones guardadas.

Un plazo largo no garantiza servicio 24/7, conectividad ni precio ejecutable
entre observaciones. Mantén ORBIS abierto y revisa cobertura, diario y fechas.
Caducar/bloquear puede dejar exposición ficticia pendiente, e impedir escribir
un último evento de interrupción si la clave ya se olvidó. No se inventan
ventas, stops ni resultados para completar esos intervalos.

Los plazos de LAB/PAPER no conectan una wallet, no conceden permisos Trade o
Transfer ni cambian límites o claves REAL. El paso a ejecución real mantiene
su revisión y autorización separadas.

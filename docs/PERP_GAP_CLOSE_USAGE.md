# Cerrar una posición PAPER después de una interrupción

1. Desbloquea **GRVT PAPER** en Ajustes. En **PAPER de derivados** del Laboratorio,
   o **Perpetuos PAPER → Estrategias validadas**, busca la sesión interrumpida.
2. Pulsa **Estimar cierre**. Revisa la posición y su gráfico original. Marca
   **Aceptar intervalo desconocido** si quieres calcular ese escenario.
3. Pulsa **Guardar cierre estimado**. ORBIS obtiene una cotización actual,
   especificación/reglas actuales y financiación verificable desde el último
   punto conocido. No coloca órdenes en un exchange.
4. Revisa el patrimonio, costes, financiación y diario del **cierre estimado**.
   La marca violeta `≈ cierre` usa su precio/tiempo; `≈ LQ` indica liquidación
   estimada en un punto MARK conocido. Las marcas observadas permanecen aparte.
5. **Ver** conserva el resultado observado original, con su posición al perder
   cobertura. **Ver cierre** reproduce la estimación guardada. No se reanudan
   entradas ni se sobrescriben operaciones originales.

El escenario supone conservar la posición hasta los puntos conocidos. No
podemos saber si un stop o una liquidación se habría ejecutado entre ellos.
Por eso conserva la etiqueta `ESTIMATED_GAP_CLOSE` y autoridad REAL `NONE`.
Una liquidación deducida en un punto de financiación también es reconstrucción,
con su tiempo histórico y el instante actual en que se calculó separados.

Si faltan tasas, cambia el ancla, caduca la clave o la posición completa no es
cerrable con las reglas actuales, ORBIS informa del error y no inventa un cierre.
Una observación activa en esta u otra instancia impide el cierre; el intervalo
sigue perteneciendo al observador hasta que se detenga y libere su propiedad.

Si se pierde la respuesta, **Consultar cierre** recupera el registro, sin otro
POST ni otra compra/venta ficticia. **Dejar consulta** sólo quita su identificador
local; no deshace un cierre ya confirmado. La biblioteca conserva su enlace.
Tras reiniciar, desbloquea y usa **Ver cierre**: repite la misma evidencia, sin
consultar otra cotización ni activar el bot.

**Exportar cierre** descarga un JSONL que incluye la cadena original completa,
el recibo público y el cálculo nuevo. Su verificador exige ambos cierres/digests
obligatorios y vuelve a calcular los dos motores; una descarga truncada no es un
informe válido. La copia SQLCipher conserva también estos registros.

Este mecanismo permite finalizar un escenario PAPER con exposición pendiente.
No reconcilia órdenes reales, no acredita rentabilidad alcanzada y no habilita
el nuevo motor para operar REAL. [Diseño](PERP_GAP_CLOSE_PLAN.md).

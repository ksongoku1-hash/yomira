# Protocolo de resolución de bugs

**Estado:** APPROVED — K004 aprobado formalmente por el usuario el 2026-09-25.

## 1. Ciclo obligatorio

```text
REPRODUCIR
  ↓
ESCRIBIR TEST QUE FALLE
  ↓
IDENTIFICAR CAUSA RAÍZ
  ↓
APLICAR FIX
  ↓
COMPROBAR QUE EL TEST PASA
  ↓
EJECUTAR SUITE PERTINENTE
  ↓
AÑADIR REGRESSION TEST PERMANENTE
  ↓
REVISAR DIFF Y REGISTRAR CAMBIO
```

Está prohibido cambiar cosas hasta que parezca funcionar sin reproducir el fallo y sin un test que primero falle. Si el fallo no puede reproducirse localmente, registrar las condiciones observadas y preparar una reproducción controlada o un fixture antes de declarar causa.

## 2. Registro inicial

Cada bug debe registrar, cuando aplique:

- resumen observable y pasos reproducibles;
- versión de Yomira, Windows y componente;
- resultado esperado y resultado real;
- `trace_id`, `job_id`, `series_id`, `chapter_id`, `source_id`;
- etapa, código de error, intento y duración;
- impacto sobre contenido, DB, exportación o confidencialidad;
- frecuencia y evidencia sanitizada.

No adjuntar páginas de manga, CBZ, cookies, tokens o contraseñas a un reporte ordinario.

## 3. Reproducción y prueba que falla

1. Congelar el escenario, fixture y precondiciones.
2. Ejecutar reproducción y guardar logs/contexto seguro.
3. Escribir la prueba más pequeña que expresa el comportamiento esperado.
4. Confirmar que falla por la causa observada, no por configuración ni por un test inválido.
5. Para incidentes de integridad, usar fixture conservado y verificar hashes/archivos antes y después.

## 4. Causa raíz y reparación

- Identificar el límite de componente propietario y por qué su protección falló.
- Revisar impacto en DB, temporales, CBZ publicados, hashes, progreso y Support Bundle.
- Si el arreglo cambia comportamiento fundamental, crear/actualizar ADR antes de implementarlo.
- Mantener cambios acotados a la causa; no eliminar validaciones para silenciar síntomas.
- Especificar recuperación de datos si pudo existir contenido afectado.

## 5. Verificación y cierre

- El test nuevo debe pasar después del fix.
- Ejecutar las pruebas pertinentes de regresión; ante cambio de frontera, cubrir ambos lados del contrato.
- Añadir regresión permanente y revisar diff, logs y códigos de error.
- Documentar commit/cambio y evidencia. No cerrar sin causa raíz y resultado de verificación.

## 6. Severidad y release

- P0: pérdida o destrucción de datos.
- P1: corrupción/integridad.
- P2: funcionalidad mayor rota.
- P3: problema menor.
- P4: visual/cosmético.

Un release V1 requiere cero P0/P1 conocidos y cero P2 sin aceptación explícita. Cualquier P0/P1 bloquea release hasta resolverlo o emitir una excepción aceptada formalmente según el gate.


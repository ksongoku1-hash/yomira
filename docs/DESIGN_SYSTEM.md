# Yomira V1 — Design System

**Estado:** APPROVED — Fase 1 alcanzó PASS en K013; ver PHASE_01_REPORT.md.
**Trazabilidad:** K005, K009, K012.  
**Fuentes normativas:** YOMIRA_V1_SPEC.md §§44–47 y 103; YOMIRA_MASTER_EXECUTION_PLAN.md, K005 y K009–K012.  
**Alcance:** especificación visual y de interacción; no contiene frontend ni código ejecutable.

## 1. Principios visuales

1. La portada y la lectura del contenido tienen prioridad visual sobre decoración.
2. Las acciones de la aplicación deben comunicar claramente su estado: disponible, en curso, completada, pendiente de revisión o fallida.
3. No comunicar estados únicamente mediante color; acompañarlos con texto, icono o forma reconocible.
4. Mantener jerarquía y controles estables entre PC, tablet, móvil y navegador de e-reader.
5. Los datos locales de la biblioteca deben seguir siendo navegables cuando una fuente o Internet no estén disponibles.
6. Evitar efectos que añadan ruido, dificulten el refresco de pantalla o eleven trabajo de renderizado sin aportar información.

## 2. Paleta normativa

Los siguientes valores vienen de la especificación y no son propuestas nuevas:

| Token | Valor | Uso previsto |
|---|---|---|
| background-primary | #0B0D10 | Fondo principal de la aplicación |
| background-secondary | #101318 | Paneles, navegación y zonas secundarias |
| surface-card | #15191F | Tarjetas y filas agrupadas |
| surface-elevated | #1A1F26 | Diálogos, popovers y superficie elevada |
| text-primary | #F4F6F8 | Texto principal sobre superficies oscuras |
| text-secondary | #98A1AD | Metadatos y ayuda secundaria |
| accent | #00D66B | Acción primaria, selección y foco cuando corresponda |

La implementación debe conservar los tokens semánticos para evitar valores de color dispersos. El uso de accent para un estado nunca reemplaza una etiqueta textual.

## 3. Colores semánticos propuestos

Los valores siguientes son una propuesta de Fase 1 y requieren revisión visual y de contraste antes de congelarse:

| Token propuesto | Valor inicial | Señal además del color |
|---|---|---|
| status-success | #00D66B | Texto “Completado” o marca de verificación |
| status-warning | #F2B84B | Texto “Revisar” o icono de advertencia |
| status-error | #FF6B6B | Código de error, texto y acción disponible |
| status-info | #61A8FF | Etiqueta informativa o icono |
| border-subtle | #2A3038 | Borde visible en superficies oscuras |

Antes de K013, revisar contraste de texto y controles con los fondos reales, modos de zoom y e-reader. Si un color no alcanza legibilidad, corregir el token y documentar la decisión en esta especificación antes de escribir UI funcional.

## 4. Tipografía propuesta

- Fuente de interfaz: Segoe UI cuando esté disponible, con fallback del sistema sans-serif.
- No incorporar fuentes propietarias ni descargar tipografías desde Internet.
- Texto base: 16 px propuestos; metadatos y etiquetas nunca deben quedar ilegibles al aumentar zoom.
- Escala propuesta de títulos: 12, 14, 16, 20, 24 y 32 px.
- Usar peso y tamaño para jerarquía. Evitar mayúsculas sostenidas en títulos extensos.
- En lector, el tamaño del contenido se controla con zoom/ajuste; la tipografía de controles no debe reducir el área de lectura sin necesidad.

La escala es un punto de partida sujeto a prueba a 100 %, 150 % y 200 % de zoom. El zoom no debe recortar contenido ni ocultar controles esenciales.

## 5. Espaciado, forma y superficies propuestos

- Unidad base propuesta: 4 px; pasos: 4, 8, 12, 16, 24, 32 y 48 px.
- Radio propuesto: 8 px para tarjetas y controles, 12 px para paneles amplios, radio completo para chips.
- Contorno propuesto: 1 px usando border-subtle para separar superficies sin depender de sombras.
- Elevación: preferir diferencia de superficie y borde. Sombras discretas solo en pantalla LCD; suprimirlas en modo e-reader.
- No aplicar blur ni transparencias decorativas. El modo e-reader elimina también animaciones y minimiza transiciones.
- Estados disabled deben conservar texto legible y explicar por qué la acción no está disponible cuando no resulte obvio.

## 6. Objetivos responsive

Los objetivos por número de portadas proceden de la especificación. Los anchos de corte siguientes son propuestas, no restricciones aprobadas:

| Clase propuesta | Ancho de viewport inicial | Objetivo de portadas | Comportamiento |
|---|---:|---:|---|
| Móvil | menos de 640 px | 2–3 | Navegación compacta; controles táctiles; texto sin desbordar |
| Tablet / e-reader | 640–1023 px | 4 en tablet; 2–3 en e-reader | Navegación comprimida, funciones completas |
| Desktop | 1024 px o más | 5–7 | Navegación superior completa y área de contenido amplia |

El diseño debe responder al espacio disponible y al ancho mínimo legible de cada tarjeta, no al nombre o modelo del dispositivo. Validar orientación vertical y horizontal. Si la densidad objetivo no cabe con títulos y controles legibles, se prioriza legibilidad y se baja el número de columnas.

## 7. Foco, teclado y objetivos táctiles

- El foco de teclado debe ser siempre visible, continuo y separado del contenido adyacente.
- Propuesta de foco: contorno sólido de 3 px, separación de 2 px y una señal adicional en controles de selección.
- Orden de tabulación sigue el orden visual y el flujo de trabajo; no atrapa el foco fuera de un diálogo modal.
- Propuesta de tamaño mínimo de objetivo táctil: 44 × 44 px CSS; se permite más espacio en modo e-reader.
- No depender de hover para revelar una acción. Todo control hover-only debe tener alternativa para teclado y touch.
- Cada control debe exponer nombre accesible, estado, valor y motivo de deshabilitación cuando aplique.

## 8. Patrones de superficie

- Tarjeta de obra: portada con proporción propuesta 2:3, título, metadata secundaria y señales de estado claramente separadas.
- Chips: filtros y fuentes; texto legible, estado seleccionado perceptible sin color exclusivo.
- Progreso: porcentaje o etapa textual más indicador visual. No mostrar “completado” antes de confirmación del sistema.
- Error: mensaje breve, código estable visible en detalle y acción explícita de reintento cuando proceda.
- Skeleton: reservado a cargas iniciales; no reemplaza contenido local válido durante una actualización.
- Modal: para decisiones que requieren contexto o confirmación; evitar modales para notificaciones rutinarias.
- Toast: confirmación breve no crítica. Los errores que requieren acción permanecen visibles en el área relacionada.

## 9. Regla de modo e-reader

El modo e-reader comparte el lenguaje visual, pero usa un perfil estable: sin blur, sin animaciones, transiciones mínimas, sombras y transparencias eliminadas, contraste alto, objetivos táctiles grandes y precarga reducida. Los ajustes deben poder identificarse y revertirse sin alterar archivos de origen ni progreso de lectura.

## 10. Criterios K005 y K012

K005 queda documentado cuando paleta, tipografía, espaciado, radios, estados, foco, breakpoints y objetivos táctiles hayan sido revisados y aprobados. K012 queda documentado cuando el recorrido completo pueda operarse con teclado, zoom y touch, y los estados no dependan exclusivamente del color.

**Base de implementación aprobada:** colores semánticos, fuente fallback, escala tipográfica, espaciado/radios, anchos de corte y tamaño mínimo de foco/touch se aceptaron como valores iniciales. Se conserva la paleta normativa y las metas responsive de la especificación; los cambios fundamentales requieren el ADR correspondiente.

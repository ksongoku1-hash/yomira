# ADR-0059 — Contrato HTTP normal del Mock Manga Server

## Status

Accepted for K072 implementation — 2026-09-26.

## Context

K072 necesita una respuesta normal contra la que se puedan comparar los escenarios de falla K073–K079. Los endpoints deben ser reproducibles y representar los valores del contrato SDK sin depender de un servicio público. El MockConnector de K069 es un fixture de memoria independiente, no un cliente HTTP.

## Decision

- Cada `MockMangaServer` recibe un `MockScenario` inmutable al construirse. El valor por defecto es `NORMAL`; una instancia no cambia de escenario mientras tiene requests activos.
- El modo normal usa rutas versionadas bajo `/api/v1`: Search, detalle de serie, capítulos, referencias de páginas y bytes de cada página.
- `GET /api/v1/search?q=fixture` devuelve el único registro sintético. La comparación del término es exacta; otras consultas producen una lista vacía.
- La serie y el capítulo son los mismos IDs sintéticos del SDK: `mock-series-1` y `mock-chapter-1`. El detalle incluye metadata de los siete campos existentes; la respuesta de capítulo incluye las etiquetas y orden exactos más un `page_count` declarado de 2.
- La respuesta normal lista exactamente dos referencias distintas y relativas a rutas de página de este servidor. Ambas rutas devuelven un PNG pequeño válido y determinista con `Content-Length` exacto y `Cache-Control: no-store`.
- Referencias de serie, capítulo, página o rutas no conocidas responden 404 genérico. Las listas JSON conservan su orden; escenarios posteriores modificarán respuestas solo cuando se seleccionen expresamente.
- Search, metadata, capítulos y páginas son respuestas de fixture para test; no se promete compatibilidad con una fuente comercial ni se envían estas rutas a usuarios finales.

## Alternatives Considered

- **Usar el MockConnector directamente como transporte:** no ejercita HTTP, cabeceras, desconexión ni cuerpos parciales requeridos en fases posteriores.
- **Variar el escenario global durante requests activos:** produce carreras y hace difícil atribuir una respuesta a su configuración; instancias separadas mantienen cada test aislado.
- **Usar rutas absolutas con localhost/puerto fijo en las referencias:** acopla el fixture a DNS y puertos estables; referencias relativas se resuelven contra el endpoint efímero del test.
- **Servir imágenes grandes como modo normal:** eleva memoria y latencia de todas las pruebas; los casos de tamaño y bomb pertenecen a escenarios explícitos posteriores.

## Consequences

- Un cliente HTTP de test puede cubrir búsqueda, metadata, paginación y lectura de bytes en loopback sin acceso a Internet.
- La implementación futura debe resolver referencias relativas con `base_url` y verificar status, tipo de contenido, longitud y bytes por separado.
- El PNG de 2×2 sirve solo para transporte y decodificación normal; no representa calidad visual de lectura.
- Las rutas son una API del fixture, no del backend Yomira ni una decisión sobre protocolos de fuentes reales.

## Failure Modes

- Consulta no incluida: `200` con lista vacía.
- Ruta o referencia desconocida en modo normal: `404` genérico.
- El fixture normal debe fallar si los bytes no decodifican como PNG o si longitud/cabeceras no reflejan el cuerpo servido.
- Una respuesta inválida debe agregarse como escenario separado y no reemplazar silenciosamente el fixture normal.

## Reversal Strategy

Cambiar IDs, rutas, campos de metadata, número de páginas, consulta exacta o formato normal requiere actualizar este ADR y los contract tests. Las rutas no deben migrarse sin actualizar simultáneamente todo cliente de test.

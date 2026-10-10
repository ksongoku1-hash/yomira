# ADR-0091 — Presentación y controles del lector

## Estado

Accepted — 2026-09-27. Concreta K174–K178 sin alterar los bytes ni el orden persistido del CBZ.

## Contexto

La SPEC requiere página única, vertical, doble página, RTL, zoom, ajuste al ancho/alto y pantalla completa. Define las capacidades, pero no el emparejamiento de páginas, los límites de zoom ni la persistencia de preferencias. El diseño de Fase 1 establece iniciar en página única y conservar las preferencias explícitas; la base ya dispone de la tabla genérica `settings`.

## Decisión

1. La preferencia persistida `reader.mode` admite `single`, `vertical` y `double`; su default es `single`. `reader.rtl` es booleano y su default es `false`. Se guardan únicamente tras una selección explícita. Cada ajuste persiste sin modificar CBZ, progreso ni orden de páginas.
2. `double` presenta dos páginas consecutivas desde la página actual restaurada. Siguiente/anterior avanza o retrocede dos índices, con límites en el capítulo; una página final sin pareja se presenta sola. RTL invierte solo la posición visual del par. No cambia los ordinales, el progreso ni el orden ZIP. No se reconocen spreads especiales en V1; esa clasificación sigue siendo K135.
3. El zoom manual comienza en 100 %, cambia en pasos de 25 % y queda entre 50 % y 300 %. Los controles visibles permiten acercar, alejar y restablecer 100 %. Fit width ajusta el ancho del área de lectura; fit height ajusta su alto. El ajuste puede dejar desplazamiento en el otro eje. El zoom y fit se reinician a 100 % al abrir un capítulo.
4. Pantalla completa se solicita sobre el área completa del lector, incluidos los controles. Si el navegador la rechaza o no la soporta, la página permanece usable y anuncia el fallo con un mensaje recuperable; Escape y la salida nativa del navegador se respetan.
5. En una ventana estrecha, el par se apila verticalmente para que ambas páginas continúen visibles y navegables. No se oculta ni se salta una página.

## Alternativas consideradas

- Emparejar siempre desde el ordinal uno: puede desplazar una página restaurada de K179 y confundir el progreso mostrado.
- Tratar la portada o spreads especiales de forma distinta: requiere metadatos/clasificación no disponible en V1 y adelantaría K135.
- Reordenar el ZIP al activar RTL: cambia contenido persistido sin necesidad y puede corromper la interpretación de índices.
- Zoom ilimitado o solo CSS transform: deja el contenido/controles fuera del viewport sin una cota ni ajuste geométrico claro.
- Persistir zoom o pantalla completa: añade estado por dispositivo sin requisito; el zoom se restablece al abrir el capítulo y fullscreen es solo de la sesión activa.

## Consecuencias

- El progreso sigue asociado al ordinal original; entrar/salir de modos no escribe progreso por sí solo.
- La configuración persistida requiere validación tipada y errores trazables. Un registro inválido no se sustituye silenciosamente por el default.
- `double` no afirma soporte de spreads detectados; muestra pares consecutivos de páginas.

## Reversión

Una ADR sucesora puede cambiar pairing, RTL, rangos, fit o alcance de fullscreen. Debe conservar los índices y bytes originales de capítulos ya publicados.

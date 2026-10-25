# Inicio: continuar por manga — 16 de octubre de 2026

- Inicio muestra hasta cuatro mangas distintos, con el último punto guardado de cada uno. El servidor agrupa antes de limitar, de modo que los capítulos recientes de un mismo manga no ocultan las otras obras.
- El punto de lectura se elige por la fecha de guardado, con el mismo desempate estable por capítulo que ya tenía el historial. Se conservan los puntos al 0 % y los capítulos terminados; no se retrocede a un capítulo antiguo por estar incompleto.
- Una tarjeta cuyo último capítulo está al 100 % indica «Capítulo terminado» y abre la ficha para ver sus capítulos. No se marca toda la obra como terminada. El botón principal de Inicio usa el manga más reciente cuyo último capítulo aún no se haya terminado.
- El modo nuevo de la API es opcional. Las fichas siguen recibiendo el historial por capítulos, sin agrupar; el historial guardado no se modifica. El cliente valida que el resultado agrupado no repita mangas.

Validación: 24 pruebas del progreso y las vistas recientes y 155 de interfaz, cliente API y ficha aprobadas. Se comprueban el límite, los empates, un manga antiguo que quedaba fuera de los cuatro capítulos recientes, un último capítulo terminado, la compatibilidad del historial anterior y una única consulta SQL acotada. TypeScript, ESLint, Ruff, Pyright y compilación correctos.

Activado en la copia local de E: tras comprobar que no había descargas pendientes ni en curso. Se conservaron las 34 posiciones de lectura y los ajustes tras el reinicio. La revisión real muestra cuatro mangas distintos y el capítulo terminado de Death Note. Comprobado a 320 píxeles sin desbordamiento horizontal. No se abrieron capítulos reales ni se iniciaron descargas durante esta comprobación.

Se guardaron las fuentes anteriores, la interfaz anterior y una copia privada de recuperación de la base de datos en el archivo local de Yomira. El ZIP de cambios solo contiene código y estas notas, sin datos personales.

No se añadieron dependencias ni migraciones. No se recompiló el instalador ni se publicó una Release de GitHub.

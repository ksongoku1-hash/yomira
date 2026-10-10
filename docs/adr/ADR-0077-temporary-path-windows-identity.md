# ADR-0077 — Identidad segura de `temporary_path` en Windows

## Status

Accepted — decisión de Arquitectura Principal el 2026-09-26 para completar el contrato de K092 y fijar los requisitos de K093. No se atribuye esta decisión al usuario.

## Context

ADR-0035 establece que `temporary_path` es una ruta relativa POSIX bajo `YomiraData/temp/`, no puede escapar de su raíz y debe ser única mientras esté registrada. El modelo actual conserva Unicode y usa un índice `UNIQUE` binario; dos cadenas con distinta capitalización pueden pasar ese índice y señalar el mismo destino en un directorio Windows que no sea sensible a mayúsculas. La especificación exige soporte Unicode y protección frente a nombres reservados y nombres excesivamente largos. El manifest K092 debe representar rutas existentes sin alterarlas, mientras que K093 deberá hacer persistente la reserva global antes de escribir archivos.

La documentación de Microsoft indica que no se debe asumir sensibilidad a mayúsculas en los nombres, que Windows permite Unicode, que reconoce `COM¹`/`COM²`/`COM³` y `LPT¹`/`LPT²`/`LPT³` como nombres de dispositivo reservados incluso con extensiones, y que la longitud máxima de un componente depende del sistema de archivos. NTFS y ReFS documentan un máximo de 255 caracteres por nombre; las APIs Unicode de Windows operan con UTF-16. Las fuentes oficiales consultadas se enlazan al final de esta ADR.

## Decision

- La identidad de una ruta temporal registrada se calcula como `temporary_path.casefold()` usando el valor Unicode exactamente recibido. No se aplica NFC, NFD, NFKC, traducción de separadores, cambio de capitalización ni otra normalización al valor guardado o serializado. La ruta original se conserva byte por byte en UTF-8 cuando se representa en el manifest; la clave derivada solo sirve para comparar unicidad.
- Dos `temporary_path` no nulos son duplicados cuando sus claves `casefold()` son iguales. Esta regla aplica a las rutas relativas completas con los separadores POSIX `/` que valida ADR-0035. `null` no reserva una ruta. La comparación `casefold()` es una regla determinista y conservadora del producto; puede rechazar algunos pares Unicode que un volumen concreto de Windows trataría como distintos. No se permite resolver ese falso positivo reescribiendo o descartando una ruta existente: la reserva conflictiva falla y las cadenas fuente permanecen intactas.
- Cada componente de `temporary_path`, medido en unidades de código UTF-16, puede tener como máximo 255 unidades; 255 se acepta y 256 se rechaza. Los caracteres suplementarios ocupan dos unidades. No se admiten sustitutos aislados ni componentes vacíos, `.` o `..`, separadores inversos, caracteres prohibidos por Windows ni componentes terminados en espacio o punto. La ruta sigue siendo relativa, usa `/` y debe permanecer bajo `YomiraData/temp/`. Este límite por componente no elimina el límite de longitud de la ruta absoluta resultante; K093 y las operaciones de filesystem posteriores deben comprobar la ruta resuelta y manejar de forma trazable cualquier límite adicional del volumen o de la API.
- Se rechazan nombres de dispositivo reservados en cualquier componente, independientemente de la extensión y la capitalización: `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, además de `COM¹`, `COM²`, `COM³`, `LPT¹`, `LPT²` y `LPT³`. El nombre base se evalúa antes de la primera extensión; por ejemplo, `NUL.txt` y `COM¹.part` son inválidos.
- K092 preserva el valor original y aplica las reglas anteriores al crear, serializar y leer un manifest. Rechaza el manifest completo si dos rutas no nulas tienen la misma clave `casefold()`. K092 no accede al filesystem ni modifica SQLite.
- K093 debe persistir la clave `temporary_path.casefold()` bajo una restricción global de unicidad para rutas no nulas, calcularla dentro de la misma transacción que registra la ruta y asegurar que una reserva o creación de destino no trunque ni reemplace un archivo existente. La migración debe preflightar todas las rutas ya registradas antes de construir el índice: ante una colisión, informa las identidades de las filas implicadas y falla de forma segura. No renombra, elimina ni reescribe rutas, filas o archivos existentes. Si no hay colisiones, conserva cada `temporary_path` exactamente y agrega la representación derivada requerida por el índice.
- La regla está limitada a `temporary_path`. `remote_reference`, títulos, nombres visibles de biblioteca o exportación, y los demás metadatos mantienen el tratamiento Unicode que definan sus propios contratos.

## Alternatives Considered

- Conservar únicamente unicidad binaria SQLite: permite filas diferentes como `Page/A.part` y `Page/a.part` que pueden referirse al mismo archivo en Windows; se rechaza por no cumplir la unicidad segura de ADR-0035.
- Restringir todas las rutas a ASCII minúscula: simplifica la comparación, pero rechaza rutas Unicode válidas que el modelo actual y la especificación permiten. No se impone como requisito retroactivo.
- Normalizar o reescribir rutas Unicode para producir una clave canónica almacenada en lugar del valor original: podría alterar una ruta ya persistida o señalar otros bytes; se rechaza para preservar datos.
- Aplicar `casefold()` como si reprodujera exactamente la comparación de cada volumen Windows: la transformación puede ser más conservadora que la comparación de un filesystem concreto. Se acepta el posible rechazo falso de una reserva nueva antes que permitir dos registros ambiguos; el valor guardado nunca se altera.

## Consequences

- Las rutas temporales Unicode siguen siendo válidas y conservan su grafía original; los nombres de usuario y metadatos Unicode quedan fuera de esta restricción.
- La unicidad ya no se limita a igualdad binaria: manifests con aliases dentro del job se rechazan en K092 y las reservas entre jobs se protegen en K093.
- Una migración puede detenerse si descubre aliases históricos. El fallo conserva las filas y los archivos para que K095 o la interfaz futura pueda mostrar un diagnóstico y solicitar una resolución explícita.
- La clave `casefold()` puede causar falsos conflictos para ciertas secuencias Unicode. No debe corregirse automáticamente ni usarse para cambiar la grafía original.
- El límite de componente protege frente a nombres que exceden la cota elegida, pero no garantiza que la ruta absoluta completa quepa en todas las configuraciones de Windows o volúmenes. La operación que resuelva la ruta debe comprobar el resultado antes de escribir.

## Failure Modes

- Dos rutas difieren solo por capitalización u otra equivalencia de `casefold()`: la serialización/lectura K092 o la reserva global K093 rechaza el conjunto sin cambiar los datos.
- La migración encuentra claves derivadas repetidas: reporta las filas afectadas y aborta antes de borrar, fusionar o renombrar contenido.
- Un componente supera 255 unidades UTF-16, contiene sustituto aislado, usa nombre reservado o viola reglas de ruta: K092 rechaza el manifest; el registro fuente permanece intacto.
- El filesystem detecta un destino ya existente o una ruta absoluta demasiado larga: la operación no trunca ni reemplaza el archivo; devuelve un error trazable y conserva origen y destino.

## Reversal Strategy

Cambiar el algoritmo de identidad, el límite de componente, la lista de nombres reservados o la política de preservación requiere una nueva ADR. Cualquier cambio de clave persistida necesita una migración con preflight de colisiones, backup conforme a las reglas de persistencia, conteos, verificación de claves foráneas e `integrity_check`. Nunca resolver conflictos mediante renombrado o eliminación automática.

## Microsoft References

- Microsoft Learn, [Naming Files, Paths, and Namespaces](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file): sensibilidad a mayúsculas que no debe asumirse, Unicode permitido, caracteres/nombres reservados y dígitos superíndice para `COM#`/`LPT#`.
- Microsoft Learn, [GetVolumeInformationW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getvolumeinformationw): el límite máximo de componente se informa por volumen/sistema de archivos; sus unidades se expresan como caracteres `TCHAR`.
- Microsoft Learn, [NTFS overview](https://learn.microsoft.com/en-us/windows-server/storage/file-server/ntfs-overview) y [ReFS overview](https://learn.microsoft.com/en-us/windows-server/storage/refs/refs-overview): nombres de archivo de hasta 255 caracteres y rutas extendidas; esto no implica que una ruta absoluta de 255 caracteres sea aceptable en todas las APIs/configuraciones.
- Microsoft Learn, [Working with Strings](https://learn.microsoft.com/en-us/windows/win32/learnwin32/working-with-strings): `WCHAR` es una unidad de código UTF-16 de 16 bits; un carácter suplementario usa un par sustituto.

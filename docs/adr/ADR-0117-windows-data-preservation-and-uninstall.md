# ADR-0117 — Propiedad de raíces y conservación explícita de datos en Windows

## Estado

Propuesto — requiere aprobación de la propietaria antes de cambiar el instalador o cualquier comportamiento de conservación/eliminación.

**Alcance:** V1, SPEC §§108–111; gates K260, K262, K263, K266, K267 y K269. ADR-0084 limita K267 a una actualización básica compatible; rollback avanzado K268 queda fuera de V1.

## Contexto

La SPEC 1.1 exige instalación sin runtimes de desarrollo y que el desinstalador pregunte por conservar la biblioteca, conservar la configuración o eliminar todo. También prohíbe borrar la biblioteca automáticamente.

La Alpha actual usa dos raíces independientes: data_root puede seleccionar un perfil existente AlphaData y conserva base, progreso, cola, secretos y otros datos; library_root apunta por defecto a Documentos/Mangas y puede sobrescribirse con variables de entorno. Los CBZ antiguos pueden seguir bajo data_root/library/chapters, mientras los nuevos están bajo library_root/chapters. Los settings actuales aceptan raíces configurables, pero el instalador no registra las raíces que usó cada perfil ni implementa la elección de conservación.

El instalador requiere elevación y usa el mutex de Yomira. El Windows de verificación no tiene Windows Sandbox disponible en PATH; consultar la característica opcional exige elevación. No hay una VM desechable confirmada. No es seguro usar el Windows activo para probar instalación o desinstalación.

## Propuesta

1. **Instalar, actualizar y reinstalar:** modificar únicamente el directorio del programa y los accesos directos. Nunca borrar, mover ni migrar automáticamente data_root, library_root, AlphaData, progreso, secretos o CBZ. Si Yomira está activa, Setup no la termina ni cambia sus datos.
2. **Inventario de raíces:** tras una inicialización de producción exitosa, registrar de forma atómica en el perfil local del usuario las raíces data_root y library_root que realmente se usaron, sus funciones y sus identidades canónicas. No guardar secretos en el inventario. Cada cambio explícito de raíz agrega la ubicación validada a un historial acotado para que el desinstalador no dependa de las variables de entorno que existan en el momento de desinstalar.
3. **Elecciones del desinstalador:** mostrar dos opciones independientes, ambas activadas de manera predeterminada:
   - Conservar biblioteca: mantener los CBZ de library_root y los CBZ heredados de data_root/library/chapters.
   - Conservar configuración y estado: mantener base de datos, progreso, trabajos e historial, preferencias, secretos cifrados, metadatos, portadas, logs y backups.
   Si el usuario desactiva ambas, mostrar una segunda confirmación llamada Eliminar todos los datos de Yomira y enumerar las rutas exactas antes de continuar. El usuario puede cancelar sin modificar ninguna raíz.
4. **Desinstalación silenciosa:** conservar biblioteca y configuración siempre. Los argumentos silenciosos nunca autorizan borrado de datos ni cambian las opciones predeterminadas.
5. **Alcance del borrado:** operar solo sobre subdirectorios que Yomira haya registrado como propios en las raíces enumeradas; nunca eliminar la raíz Documentos, el perfil del usuario ni un padre arbitrario elegido mediante un override. Preservar y reportar entradas no reconocidas. La opción de borrar biblioteca requiere confirmación afirmativa explícita y nunca se activa por actualización, error, falta de espacio o fallo de limpieza.
6. **Validación fail-closed:** antes de borrar, comprobar identidad del volumen y ruta canónica, ausencia de enlaces/reparse points, solapamientos no permitidos y que la app haya liberado su mutex. Si el inventario falta, está corrupto, apunta a una ruta distinta o no se puede inspeccionar, preservar los datos y mostrarlo en el resultado; nunca inferir una raíz mirando solamente el entorno actual.
7. **Configuración conservada sin CBZ:** si el usuario elige conservar configuración y borrar la biblioteca, no reescribir la base de datos durante el desinstalado. Los assets ausentes deben seguir siendo fallos explícitos y recuperables en la app, no presentarse como archivos sanos ni crear sustitutos.
8. **Configuración eliminada con CBZ conservados:** dejar los CBZ como archivos normales en sus ubicaciones y no afirmar que se preservaron el progreso o el índice. Una instalación posterior no debe importarlos ni moverlos silenciosamente.

## Alternativas consideradas

- Borrar recursivamente data_root y library_root: rechazada; los overrides pueden señalar directorios que contienen archivos ajenos y las raíces no son necesariamente propiedad exclusiva de Yomira.
- Derivar las rutas durante la desinstalación de las variables de entorno actuales: rechazada; pueden diferir de las usadas para crear la biblioteca o el perfil.
- Borrar siempre solo el programa: no satisface la elección de eliminar datos requerida por SPEC §109.
- Ejecutar Setup o el desinstalador en el Windows activo para conseguir una prueba rápida: rechazado; no aísla registro, mutex, accesos directos ni estado de usuario.

## Consecuencias

- K263 requiere el inventario durable de raíces, la UX explícita y un plan de borrado que enumere archivos/subdirectorios antes de ejecutarse.
- Las dos opciones de conservación permiten mantener únicamente la biblioteca o únicamente la configuración cuando el usuario lo solicita. El contenido de la otra categoría se elimina solo tras la elección explícita y confirmación.
- Un inventario inválido o una raíz custom no inspeccionable se conserva. El desinstalador informa qué datos no pudo verificar o retirar.
- K267 no adquiere requisitos de rollback avanzado: se verifica la actualización compatible y supervivencia de data_root/library_root conforme a ADR-0084. K268 permanece diferido.

## Verificación requerida

- Tests de plan de borrado sin mutaciones para las cuatro combinaciones de opciones, cancelación y modo silencioso.
- Fixtures con raíces predeterminadas, AlphaData legado, Documentos/Mangas, overrides, raíz inexistente, inventario ausente/corrupto, rutas solapadas, reparse points y archivos centinela fuera de las rutas administradas.
- Test primero de cada fallo de identidad, permisos o mutex; no debe cambiar ningún dato si la validación falla.
- Smoke real de instalación, actualización compatible, desinstalación y Windows limpio en VM/Sandbox desechable, con los centinelas preservados.
- El PASS de build K260 no se propaga a los gates de ejecución o release.

## Preguntas para aprobación

- ¿Apruebas que las dos opciones de conservar biblioteca y conservar configuración/estado sean independientes, con ambas activadas por defecto y una segunda confirmación para eliminar todos los datos?
- ¿Apruebas que las raíces ausentes, no registradas, solapadas o no verificables se conserven, aunque eso haga que el desinstalador informe que no pudo completar toda la eliminación?
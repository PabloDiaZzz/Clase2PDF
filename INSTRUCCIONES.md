# Guía de Uso — Clase2PDF

Convierte grabaciones de clase (audio o vídeo) y documentos de apoyo en **apuntes limpios en PDF** o **transcripciones completas** usando Gemini, con limpieza automática de audio y actualización automática desde GitHub.

> [!NOTE]
> Este archivo (`INSTRUCCIONES.md`) es **ignorado automáticamente por el script**: puedes dejarlo junto a `procesar_clase.bat` o dentro de cualquier carpeta de clase sin que interfiera con el procesamiento.

---

## Índice

1. [Requisitos y primera ejecución](#requisitos-y-primera-ejecución)
2. [Cómo usarlo (paso a paso)](#cómo-usarlo-paso-a-paso)
3. [Flujo completo del programa](#flujo-completo-del-programa)
4. [Modos de uso y comandos (CLI)](#modos-de-uso-y-comandos-cli)
5. [Qué archivos lee, ignora y genera](#qué-archivos-lee-ignora-y-genera)
6. [Gestión de varias API Keys](#gestión-de-varias-api-keys)
7. [Auto-actualización y modo offline](#auto-actualización-y-modo-offline)
8. [Errores frecuentes y cómo solucionarlos](#errores-frecuentes-y-cómo-solucionarlos)
9. [Códigos de salida](#códigos-de-salida)

---

## Requisitos y primera ejecución

- **Sistema operativo:** Windows 10 u 11.
- **Navegador:** Microsoft Edge (incluido en Windows), Google Chrome o Brave para maquetado avanzado (si no hay ninguno o tarda demasiado, usa `fpdf2` como respaldo automático).
- **Conexión a Internet:** necesaria en la primera ejecución para descargar `pipeline.py`, dependencias y validar tu API Key.
- **API Key de Google Gemini:** gratuita desde [Google AI Studio](https://aistudio.google.com/apikey) (empieza por `AQ.`).

> [!TIP]
> La primera vez que abras `procesar_clase.bat`, si te faltan `ffmpeg` o `Python 3.12+`, el propio script intentará instalarlos automáticamente con `winget` y preparará su entorno aislado en `%LOCALAPPDATA%\Clase2PDF`.

---

## Cómo usarlo (paso a paso)

### Opción A — Sin escribir comandos (recomendada)

1. Mete en una carpeta tus **audios/vídeos** de clase (y opcionalmente diapositivas o PDFs de apoyo).
2. **Doble clic** en `procesar_clase.bat` dentro de esa carpeta **o arrastra la carpeta** encima del icono de `procesar_clase.bat`.
3. Si es la primera vez, pega tu API Key (`AQ....`) cuando te la pida. Se guarda cifrada con DPAPI de Windows.
4. Elige el modo cuando te pregunte:
   - **`apuntes`**: genera apuntes estructurados listos para estudiar.
   - **`transcripcion`**: genera la transcripción completa y fiel de la clase.
5. Al terminar, verás el PDF generado (`APUNTES_*.pdf` o `TRANSCRIPCION_*.pdf`) en la misma carpeta.

### Opción B — Desde terminal (PowerShell o CMD)

```powershell
# Procesar la carpeta actual (pregunta el modo)
.\procesar_clase.bat

# Procesar una carpeta concreta directamente en modo apuntes
.\procesar_clase.bat "C:\Users\TuUsuario\Desktop\Clase_Tema_3" --mode apuntes

# Generar transcripción y conservar también el archivo Markdown (.md)
.\procesar_clase.bat --mode transcripcion --keep-md
```

---

## Flujo completo del programa

```text
procesar_clase.bat
  ├── [0] Auto-actualizacion (manifest.json -> pipeline.py, INSTRUCCIONES.md, .bat)
  └── [0b] Entorno (winget ffmpeg/Python 3.12 si faltan + venv en %LOCALAPPDATA%\Clase2PDF)
         │
         ▼
pipeline.py
  ├── [1] Escaneo de carpeta (audios, docs .pdf/.docx, apuntes base) + modo + API Key
  ├── [2] Vocabulario tecnico (solo si hay docs): extrae terminos y siglas con Flash
  ├── [3] Audio (ffmpeg): limpia ruido/voz y divide en bloques de 45 min (+15s solape)
  ├── [4] Transcripcion: sube cada bloque a Gemini, transcribe con vocabulario y borra remoto
  ├── [5] Triage de PDFs (si hay varios docs o >25 pags): recorta paginas o divide por temas
  ├── [6] Sintesis: redacta apuntes estructurados o transcripcion revisada
  └── [7] Exportacion y limpieza: HTML -> PDF con Edge/Chrome + borrado de .clase_pipeline\
```

Detalle de cada fase:

1. **Arranque y auto-actualización (`procesar_clase.bat`):**
   - Consulta `manifest.json` en GitHub y comprueba por SHA-256 si hay versión nueva de `pipeline.py` (en `%LOCALAPPDATA%\Clase2PDF`), `INSTRUCCIONES.md` (en la carpeta del `.bat`) o del propio `.bat`. Si no hay internet y ya existe copia local de `pipeline.py`, continúa en modo caché.
   - Comprueba `ffmpeg` y `Python 3.12+` (si faltan, intenta instalarlos con `winget`) y prepara el entorno virtual `venv` con `google-genai`, `rich`, `questionary` y `pypdf`.
2. **Escaneo e inicialización:**
   - Detecta audios/vídeos compatibles (orden natural), documentos de apoyo (`.pdf`, `.docx`) y posibles apuntes previos (`APUNTES_*.pdf`, `TRANSCRIPCION_*.pdf`).
   - Pregunta el modo (`apuntes` o `transcripcion`) si no se pasó `--mode`, y pide la API Key si aún no hay ninguna guardada.
3. **Extracción de vocabulario técnico** *(solo si hay documentos en la carpeta)*:
   - Analiza los `.pdf` y `.docx` con modelos Flash para extraer términos técnicos, siglas y nombres propios, mejorando la precisión de la transcripción posterior.
4. **Limpieza y división de audio (`ffmpeg`):**
   - Aplica filtros de limpieza de ruido de aula, ecualización de voz y normalización de volumen.
   - Si un audio es largo, lo divide en bloques de 45 minutos (con 15 segundos de solapamiento) dentro de `.clase_pipeline/`. Guarda huella de cada audio para no repetir la limpieza si reintentas tras un fallo.
5. **Transcripción por bloques (concurrente con varias APIs):**
   - Sube cada bloque limpio a Gemini, lo transcribe apoyándose en el vocabulario técnico y elimina el archivo remoto al terminar.
   - Si tienes 2 o más APIs de proyectos distintos, procesa bloques en paralelo (hasta 3 a la vez) asignando cada bloque a una API diferente.
   - Antes de reutilizar caché previa, inspecciona los `.txt` antiguos: si detecta alucinaciones, bucles de repetición (incluidos caracteres sin espacios) o archivos vacíos, los **borra automáticamente** y los re-transcribe de cero.
   - Si una API agota cuota (`429`), rota automáticamente a la siguiente clave guardada; si el servidor devuelve saturación (`503`), cambia al siguiente modelo de la cadena (`gemini-3.1-flash-lite` y `gemma-4` como respaldo extra).
6. **Filtrado y división de documentos o clases largas (Triage):**
   - Si hay varios documentos o algún PDF supera las 25 páginas, recorta literalmente solo las páginas relevantes con `pypdf` sin resumir.
   - Si hay un único PDF grande (`> 25 páginas`) como base (o como guía en modo `apuntes`), planifica y divide el trabajo por temas para generar varios PDFs temáticos en lugar de un único PDF gigante.
   - Si la transcripción total de la clase es muy extensa (`> 18.000 palabras`), la divide automáticamente en varios PDFs por temas coherentes sin resumir ni omitir ningún bloque (evitando también superar el límite de tokens por minuto).
7. **Síntesis, exportación a PDF y limpieza:**
   - Redacta el documento final en Markdown según el modo elegido (`apuntes` o `transcripcion`).
   - Convierte el Markdown a HTML y lo renderiza a `APUNTES_<tema>.pdf` o `TRANSCRIPCION_<tema>.pdf` usando Edge/Chrome en modo headless (conservando el `.md` solo si pasaste `--keep-md`).
   - Elimina la carpeta temporal `.clase_pipeline\` al finalizar.

---

## Modos de uso y comandos (CLI)

| Opción / Flag | Qué hace |
| :--- | :--- |
| `carpeta` | Ruta a la carpeta con audios (por defecto, la carpeta actual). |
| `--mode apuntes` | Genera apuntes estructurados directamente sin preguntar. |
| `--mode transcripcion` | Genera transcripción literal directamente sin preguntar. |
| `--keep-md` | Guarda también el archivo `.md` además del `.pdf`. |
| `--apis` | Abre el menú interactivo para ver, añadir, probar o borrar API Keys. |
| `--add-api` | Añade y verifica una nueva API Key (`API 1`, `API 2`, ...). |
| `--reset-key` | Borra todas las API Keys guardadas antes de ejecutar. |
| `--dry-run` | Muestra qué audios/documentos detecta y qué haría, sin gastar API. |
| `--debug` | Simula todas las fases sin llamar a Gemini ni escribir archivos. |
| `--json` | Devuelve el resultado estructurado en JSON por `stdout`. |
| `-q`, `--quiet` | Oculta paneles y barras de progreso. |
| `--no-clear` | No limpia la pantalla de la terminal al arrancar. |
| `--version` | Muestra la versión actual de `Clase2PDF`. |

### Ejemplos útiles

```powershell
# Comprobar qué archivos detecta en una carpeta sin gastar cuota
.\procesar_clase.bat "D:\Clases\Fisica" --dry-run

# Añadir una segunda API Key de respaldo
.\procesar_clase.bat --add-api

# Abrir el menú completo de gestión de APIs
.\procesar_clase.bat --apis

# Ejecutar en modo silencioso con salida JSON (útil para automatizaciones)
.\procesar_clase.bat "D:\Clases\Historia" --mode apuntes -q --json
```

---

## Qué archivos lee, ignora y genera

### Archivos que **SÍ** procesa

- **Audios y vídeos:** `.mp3`, `.mp4`, `.m4a`, `.aac`, `.mka`, `.wav`, `.ogg`, `.opus`, `.flac`, `.webm`
  - *Nota:* Se ordenan de forma natural (`clase1`, `clase2`, ..., `clase10`). Los audios largos se dividen automáticamente en bloques de 45 minutos (con 15 segundos de solapamiento) para evitar límites de tokens y pérdidas de conexión.
- **Documentos de contexto:** `.pdf`, `.docx`
  - Si hay un PDF muy grande (`> 25 páginas`) o varios documentos, el script selecciona y recorta solo las páginas relevantes por tema usando `pypdf`.
  - Si ya existe un `APUNTES_*.pdf` o `TRANSCRIPCION_*.pdf` previo en la carpeta, lo detecta como base para continuar/ampliar o dividir por temas si es muy extenso.

### Archivos que **IGNORA**

- Cualquier archivo `.md` (incluido `INSTRUCCIONES.md` o `README.md`), `.txt`, imágenes, `.bat`, `.py`, etc.
- Carpetas internas del sistema o temporales.

### Dónde se guarda cada cosa

| Ubicación | Qué contiene |
| :--- | :--- |
| **Carpeta de la clase** | El resultado final (`APUNTES_<nombre>.pdf` o `TRANSCRIPCION_<nombre>.pdf`, y `.md` si usaste `--keep-md`). |
| **`.clase_pipeline\`** *(dentro de la carpeta de clase)* | Temporales y caché de reanudación de esa ejecución (`clean/`, `chunks/`, `transcripts/`, `slices/`, `topics/`, `doc.html`). **Se borra automáticamente** al terminar. |
| **`%LOCALAPPDATA%\Clase2PDF\`** | `pipeline.py`, entorno `venv\`, API Keys cifradas (`api_key*.dpapi`) y registro de errores (`last_error.log`). |

---

## Gestión de varias API Keys

Si sueles procesar clases largas o varias seguidas, conviene añadir 2 o más API Keys gratuitas (de distintas cuentas o proyectos de Google AI Studio):

```powershell
.\procesar_clase.bat --apis
```

- **Gestión individual:** desde `--apis` puedes añadir, probar, eliminar una clave concreta o borrar todas las guardadas.
- **Ejecución en paralelo:** si añades 2 o más API Keys de **proyectos distintos**, `Clase2PDF` transcribe bloques y sintetiza temas en paralelo (hasta 3 a la vez), reduciendo el tiempo total.
- **Rotación automática (`429`):** si una clave agota su cuota, `Clase2PDF` pasa inmediatamente a la siguiente clave manteniendo el mejor modelo.
- **Fallback de modelo (`503`):** si los servidores de un modelo están saturados, baja al siguiente modelo disponible al instante.
- **Seguridad:** las claves se cifran localmente con Windows DPAPI (`api_key.dpapi`, `api_key_2.dpapi`, etc.) y **nunca** se muestran completas ni se suben a ninguna carpeta de clase.

---

## Auto-actualización y modo offline

Cada vez que ejecutas `procesar_clase.bat`:

1. Consulta `manifest.json` en GitHub y verifica por **SHA-256** si hay versión nueva de `pipeline.py`, de dependencias, de `INSTRUCCIONES.md` o del propio `.bat`.
2. Si hay cambios, descarga y verifica el archivo antes de ejecutarlo (no necesitas volver a pedir el `.bat` a nadie).
3. Si **no tienes internet**, usa la copia en caché de `%LOCALAPPDATA%\Clase2PDF\pipeline.py` (aunque para transcribir con Gemini sí necesitarás conexión).

### Variables de entorno opcionales

```cmd
:: Saltar la comprobación de actualizaciones en GitHub
set CLASE2PDF_NO_UPDATE=1
procesar_clase.bat
```

---

## Errores frecuentes y cómo solucionarlos

### 1. Errores de arranque y actualización (`.bat`)

| Error | Causa | Solución |
| :--- | :--- | :--- |
| `UPDATE_OFFLINE` | No hay internet y es la primera ejecución (aún no existe `pipeline.py` local). | Conéctate a internet y vuelve a ejecutar `procesar_clase.bat` una vez para descargar el núcleo. |
| `MISSING_WINGET` | Falta `winget` en Windows para instalar `ffmpeg` o Python. | Instala **Instalador de aplicación (App Installer)** desde Microsoft Store, o instala `Python 3.12+` y `ffmpeg` manualmente y reintenta. |
| `ENV_SETUP` | Fallo creando el `venv` o instalando paquetes (`google-genai`, `rich`, `questionary`, `pypdf`). | Comprueba tu conexión, desactiva temporalmente VPN/proxy corporativo o borra `%LOCALAPPDATA%\Clase2PDF\venv` y vuelve a abrir el `.bat`. |
| `MISSING_PIPELINE` | Ejecutaste con `CLASE2PDF_NO_UPDATE=1` sin tener `pipeline.py` descargado. | Ejecuta sin `CLASE2PDF_NO_UPDATE=1` teniendo conexión a internet. |
| `No se pudo detectar ffmpeg/Python tras instalar` | `winget` instaló el programa pero el `PATH` de la sesión actual aún no lo ve. | Cierra la ventana negra y vuelve a abrir `procesar_clase.bat`. |

### 2. Errores de API Key y cuota

| Error | Causa | Solución |
| :--- | :--- | :--- |
| `MISSING_API_KEY` / `EMPTY_API_KEY` | No hay ninguna API Key guardada o se introdujo vacía. | Ejecuta `procesar_clase.bat --add-api` y pega una clave válida de AI Studio. |
| `INVALID_API_KEY_FORMAT` | La clave pegada no tiene el formato correcto (demasiado corta, con espacios o caracteres inválidos). | Copia la clave completa (empieza por `AQ.`) desde [Google AI Studio](https://aistudio.google.com/apikey) sin espacios. |
| `API_KEY_TEST_FAILED` / `AUTH_FAILED` | Clave revocada, inválida, sin permisos o bloqueada (`401`/`403`). | Revisa la clave en `--apis`, bórrala con `--reset-key` o genera una nueva en AI Studio. |
| `API_UNAVAILABLE` *(código 75)* | Todas tus APIs están sin cuota (`429`) o los servidores de Gemini están caídos/saturados (`503`). | Espera unos minutos o añade otra API Key de otra cuenta con `procesar_clase.bat --add-api`. |
| `RemoteProtocolError: Server disconnected` | Corte puntual de red o timeout del servidor al subir/procesar un audio largo. | Vuelve a ejecutar `procesar_clase.bat`: reintentará con backoff y aprovechará la caché de audio ya limpiado. |

### 3. Errores de archivos, navegador y sistema

| Error | Causa | Solución |
| :--- | :--- | :--- |
| `INVALID_FOLDER` | La ruta pasada no existe o no es una carpeta válida. | Revisa la ruta entre comillas, por ejemplo: `procesar_clase.bat "C:\ruta\clase"`. |
| `NO_AUDIOS` | No hay archivos de audio/vídeo compatibles en la carpeta. | Asegúrate de ejecutar el `.bat` dentro de la carpeta con los audios (`.mp3`, `.m4a`, `.wav`, `.mp4`, etc.) o arrastrar la carpeta encima. |
| `BROWSER_NOT_FOUND` | No se encontró Microsoft Edge ni Google Chrome y también falló el respaldo `fpdf2`. | Instala Microsoft Edge o Google Chrome, o borra `%LOCALAPPDATA%\Clase2PDF\venv` para reinstalar `fpdf2`; como alternativa usa `--keep-md`. |
| `UNEXPECTED_ERROR` *(código 1)* | Error interno no previsto. | Revisa el detalle técnico en `%LOCALAPPDATA%\Clase2PDF\last_error.log`. |

> [!TIP]
> Si un proceso falla a mitad, **no pierdes el audio ya limpiado**: `.clase_pipeline` guarda una huella del audio procesado para no repetir la limpieza con `ffmpeg` en el siguiente intento.

---

## Códigos de salida

| Código | Significado |
| :---: | :--- |
| `0` | Completado correctamente (`EXIT_OK`). |
| `1` | Error general o inesperado (`EXIT_ERROR`). |
| `2` | Argumentos de CLI incorrectos (`EXIT_USAGE`). |
| `3` | Carpeta o audios no encontrados (`EXIT_NOT_FOUND`). |
| `4` | Error de autenticación o API Key inválida (`EXIT_AUTH`). |
| `75` | Error temporal: sin cuota en todas las APIs o servidor saturado (`EXIT_TRANSIENT`). |
| `78` | Error de configuración o falta Edge/Chrome (`EXIT_CONFIG`). |
| `130` | Cancelado por el usuario (`Ctrl+C`) (`EXIT_CANCELLED`). |

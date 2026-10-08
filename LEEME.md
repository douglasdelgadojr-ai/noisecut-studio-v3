# NoiseCut Studio
1. Sube esta carpeta a un repositorio de GitHub y abre **Actions**.
2. Ejecuta **Construir NoiseCut Studio para Windows** con **Run workflow**, o sube un cambio.
3. Cuando termine, descarga **NoiseCutStudio-Windows** en Artifacts y descomprime el ZIP.
4. Abre `NoiseCutStudio.exe` para iniciar el editor.
5. Pulsa **Importar**, elige videos, audios o imágenes y añádelos a la línea de tiempo.
6. Usa **Limpiar voz** para suavizar el ruido y mejorar el audio; puedes elegir todos o el clip seleccionado.
7. **Reproducir todo** prepara una vista previa continua del proyecto; **Ajustar a ventana** encuadra la línea de tiempo.
8. En Audio puedes cambiar volumen, aplicar filtros o grabar voz en off; exporta desde el botón de exportación.
9. La pestaña **IA** ofrece subtítulos Whisper, quitar fondo y limpieza RNNoise; son funciones opcionales.
10. Para Whisper y quitar fondo, instala `requirements-ai.txt`; el modelo se descarga al usar la función y no va en el instalador.
11. Los modelos de IA no se incluyen en el instalador; Whisper puede ocupar de unos 75 a 460 MB según el modelo.
12. El ZIP de Windows incluye FFmpeg GPL de BtbN. Si distribuyes la aplicación, debes cumplir esa licencia y conservar sus avisos.
13. Los proyectos guardan rutas a tus medios; conserva los archivos originales para abrirlos de nuevo.

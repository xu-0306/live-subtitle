# README media

## Demo clip (`demo.gif`, `demo.png`)

Recorded automatically with the real extension, backend and local translation model.

- Video: [“Improve Your Speaking”](https://www.youtube.com/watch?v=c8_BIamLESg) by Conor Neill, licensed CC BY on YouTube. The README credits it under the clip; keep that line if the clip is replaced with another part of the same video, and change it if a different video is used. Only use videos whose YouTube page says "Creative Commons Attribution license (reuse allowed)", or your own footage.
- Translation: local `qwen35-4b-q4` to Traditional Chinese, speech model from the desktop default.

### Re-recording

1. Start the backend from the desktop app (speech and local translation models already downloaded).
2. Run, from the repository root:

   ```bash
   node docs/media/record_demo.js c8_BIamLESg 15 40
   ```

   Set `PLAYWRIGHT_MODULE` if Playwright is not installed in this repo, and `CHROMIUM` to a Chromium build (branded Chrome ignores `--load-extension`). The script explains its two test-only shortcuts at the top. It saves a `page@*.webm` in this folder; do not commit it.
3. Pick about 13 seconds where subtitles and translation are both on screen, then convert (the crop removes the black bars around the 4:3 video):

   ```bash
   ffmpeg -ss 26 -t 13 -i page@XXXX.webm -vf "crop=960:720:160:0,fps=8,scale=760:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" docs/media/demo.gif
   ffmpeg -ss 33 -i page@XXXX.webm -frames:v 1 -vf "crop=960:720:160:0,scale=760:-1:flags=lanczos" docs/media/demo.png
   ```

   Keep the GIF under about 8 MB; lower `fps` or `max_colors` if it grows.

Screen grabbing with `ffmpeg -f gdigrab` does not work while the machine is used over Remote Desktop (blank frames or "access denied"), which is why the script records through the browser instead.

## Desktop app screenshots (`gui-*.png`)

Rendered by `render_gui.py` with a throwaway config, so no personal settings appear:

```bash
python -X utf8 docs/media/render_gui.py --pages local,services
```

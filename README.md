# TubeLube

A small desktop app for changing the speed (and optionally the pitch) of audio and video, sourced either from YouTube or from your own files.

It runs as a local Flask server that auto-opens in your browser, giving you a modern UI without the weight of Electron.

## Features

- Paste a **YouTube URL** or drop in a local **MP3/MP4** (also accepts m4a, wav, webm, mkv, mov for input).
- Choose **MP3** (192/320 kbps) or **MP4** (720p/1080p/best) for the output.
- Speed range **0.25x – 4.0x** via slider + numeric input.
- Toggle **preserve pitch** (clean tempo change) vs. the **slowed/sped aesthetic** (pitch shifts with speed).
- Output goes to a folder you pick, with auto-generated names like `mytrack_0.75x.mp3`.

## Setup

### Requirements

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/) on your PATH
- Python deps:

```bash
pip install -r requirements.txt
```

### Installing ffmpeg

- **Windows:** `winget install Gyan.FFmpeg` (or download from gyan.dev and add to PATH)
- **macOS:** `brew install ffmpeg`
- **Linux:** `sudo apt install ffmpeg`

The app checks for ffmpeg/yt-dlp on launch and shows install instructions if anything is missing.

## Run

```bash
python app.py
```

The app starts on `http://127.0.0.1:5001` and opens your default browser automatically.

## Run as a desktop app

Same app, in its own native window instead of a browser tab (uses pywebview + the
Windows WebView2 runtime, which ships with Windows 10/11):

```bash
python desktop.py
```

On Windows, double-click **`TubeLube (App).lnk`** (drag it to your desktop/Start menu).
The web version (`python app.py` / `TubeLube.lnk`) keeps working independently — if a
web instance is already running, the desktop window just attaches to it.

### Build a standalone .exe

To produce a single double-clickable `TubeLube.exe` (no Python needed on the target
machine — ffmpeg / yt-dlp / Deno are still required separately):

```bash
build_exe.bat
```

or manually:

```bash
pip install --user pyinstaller
pyinstaller TubeLube.spec
```

The result is `dist\TubeLube.exe`. First launch is a little slow (it unpacks to a temp
folder). Some antivirus tools flag fresh PyInstaller exes as a false positive — that's
expected for unsigned one-file builds. To give it an icon, drop a `TubeLube.ico` in the
project root before building.

## How the speed transform works

ffmpeg's `atempo` filter only accepts factors between 0.5 and 2.0. For anything outside that range TubeLube chains multiple `atempo` filters whose product equals the target — e.g. `0.25x` becomes `atempo=0.5,atempo=0.5`, and `4.0x` becomes `atempo=2.0,atempo=2.0`.

For video, the audio chain is paired with `setpts={1/speed}*PTS` so the audio and video stay aligned.

When "preserve pitch" is **off**, the audio is processed with `asetrate={sr}*{speed},aresample={sr}` instead — this is the classic "slowed and reverb" / chipmunk effect, where slowing the audio also drops the pitch.

## Project layout

```
TubeLube/
├── app.py                     # Flask routes, dep checks, browser launch
├── desktop.py                 # native-window launcher (pywebview)
├── downloader.py              # yt-dlp wrapper
├── converter.py               # ffmpeg speed/pitch logic
├── playset.py                 # JSON-backed playlists
├── TubeLube.spec              # PyInstaller build config (→ TubeLube.exe)
├── build_exe.bat              # one-click .exe build
├── templates/
│   ├── index.html             # main UI
│   └── install_prompt.html    # shown when ffmpeg/yt-dlp missing
├── requirements.txt
└── README.md
```

## Out of scope for v1

- Batch / playlist processing
- Trimming
- Reverb and other effects
- Format conversion beyond MP3/MP4

These are tagged `# TODO v2` in the code where they'd naturally extend.

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

### Fresh setup on a new machine (e.g. at work)

After cloning the repo:

1. **Python deps:** `pip install -r requirements.txt`
2. **ffmpeg** on PATH (see above).
3. **Deno** — YouTube obfuscates download URLs with a JS challenge that yt-dlp
   solves via a JavaScript runtime. Install Deno so downloads work:
   - Windows: `winget install DenoLand.Deno` (then open a fresh terminal)
   - macOS/Linux: `curl -fsSL https://deno.land/install.sh | sh`
4. **`cookies.txt`** — YouTube increasingly requires a signed-in session
   ("Sign in to confirm you're not a bot"). This file is **not** in the repo
   (it holds live session tokens). To create it: install the *Get cookies.txt
   LOCALLY* browser extension, sign in at youtube.com, export, and save the
   file as `cookies.txt` in the project root. TubeLube auto-detects it.
   You only need this if you hit the bot wall.

> Note: your saved playlists (`playset.json`) are also local-only, so a fresh
> clone starts with an empty library.

## Run (in your browser)

```bash
python app.py
```

The app starts on `http://127.0.0.1:5001` and opens your default browser
automatically — no desktop app needed. On Windows you can just **double-click
`run_web.bat`** instead of typing the command. Keep the terminal/window open
while you use it; close it to stop the app.

## Use it from your phone

TubeLube can serve its UI to your phone over your home Wi-Fi — the PC still does
the downloading/converting, your phone is just the remote control:

```bash
python app.py --lan
```

(or double-click **`run_phone.bat`** on Windows). The terminal prints the address
to open — `http://<your-pc-ip>:5001` — plus a QR code you can scan with your
phone's camera. Requirements and caveats:

- Phone and PC must be on the **same network**, and the PC must stay on.
- **Windows firewall:** the first LAN run pops a prompt — allow Python on
  *private* networks, or the phone won't connect.
- Converted files are saved on the PC as usual; on the phone you get a
  **Download** button (and the folder icon in the top bar re-downloads the last
  file) instead of "Open folder".
- The folder *Browse* picker is desktop-only (it opens a dialog on the PC), so
  it's hidden on remote devices — the save-to path still shows where the PC
  keeps the files.
- Anyone on your Wi-Fi can reach the app while it runs in LAN mode, so use it
  on networks you trust (there's no login). Plain `python app.py` stays
  localhost-only, exactly as before.

If you want access *away* from home too, run something like
[Tailscale](https://tailscale.com/) on the PC and phone and open the PC's
Tailscale IP the same way — no TubeLube changes needed.

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

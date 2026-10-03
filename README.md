<div align="center">

# 🎬 Video Mixer

**Batch-mix system + microphone audio in screen recordings and re-encode to AV1 / H.264 on CPU or GPU.**

![Version](https://img.shields.io/badge/version-2.1.0-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6)
![FFmpeg](https://img.shields.io/badge/requires-FFmpeg-007808?logo=ffmpeg&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

[**⬇ Download the latest release**](../../releases/latest)

</div>

A terminal app for batch-processing screen recordings with **FFmpeg**:

- mixes **audio track 1** (system sounds) and **audio track 2** (microphone) into a new **"Mix"** track
- keeps the original tracks, with clear names
- re-encodes the video to **AV1** or **H.264** on the **CPU** or the **GPU** (NVIDIA, Intel or AMD)

All of it runs from one file: `VideoMixer.exe`, or `video_mixer.py`.

---

## ✨ Features

| Feature | Description |
|---|---|
| **Audio mixing** | Tracks 1 + 2 → a new "Mix" track, set as the default track so players use it automatically |
| **Track control** | Scans your videos and lists every track number found. For each track, choose **In Mix** and **Keep**, each with its own volume, so you can drop a track completely (e.g. track 3) or make it quieter. The microphone is 75% in the Mix by default |
| **Add a track** *(v2.1)* | Adds an audio file with the same name as the video (`recording.mp4` + `recording.wav`) as an extra track, e.g. a separately recorded mic |
| **Track naming** | Output tracks: `Mix` · `System sounds` · `Microphone` (you can rename them) |
| **Two codecs** | AV1 (smaller files) or H.264 (plays everywhere) |
| **GPU auto-detection** | Test-encodes on startup to find which encoders really work on this PC |
| **4 backends** | CPU, NVIDIA NVENC, Intel Quick Sync, AMD AMF |
| **Animated progress** | Gradient bars with a shimmer effect, showing %, frames, fps, speed, elapsed time and ETA |
| **Batch processing** | Processes every `.mp4` in the folder and skips files it has already processed |
| **Saved settings** | Every change is stored in `settings.json` |
| **Safe cancel** | `Ctrl+C` stops the current file and deletes the half-finished output |
| **Summary** | Processed / skipped / failed counts, total time, and file size before → after |
| **Portable** | A single `.exe`, with no Python needed |
| **FFmpeg check** | If FFmpeg is missing, shows step-by-step install and PATH instructions |

---

## 📦 Requirements

- **Windows 10/11 (64-bit)** only, for now. Both `VideoMixer.exe` and `video_mixer.py` are tested on Windows only; macOS and Linux are not supported yet.
- **FFmpeg** (`ffmpeg` + `ffprobe`), found in either of these places:
  - next to `VideoMixer.exe` (or in an `ffmpeg\bin\` or `bin\` subfolder), **or**
  - on the system PATH

### Installing FFmpeg on Windows

**Easiest:**
```powershell
winget install Gyan.FFmpeg
```

**Manual:**
1. Download `ffmpeg-release-essentials.zip` from https://www.gyan.dev/ffmpeg/builds/
2. Extract it to `C:\ffmpeg`, so that `C:\ffmpeg\bin\ffmpeg.exe` exists.
3. Add `C:\ffmpeg\bin` to PATH: press `Win + R` → `sysdm.cpl` → **Advanced** → **Environment Variables** → **Path** → **Edit** → **New**.
4. Open a **new** terminal and check with `ffmpeg -version`.

**No install:** copy `ffmpeg.exe` and `ffprobe.exe` next to `VideoMixer.exe`.

---

## 🚀 Quick start

> **Download:** grab the `.zip` file from the [Releases page](../../releases/latest), extract it on any Windows PC and run `VideoMixer.exe`. If that PC doesn't have FFmpeg, the app shows how to install it (see above).

1. Put `VideoMixer.exe` somewhere, and make sure FFmpeg is installed.
2. Open a terminal **in the folder that contains your videos** and run the app:
   ```powershell
   & "C:\Tools\VideoMixer\VideoMixer.exe"
   ```
   Or set **Settings → Input folder** once, and after that you can simply double-click the exe.
3. Choose **▶ Start processing**, check the file list, and confirm.
4. Output files are saved to `Processed - <backend>\<name>-Mix.mp4`.

Use the **↑ ↓ arrow keys** and **Enter** in every menu, and **Backspace** to go back (in a list of options, Backspace cancels without changing anything). On the main menu, Backspace does nothing, so you can't exit by accident.

---

## ⚙️ How it works

```
 your-video.mp4                              Processed - NVIDIA\your-video-Mix.mp4
 ├── video                 ── re-encode ──▶  ├── video  (AV1 / H.264, 60 fps)
 ├── audio 1 (system) ──┬──── amix ──────▶   ├── audio: "Mix"            ← default
 └── audio 2 (mic) ─────┘                    ├── audio: "System sounds"  (copy of track 1)
                                             └── audio: "Microphone"     (copy of track 2)
```

- Which tracks go into the Mix and which are kept is set **per track number** in *Audio tracks* (below). By default every track is mixed and kept; track 3 and up are named "Track 3", "Track 4", …
- With **Add a track** on, an audio file with the same name next to a video becomes one more track (`ext` in the file list).
- A file with **one** audio track keeps that track and names it "Audio" (or is skipped — see *Audio tracks* below).
- A file with **no** audio gets its video re-encoded only (or is skipped).
- Before you confirm, the file list shows an **Audio** column with what will happen to each file.
- All audio is encoded as AAC (192k by default).
- Files whose names already end in the suffix (`-Mix`) are **skipped**, so running the app again only processes new files.

### Startup
1. Looks for `ffmpeg` and `ffprobe`. If either is missing, shows a warning and exits.
2. Test-encodes a few frames with each encoder to see which ones work. For example:
   - an RTX 30-series card or older → H.264 NVENC ✓, AV1 NVENC ✗
   - an RTX 40-series card or newer → both ✓
3. Loads `settings.json`. If the saved encoder doesn't work on this PC, it switches to one that does and tells you.

---

## 🧭 Menus

### Main menu
| Item | What it does |
|---|---|
| ▶ Start processing | Lists the files, asks for confirmation, then encodes them all |
| ⚙ Settings | Change every option (see below) |
| ◎ Detected encoders | Table of every encoder with ✓ / ✗ |
| ✕ Exit | Close the app |

The header always shows your current settings and which backends are available.

### Settings

| Setting | Default | Notes |
|---|---|---|
| Codec | AV1 | AV1 or H.264 |
| Encoder | SVT-AV1 (CPU) | Only encoders that work on this PC can be selected |
| Quality | 23 | Lower = better quality and a bigger file (see the table below) |
| Preset | 6 | Speed vs. quality trade-off; the options depend on the encoder |
| 10-bit color | on | AV1 only; reduces color banding |
| Output frame rate | 60 | 24 / 25 / 30 / 50 / 60 / 120, custom, or **keep source** |
| Audio bitrate | 192k | 96k – 320k |
| Audio tracks | Mix 1+2 · keep 1,2 | Opens the track settings page (see below) |
| Input folder | *(current folder)* | Where your videos are |
| Output folder | `Processed - {backend}` | Relative paths are inside the input folder. `{backend}` and `{codec}` are replaced automatically |
| Output suffix | `-Mix` | Added to output names; files ending with it are skipped |
| Reset to defaults | — | Restores all of the above |

### Audio tracks

When you open this page, the app **scans the input folder** and shows how many audio tracks your videos have:

```
 Found in input folder: 5 videos → 2 have 3 tracks · 2 have 2 tracks · 1 has 1 track

   Create "Mix" track               on
     Track      Name                  In Mix    Keep
 ❯ Track 1    System sounds         ✓ 100%    ✓ 100%    in 5/5 videos
   Track 2    Microphone            ✓ 75%     ✓ 100%    in 4/5 videos
   Track 3    Discord               ✗         ✗         in 2/5 videos
   + Add a track…  (audio file with the same name)
```

Select a track to change it:

| Option | Meaning |
|---|---|
| **In Mix** | The track is mixed into the "Mix" track |
| **Keep** | The track is also copied to the output as its own track |
| **Name** | The track's name in the output |
| **Volume in Mix** | How loud the track is **inside the Mix** |
| **Volume (own track)** | How loud the track's own kept track is (also used for 1-track files) |

Volumes are in percent: `100` = unchanged, `50` = half as loud, `200` = twice as loud (0–400). Very high values can distort the sound. By default the **Microphone is 75% in the Mix**, so your voice doesn't drown out the game, and every other volume is 100%. The kept tracks always start at 100%, the same as the original.

A track with **In Mix ✗** and **Keep ✗** is removed from the output. A rule only applies to videos that have that track. A 2-track video ignores the Track 3 rule.

| Setting | Default | Notes |
|---|---|---|
| Create "Mix" track | on | Off = no mixing; tracks marked **Keep** are re-encoded and named |
| Track 1 / Track 2 / … | In Mix ✓ 100% · Keep ✓ 100% | One row per track number found in your videos. Microphone (track 2) is 75% in the Mix |
| + Add a track… | off | See *Adding a track* below |
| Files with 1 audio track | keep the track | Or **skip the file** (nothing to mix) |
| Files with no audio | encode video only | Or **skip the file** |
| Mix / 1-track names | Mix / Audio | The second one is used for 1-track files |
| ↻ Rescan input folder | — | Scan again after adding videos or changing the input folder |

The first track in the output is always the default track: the Mix when it exists, otherwise the first kept track. A Mix needs at least 2 tracks marked **In Mix**. With only one, that track is kept instead.

#### Adding a track

**+ Add a track…** makes the app look for an audio file with the **same name** next to each video:

```
recording.mp4   +   recording.wav   (or .mp3 .m4a .aac .flac .ogg .opus)
```

That file becomes an extra track, shown as **External** in the list (with its own In Mix / Keep / Name / Volume), and as `ext` in the file list before processing. Videos without such a file are processed as usual, and a 1-track video with an external file is mixed like a 2-track video.

The audio file should start at the same moment as the video. If it's longer than the video, it's cut at the end of the video.

### Encoder reference

| Backend | AV1 | H.264 | Quality setting (default) | Presets |
|---|---|---|---|---|
| CPU | `libsvtav1` | `libx264` | CRF (23 / 20) | `0`–`13` / `ultrafast`…`veryslow` |
| NVIDIA | `av1_nvenc` | `h264_nvenc` | CQ (23) | `p1` (fast) … `p7` (best) |
| Intel | `av1_qsv` | `h264_qsv` | ICQ (23) | `veryfast` … `veryslow` |
| AMD | `av1_amf` | `h264_amf` | QP (100 for AV1 / 23 for H.264) | `speed` … `quality` / `high_quality` |

> **Tip:** GPU encoding is much faster. CPU encoding gives slightly better quality at the same file size.

---

## 📊 Progress screen

```
 ✓ recording-01.mp4 → recording-01-Mix.mp4   1.8 GB → 412 MB (-77%)   03:12
 ⠋ recording-02.mp4     ━━━━━━━━━━━━╸━━━━━━━━━━  48%  8640/18000 fr  142 fps  2.37x  0:01:01 ETA 0:01:06
 ⣾ Overall              ━━━━━━━━━━━━━━━━━╸━━━━━  71%  file 2/3                        0:04:13 ETA 0:01:44
```

- **Top row:** the current file, with frames, encode speed and ETA.
- **Bottom row:** the whole batch, weighted by video length.
- Finished files are listed above the bars with ✓ or ✗. If a file fails, the ffmpeg error is shown under it.
- Press **Ctrl+C** at any time to cancel safely.

---

## 🗂 Files

| File | Purpose |
|---|---|
| `video_mixer.py` | The full app source code (one file) |
| `requirements.txt` | Python packages: `rich`, `questionary` |
| `build_exe.bat` | Builds the exe **and** the `VideoMixer Portable\` folder |
| `VideoMixer Portable\` | Created by `build_exe.bat` next to the repo folder: `VideoMixer.exe` + `README.md` (~12 MB). FFmpeg is not included. Published on the Releases page, not committed |
| `settings.json` | Your saved settings (created automatically next to the app) |

---

## 🛠 Development

### Project layout
```
VideoMixer\                   ← this repository
├── video_mixer.py            the whole app
├── requirements.txt          rich, questionary
├── build_exe.bat             build the exe + portable folder
├── README.md                 this file (also copied into the portable folder)
├── LICENSE
└── .gitignore
..\VideoMixer Portable\        ← created by build_exe.bat next to the repo (not committed)
├── VideoMixer.exe
└── README.md
```

### Update workflow
1. Edit `video_mixer.py`.
2. Test it from source (run this inside the folder with your test videos, or set **Input folder** in Settings):
   ```powershell
   pip install -r requirements.txt     # first time only
   python "path\to\VideoMixer Source\video_mixer.py"
   ```
3. Double-click `build_exe.bat`. It:
   1. builds `VideoMixer.exe` with PyInstaller (temporary files go to `.build\`)
   2. recreates `..\VideoMixer Portable\` with the new exe and the README
4. If you added a feature, update this README too; the build copies it into the portable folder.

The source is organised in sections: **Encoders** (`ENCODERS` table and `video_args()`), **Settings** (dataclass that loads and saves), **FFmpeg helpers** (`detect_encoders()`, `probe()`, `build_command()`), **Encoding** (`run_ffmpeg()`, `process_all()`) and **UI** (menus and header).

**Adding an encoder:** add an `EncoderSpec` to `ENCODERS` and a matching branch in `video_args()`.
**Accepting other input formats:** edit `INPUT_EXTENSIONS`, for example `(".mp4", ".mkv", ".mov")`.

---

## ❓ Troubleshooting

| Problem | Fix |
|---|---|
| "FFmpeg not found" | Install FFmpeg (see above), then open a **new** terminal window |
| A GPU shows ✗ | Update your GPU driver. AV1 needs an RTX 40-series / Intel Arc / Radeon RX 7000 or newer |
| Windows SmartScreen warning | Click **More info → Run anyway** (the exe isn't code-signed) |
| "No eligible files" | Run the app in the folder with your videos, or set **Settings → Input folder** |
| A file fails with ✗ | Read the red error lines under it. Try the CPU encoder to rule out a GPU driver problem |
| Weird characters in the old console | Use **Windows Terminal** for the best look |

---

## 📄 License

Released under the [MIT License](LICENSE).

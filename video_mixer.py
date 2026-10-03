#!/usr/bin/env python3
"""
Video Mixer
===========
Batch-process screen recordings with FFmpeg:

  * mixes audio track 1 (system sounds) + track 2 (microphone) into a new "Mix" track
  * keeps the original tracks, renamed ("System sounds", "Microphone")
  * per-track rules: which tracks go into the Mix and which are kept, based on a scan of the input folder
  * can add an audio file with the same name as the video (recording.wav) as an extra track
  * what to do with 1-track / silent files
  * re-encodes the video to AV1 or H.264 on CPU, NVIDIA (NVENC), Intel (QSV) or AMD (AMF)

Settings are stored in settings.json next to this script / exe.
Requires: ffmpeg + ffprobe (on PATH or next to the app), `pip install rich questionary`.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

try:
    import questionary
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
    from questionary import Choice, Separator
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.markup import escape
    from rich.panel import Panel
    from rich.progress import (
        Progress,
        ProgressColumn,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.progress_bar import ProgressBar
    from rich.table import Column, Table
    from rich.text import Text
except ImportError:
    print("Missing Python packages. Install them with:\n\n    pip install rich questionary\n")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────

APP_NAME = "Video Mixer"
APP_VERSION = "2.1"
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "settings.json"
INPUT_EXTENSIONS = (".mp4",)
IS_WINDOWS = os.name == "nt"

for _stream in (sys.stdout, sys.stderr):  # never crash on ✓ ▶ ━ with a non-UTF-8 codepage
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ACCENT = "#be46ff"
ACCENT2 = "#00d2ff"

console = Console(highlight=False)

QSTYLE = questionary.Style([
    ("qmark", f"fg:{ACCENT2} bold"),
    ("question", "bold"),
    ("pointer", f"fg:{ACCENT} bold"),
    ("highlighted", f"fg:{ACCENT} bold"),
    ("selected", f"fg:{ACCENT2}"),
    ("answer", f"fg:{ACCENT2} bold"),
    ("instruction", "fg:#777777 italic"),
    ("disabled", "fg:#666666 italic"),
])


# ─────────────────────────────────────────────────────────────────────────────
#  Encoders
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EncoderSpec:
    name: str
    codec: str  # "AV1" | "H.264"
    backend: str  # "CPU" | "NVIDIA" | "Intel" | "AMD"
    label: str
    quality_name: str
    quality_default: int
    quality_range: tuple[int, int]
    presets: tuple[str, ...]
    preset_default: str
    preset_hint: str


NVENC_PRESETS = ("p1", "p2", "p3", "p4", "p5", "p6", "p7")
QSV_PRESETS = ("veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")

ENCODERS: dict[str, EncoderSpec] = {e.name: e for e in (
    EncoderSpec("libsvtav1", "AV1", "CPU", "SVT-AV1", "CRF", 23, (0, 63),
                tuple(str(i) for i in range(14)), "6", "0 = slowest/best  ·  13 = fastest"),
    EncoderSpec("av1_nvenc", "AV1", "NVIDIA", "NVENC", "CQ", 23, (0, 63),
                NVENC_PRESETS, "p6", "p1 = fastest  ·  p7 = best quality"),
    EncoderSpec("av1_qsv", "AV1", "Intel", "Quick Sync", "ICQ", 23, (1, 51),
                QSV_PRESETS, "slower", "veryfast … veryslow"),
    EncoderSpec("av1_amf", "AV1", "AMD", "AMF", "QP", 100, (0, 255),
                ("speed", "balanced", "quality", "high_quality"), "quality", "speed … high_quality"),
    EncoderSpec("libx264", "H.264", "CPU", "x264", "CRF", 20, (0, 51),
                X264_PRESETS, "slow", "ultrafast … veryslow"),
    EncoderSpec("h264_nvenc", "H.264", "NVIDIA", "NVENC", "CQ", 23, (0, 51),
                NVENC_PRESETS, "p6", "p1 = fastest  ·  p7 = best quality"),
    EncoderSpec("h264_qsv", "H.264", "Intel", "Quick Sync", "ICQ", 23, (1, 51),
                QSV_PRESETS, "slower", "veryfast … veryslow"),
    EncoderSpec("h264_amf", "H.264", "AMD", "AMF", "QP", 23, (0, 51),
                ("speed", "balanced", "quality"), "quality", "speed … quality"),
)}

CODECS = ("AV1", "H.264")
BACKENDS = ("CPU", "NVIDIA", "Intel", "AMD")


def video_args(encoder: str, quality: int, preset: str, ten_bit: bool) -> list[str]:
    """FFmpeg video-encoder arguments for the given encoder."""
    spec = ENCODERS[encoder]
    q = str(quality)
    ten_bit = ten_bit and spec.codec == "AV1"  # 10-bit H.264 has poor player support

    if encoder == "libsvtav1":
        args, pix = ["-crf", q, "-preset", preset], "yuv420p10le" if ten_bit else "yuv420p"
    elif encoder == "libx264":
        args, pix = ["-crf", q, "-preset", preset], "yuv420p"
    elif encoder.endswith("_nvenc"):
        args, pix = ["-rc", "vbr", "-cq", q, "-b:v", "0", "-preset", preset], "p010le" if ten_bit else "yuv420p"
    elif encoder.endswith("_qsv"):
        args, pix = ["-global_quality", q, "-preset", preset], "p010le" if ten_bit else "nv12"
    else:  # AMF
        args = ["-rc", "cqp", "-qp_i", q, "-qp_p", q, "-quality", preset]
        if encoder == "h264_amf":
            args += ["-qp_b", q]
        pix = "p010le" if ten_bit else "nv12"

    return ["-c:v", encoder, *args, "-pix_fmt", pix]


SINGLE_TRACK_MODES = {"keep": "keep the track", "skip": "skip the file"}
NO_AUDIO_MODES = {"encode": "encode video only", "skip": "skip the file"}
EXTERNAL_EXTENSIONS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus")
VOLUME_RANGE = (0, 400)  # percent; 100 = unchanged


def default_rule(i: int) -> dict:
    """Rule for source audio track i (0-based): output name, In Mix / Keep, and volumes in percent.

    mix_volume = how loud the track is inside the Mix (the microphone is 75% by default),
    volume = how loud its own kept track is.
    """
    return {"name": ("System sounds", "Microphone")[i] if i < 2 else f"Track {i + 1}", "mix": True, "keep": True,
            "mix_volume": 75 if i == 1 else 100, "volume": 100}


def clean_rule(rule, fallback: dict) -> dict:
    if not isinstance(rule, dict):
        return dict(fallback)
    name = rule.get("name")
    clean = {"name": name.strip() if isinstance(name, str) and name.strip() else fallback["name"],
             "mix": rule.get("mix") if isinstance(rule.get("mix"), bool) else fallback["mix"],
             "keep": rule.get("keep") if isinstance(rule.get("keep"), bool) else fallback["keep"]}
    for key in ("mix_volume", "volume"):
        vol = rule.get(key)
        valid = type(vol) is int and VOLUME_RANGE[0] <= vol <= VOLUME_RANGE[1]
        clean[key] = vol if valid else fallback[key]
    return clean


# ─────────────────────────────────────────────────────────────────────────────
#  Settings
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Settings:
    codec: str = "AV1"
    encoder: str = "libsvtav1"
    quality: int = 23
    preset: str = "6"
    ten_bit: bool = True
    fps: str = "60"  # a number, or "source" to keep the original frame rate
    audio_bitrate: str = "192k"
    mix_title: str = "Mix"
    single_title: str = "Audio"
    mix_enabled: bool = True  # create the "Mix" track when 2+ tracks are marked "In Mix"
    tracks: list = field(default_factory=lambda: [default_rule(0), default_rule(1)])  # rules per track number
    external_audio: bool = False  # add "<video name>.wav/.mp3/…" next to the video as an extra track
    external: dict = field(default_factory=lambda: {"name": "External audio", "mix": True, "keep": True,
                                                    "mix_volume": 100, "volume": 100})
    single_track: str = "keep"  # files with 1 audio track: "keep" | "skip"
    no_audio: str = "encode"  # files without audio: "encode" (video only) | "skip"
    input_dir: str = ""  # empty = current folder
    output_dir: str = "Processed - {backend}"  # relative to the input folder; {backend} / {codec} placeholders
    suffix: str = "-Mix"

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return s
        for f in fields(cls):
            if f.name in data and type(data[f.name]) is type(getattr(s, f.name)):
                setattr(s, f.name, data[f.name])
        if s.encoder not in ENCODERS:
            s.apply_encoder(cls.encoder)
        if s.single_track not in SINGLE_TRACK_MODES:
            s.single_track = cls.single_track
        if s.no_audio not in NO_AUDIO_MODES:
            s.no_audio = cls.no_audio
        if "tracks" not in data:  # settings from v2.0: per-file track names + "keep originals"
            keep = data.get("keep_originals", True) is not False or data.get("mix_enabled") is False
            s.tracks = [{**default_rule(0), "keep": keep}, {**default_rule(1), "keep": keep}]
            for i, old in enumerate(("system_title", "mic_title")):
                if isinstance(data.get(old), str) and data[old].strip():
                    s.tracks[i]["name"] = data[old].strip()
        s.tracks = [clean_rule(r, default_rule(i)) for i, r in enumerate(s.tracks)]
        s.external = clean_rule(s.external, cls().external)
        return s

    def save(self) -> None:
        data = asdict(self)
        # v2.0 fields, so an older version opened with this settings.json keeps the track names
        data["system_title"], data["mic_title"] = self.rule(0)["name"], self.rule(1)["name"]
        data["keep_originals"] = self.rule(0)["keep"] or self.rule(1)["keep"]
        try:
            SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as e:
            console.print(f"[yellow]Could not save settings: {e}[/]")

    def apply_encoder(self, name: str) -> None:
        spec = ENCODERS[name]
        self.encoder, self.codec = name, spec.codec
        self.quality, self.preset = spec.quality_default, spec.preset_default

    @property
    def spec(self) -> EncoderSpec:
        return ENCODERS[self.encoder]

    def input_path(self) -> Path:
        return Path(self.input_dir).expanduser() if self.input_dir else Path.cwd()

    def output_path(self) -> Path:
        try:
            name = self.output_dir.format(backend=self.spec.backend, codec=self.codec.replace(".", ""))
        except (KeyError, IndexError, ValueError):
            name = self.output_dir
        p = Path(name).expanduser()
        return p if p.is_absolute() else self.input_path() / p

    def rule(self, i: int) -> dict:
        """Rule for source audio track i (0-based). Tracks without a saved rule use the default."""
        return self.tracks[i] if i < len(self.tracks) else default_rule(i)

    def audio_summary(self) -> str:
        rules = [(str(i + 1), r) for i, r in enumerate(self.tracks)]
        if self.external_audio:
            rules.append(("ext", self.external))
        mix = "+".join(label for label, r in rules if r["mix"])
        keep = ",".join(label for label, r in rules if r["keep"]) or "none"
        mode = f"Mix {mix}" if self.mix_enabled and mix else "no Mix"
        return f"{mode} · keep {keep} · 1-track: {self.single_track} · no audio: {self.no_audio}"

    def summary(self) -> str:
        spec = self.spec
        bits = "10-bit" if self.ten_bit and self.codec == "AV1" else "8-bit"
        fps = "source fps" if self.fps == "source" else f"{self.fps} fps"
        return (f"{self.codec} · {spec.backend} {spec.label} · {spec.quality_name} {self.quality} · "
                f"preset {self.preset} · {bits} · {fps} · AAC {self.audio_bitrate}")


# ─────────────────────────────────────────────────────────────────────────────
#  FFmpeg helpers
# ─────────────────────────────────────────────────────────────────────────────

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def find_tool(name: str) -> str | None:
    exe = name + (".exe" if IS_WINDOWS else "")
    for folder in (APP_DIR, APP_DIR / "ffmpeg" / "bin", APP_DIR / "bin", Path.cwd()):
        if (folder / exe).is_file():
            return str(folder / exe)
    return shutil.which(name)


def run_quiet(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=timeout)


def detect_encoders() -> set[str]:
    """Return the encoders that actually work on this machine (test-encodes a few frames)."""
    try:
        listing = run_quiet([FFMPEG, "-hide_banner", "-encoders"]).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    compiled = [name for name in ENCODERS if f" {name} " in listing]

    def works(name: str) -> bool:
        spec = ENCODERS[name]
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
               "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=0.5",
               *video_args(name, spec.quality_default, spec.preset_default, ten_bit=False),
               "-frames:v", "5", "-f", "null", "-"]
        try:
            return run_quiet(cmd, timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip(compiled, pool.map(works, compiled)))
    return {name for name, ok in results.items() if ok}


@dataclass
class MediaInfo:
    duration: float  # seconds (0 if unknown)
    fps: float  # source frame rate (0 if unknown)
    audio_streams: int
    has_video: bool


def _parse_rate(rate: str | None) -> float:
    try:
        num, _, den = (rate or "").partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: Path) -> MediaInfo:
    cmd = [FFPROBE, "-v", "error",
           "-show_entries", "format=duration:stream=codec_type,avg_frame_rate,r_frame_rate",
           "-of", "json", str(path)]
    try:
        data = json.loads(run_quiet(cmd).stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        data = {}
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    try:
        duration = float(data.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    fps = 0.0
    if video:
        fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    return MediaInfo(duration=duration, fps=fps,
                     audio_streams=sum(1 for s in streams if s.get("codec_type") == "audio"),
                     has_video=video is not None)


def find_external(video: Path) -> Path | None:
    """An audio file with the same name next to the video (recording.mp4 → recording.wav / .mp3 / …)."""
    for ext in EXTERNAL_EXTENSIONS:
        p = video.with_suffix(ext)
        if p.is_file():
            return p
    return None


@dataclass
class AudioSource:
    spec: str  # ffmpeg stream specifier: "0:a:1" = track 2 of the video, "1:a:0" = the external file
    label: str  # short label for the file list: "1", "2", "ext"
    title: str  # track name in the output
    volume: int = 100  # own kept track, percent; 100 = unchanged
    mix_volume: int = 100  # inside the Mix, percent


@dataclass
class AudioPlan:
    sources: int  # audio tracks available (video tracks + external file)
    has_external: bool
    mix: list[AudioSource]  # mixed into the "Mix" track (empty = no Mix)
    keep: list[AudioSource]  # copied to the output as separate tracks

    @property
    def uses_external(self) -> bool:
        return any(x.spec.startswith("1:") for x in self.mix + self.keep)

    def describe(self) -> str:
        n = self.sources - self.has_external
        head = "no audio" if n == 0 else f"{n} track{'s' if n != 1 else ''}"
        head = "ext" if n == 0 and self.has_external else head + (" + ext" if self.has_external else "")
        parts = []
        if self.mix:
            parts.append("Mix " + "+".join(x.label for x in self.mix))
        if self.keep:
            parts.append("keep " + ",".join(x.label for x in self.keep))
        return f"{head} → {' · '.join(parts) or 'video only'}"


def plan_audio(s: Settings, info: MediaInfo, external: Path | None) -> AudioPlan | None:
    """Which tracks go into the Mix and which are kept for one file, or None if the file is skipped."""
    specs = [(f"0:a:{i}", str(i + 1), s.rule(i)) for i in range(info.audio_streams)]
    if external:
        specs.append(("1:a:0", "ext", s.external))
    sources = [AudioSource(spec, label, r["name"], r["volume"], r["mix_volume"]) for spec, label, r in specs]
    rules = [r for _, _, r in specs]
    plan = AudioPlan(sources=len(sources), has_external=external is not None, mix=[], keep=[])

    if not sources:
        return plan if s.no_audio == "encode" else None
    if len(sources) == 1:  # nothing to mix
        if s.single_track == "skip":
            return None
        plan.keep = [replace(sources[0], title=s.single_title)]
        return plan

    mix = [x for x, r in zip(sources, rules) if r["mix"]] if s.mix_enabled else []
    if len(mix) == 1:  # a Mix of one track is just that track: keep it instead
        plan.keep = [x for x, r in zip(sources, rules) if r["keep"] or x is mix[0]]
    else:
        plan.mix = mix
        plan.keep = [x for x, r in zip(sources, rules) if r["keep"]]
    return plan


def build_command(s: Settings, src: Path, dst: Path, info: MediaInfo, plan: AudioPlan,
                  external: Path | None) -> list[str]:
    cmd = [FFMPEG, "-y", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(src)]
    if external and plan.uses_external:
        cmd += ["-i", str(external)]
    cmd += ["-map", "0:v:0"]

    # Each use of a track in the filter graph: its Mix input, and its kept track if that needs a volume change.
    # A track used twice is split in two first; each part gets its own volume.
    uses: dict[str, list[tuple[str, int]]] = {}
    for x in plan.mix:
        uses.setdefault(x.spec, []).append(("mix", x.mix_volume))
    for x in plan.keep:
        if x.volume != 100:
            uses.setdefault(x.spec, []).append(("keep", x.volume))
    filters: list[str] = []
    pads: dict[tuple[str, str], str] = {}  # (spec, "mix" | "keep") → filter graph label
    for n, (spec, spec_uses) in enumerate(uses.items()):
        parts = [f"s{n}_{j}" for j in range(len(spec_uses))]
        if len(spec_uses) > 1:
            filters.append(f"[{spec}]asplit={len(spec_uses)}" + "".join(f"[{p}]" for p in parts))
        else:
            parts = [spec]
        for (use, vol), part in zip(spec_uses, parts):
            if vol == 100:
                pads[(spec, use)] = f"[{part}]"
            else:
                filters.append(f"[{part}]volume={vol / 100:g}[v{n}_{use}]")
                pads[(spec, use)] = f"[v{n}_{use}]"

    titles: list[str] = []
    if plan.mix:
        inputs = "".join(pads[(x.spec, "mix")] for x in plan.mix)
        filters.append(f"{inputs}amix=inputs={len(plan.mix)}:duration=longest[mix]")
        cmd += ["-filter_complex", ";".join(filters), "-map", "[mix]"]
        titles.append(s.mix_title)
    elif filters:
        cmd += ["-filter_complex", ";".join(filters)]
    for x in plan.keep:
        cmd += ["-map", pads.get((x.spec, "keep"), x.spec)]
        titles.append(x.title)
    for i, title in enumerate(titles):  # the first track (the Mix, if any) is the default
        cmd += [f"-metadata:s:a:{i}", f"title={title}", f"-disposition:a:{i}", "default" if i == 0 else "0"]

    cmd += video_args(s.encoder, s.quality, s.preset, s.ten_bit)
    cmd += ["-color_range", "tv"]
    if s.fps != "source":
        cmd += ["-r", s.fps]
    if titles:
        cmd += ["-c:a", "aac", "-b:a", s.audio_bitrate]
    if plan.uses_external and info.duration:  # a longer external file must not make the output longer
        cmd += ["-t", f"{info.duration:.3f}"]
    cmd += ["-progress", "pipe:1", "-nostats", str(dst)]
    return cmd


# ─────────────────────────────────────────────────────────────────────────────
#  Formatting
# ─────────────────────────────────────────────────────────────────────────────

def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _num(value: str | None) -> float:
    try:
        return float((value or "").rstrip("x"))
    except ValueError:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  Animated gradient progress bar
# ─────────────────────────────────────────────────────────────────────────────

PALETTES = {
    "file": ((0, 210, 255), (190, 70, 255)),
    "total": ((255, 190, 0), (255, 70, 150)),
}


def _lerp(a: tuple[int, ...], b: tuple[int, ...], t: float) -> tuple[int, ...]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


class GradientBar(ProgressColumn):
    """A gradient bar with a light shimmer that sweeps across the filled part."""

    def __init__(self, width: int = 36):
        super().__init__()
        self.width = width

    def render(self, task) -> ProgressBar | Text:
        c1, c2 = PALETTES[task.fields.get("palette", "file")]
        if not task.total:
            return ProgressBar(total=None, width=self.width, pulse=True, animation_time=task.get_time(),
                               pulse_style="#%02x%02x%02x" % c2)

        ratio = min(task.completed / task.total, 1.0)
        filled = ratio * self.width
        full = int(filled)
        half = filled - full >= 0.5
        bar = Text()

        if task.finished:
            bar.append("━" * self.width, style="#3ddc84")
            return bar

        shimmer = ((time.monotonic() * 0.7) % 1.6 - 0.3) * self.width
        for i in range(full):
            color = _lerp(c1, c2, i / max(1, self.width - 1))
            glow = max(0.0, 1 - abs(i - shimmer) / 4)
            color = _lerp(color, (255, 255, 255), glow * 0.6)
            bar.append("━", style="#%02x%02x%02x" % color)
        if half:
            bar.append("╸", style="#%02x%02x%02x" % _lerp(c1, c2, full / max(1, self.width - 1)))
        bar.append("━" * (self.width - full - int(half)), style="grey23")
        return bar


# ─────────────────────────────────────────────────────────────────────────────
#  Encoding
# ─────────────────────────────────────────────────────────────────────────────

class Cancelled(Exception):
    pass


def run_ffmpeg(cmd: list[str], on_progress) -> tuple[int, list[str]]:
    """Run ffmpeg, calling on_progress(stats: dict) for each progress block. Returns (exit code, errors)."""
    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # Ctrl+C is handled by us, not ffmpeg
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", **kwargs)
    lines: queue.Queue[str | None] = queue.Queue()
    errors: deque[str] = deque(maxlen=30)

    def pump_stdout():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def pump_stderr():
        for line in proc.stderr:
            line = line.strip()
            if line and not line.startswith("Svt[info]"):  # SVT-AV1 logs its config even at -loglevel error
                errors.append(line)

    threading.Thread(target=pump_stdout, daemon=True).start()
    threading.Thread(target=pump_stderr, daemon=True).start()

    stats: dict[str, str] = {}
    try:
        while True:
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            key, _, value = line.strip().partition("=")
            stats[key] = value
            if key == "progress":
                on_progress(stats)
        proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        raise Cancelled from None
    return proc.returncode, list(errors)


def list_inputs(s: Settings) -> tuple[list[Path], list[Path]]:
    folder = s.input_path()
    files = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in INPUT_EXTENSIONS),
                   key=lambda p: p.name.lower())
    skip = [p for p in files if p.stem.lower().endswith(s.suffix.lower())]
    return [p for p in files if p not in skip], skip


def process_all(s: Settings) -> None:
    header(s)
    folder = s.input_path()
    if not folder.is_dir():
        console.print(f"[red]Input folder not found:[/] {folder}")
        pause()
        return

    todo, skipped = list_inputs(s)
    if not todo:
        console.print(f"[yellow]No eligible {'/'.join(INPUT_EXTENSIONS)} files in[/] {folder}")
        if skipped:
            console.print(f"[dim]({len(skipped)} file(s) skipped because they end with '{s.suffix}')[/]")
        pause()
        return

    with console.status("Reading media info…", spinner="dots"):
        infos = {p: probe(p) for p in todo}
        externals = {p: find_external(p) if s.external_audio else None for p in todo}
        for p, ext in externals.items():
            if ext and probe(ext).audio_streams == 0:  # not a readable audio file
                externals[p] = None
    plans = {p: plan_audio(s, infos[p], externals[p]) for p in todo}
    by_rule = [p for p in todo if infos[p].has_video and plans[p] is None]
    todo = [p for p in todo if p not in by_rule]

    table = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ACCENT2}", expand=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("File")
    table.add_column("Size", justify="right")
    table.add_column("Audio")
    table.add_column("Status")
    for i, p in enumerate(todo, 1):
        info = infos[p]
        plan = plans[p].describe() if info.has_video else "[red]no video stream[/]"
        table.add_row(str(i), p.name, fmt_size(p.stat().st_size), plan, "[green]queued[/]")
    for p in by_rule:
        n = infos[p].audio_streams
        reason = "no audio" if n + (externals[p] is not None) == 0 else "only 1 audio track"
        table.add_row("", f"[dim]{p.name}[/]", f"[dim]{fmt_size(p.stat().st_size)}[/]",
                      f"[dim]{n} track(s)[/]", f"[dim]skip ({reason})[/]")
    for p in skipped:
        table.add_row("", f"[dim]{p.name}[/]", f"[dim]{fmt_size(p.stat().st_size)}[/]", "",
                      "[dim]skip (already mixed)[/]")
    console.print(table)
    console.print(f"  Audio  → [bold]{s.audio_summary()}[/]")
    console.print(f"  Output → [bold]{s.output_path()}[/]\n")
    skipped += by_rule

    if not todo:
        console.print("[yellow]Nothing to process with the current audio track settings.[/]")
        pause()
        return
    if not questionary.confirm(f"Process {len(todo)} file(s)?", default=True, style=QSTYLE).ask():
        return

    out_dir = s.output_path()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        console.print(f"[red]Cannot create output folder:[/] {e}")
        pause()
        return

    file_progress = Progress(
        SpinnerColumn("dots", style=ACCENT2, finished_text="[green]✓[/]"),
        TextColumn("{task.description}", table_column=Column(width=26, no_wrap=True, overflow="ellipsis")),
        GradientBar(),
        TaskProgressColumn(),
        TextColumn("[dim]{task.fields[frames]}"),
        TextColumn("[cyan]{task.fields[fps]}"),
        TextColumn("[magenta]{task.fields[speed]}"),
        TimeElapsedColumn(),
        TextColumn("[dim]ETA"),
        TimeRemainingColumn(),
        console=console,
    )
    overall_progress = Progress(
        SpinnerColumn("dots12", style="#ffbe00"),
        TextColumn("[bold]{task.description}", table_column=Column(width=26, no_wrap=True)),
        GradientBar(),
        TaskProgressColumn(),
        TextColumn("[dim]{task.fields[info]}"),
        TimeElapsedColumn(),
        TextColumn("[dim]ETA"),
        TimeRemainingColumn(),
        console=console,
    )

    total_duration = sum(i.duration for i in infos.values())
    overall = overall_progress.add_task("Overall", total=total_duration or None, info="", palette="total")
    done_duration = 0.0
    ok = failed = 0
    size_in = size_out = 0
    cancelled = False
    start = time.monotonic()

    console.rule(f"[bold]{s.summary()}", style=ACCENT)
    with Live(Group(file_progress, overall_progress), console=console, refresh_per_second=15, transient=False):
        for idx, src in enumerate(todo, 1):
            info = infos[src]
            dst = out_dir / f"{src.stem}{s.suffix}.mp4"
            overall_progress.update(overall, info=f"file {idx}/{len(todo)}")

            if not info.has_video:
                console.print(f"  [red]✗[/] {src.name}  [dim]no video stream / unreadable file[/]")
                failed += 1
                done_duration += info.duration
                continue

            out_fps = info.fps if s.fps == "source" else _num(s.fps)
            est_frames = int(info.duration * out_fps) if info.duration and out_fps else 0
            task = file_progress.add_task(src.name, total=info.duration or None,
                                          frames="", fps="", speed="", palette="file")

            def on_progress(st: dict[str, str], task=task, est_frames=est_frames, info=info):
                out_us = _num(st.get("out_time_us") or st.get("out_time_ms"))
                pos = min(out_us / 1_000_000, info.duration) if info.duration else 0
                frame = int(_num(st.get("frame")))
                file_progress.update(
                    task, completed=pos,
                    frames=f"{frame}/{est_frames} fr" if est_frames else f"{frame} fr",
                    fps=f"{_num(st.get('fps')):.0f} fps",
                    speed=f"{_num(st.get('speed')):.2f}x",
                )
                overall_progress.update(overall, completed=done_duration + pos)

            t0 = time.monotonic()
            try:
                code, errors = run_ffmpeg(build_command(s, src, dst, info, plans[src], externals[src]), on_progress)
            except Cancelled:
                cancelled = True
                file_progress.remove_task(task)
                dst.unlink(missing_ok=True)
                console.print(f"  [yellow]■[/] {src.name}  [yellow]cancelled — partial output removed[/]")
                break

            file_progress.remove_task(task)
            done_duration += info.duration
            overall_progress.update(overall, completed=done_duration)
            took = fmt_time(time.monotonic() - t0)

            if code == 0 and dst.exists():
                ok += 1
                a, b = src.stat().st_size, dst.stat().st_size
                size_in, size_out = size_in + a, size_out + b
                change = (b - a) / a * 100 if a else 0
                console.print(f"  [green]✓[/] {src.name} [dim]→[/] {dst.name}   "
                              f"[dim]{fmt_size(a)} → {fmt_size(b)} ({change:+.0f}%)   {took}[/]")
            else:
                failed += 1
                dst.unlink(missing_ok=True)
                console.print(f"  [red]✗[/] {src.name}  [red]ffmpeg exited with code {code}[/]")
                for e in errors[-6:]:
                    console.print(f"      [dim red]{e}[/]")

        if not cancelled:
            overall_progress.update(overall, completed=total_duration or 1, total=total_duration or 1)

    # ── Summary ──
    grid = Table.grid(padding=(0, 3))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("Processed", f"[green]{ok}[/]")
    grid.add_row("Skipped", f"[dim]{len(skipped)}[/]")
    grid.add_row("Failed", f"[red]{failed}[/]" if failed else "[green]0[/]")
    if cancelled:
        grid.add_row("Cancelled", f"[yellow]{len(todo) - ok - failed}[/] not processed")
    grid.add_row("Total time", fmt_time(time.monotonic() - start))
    if size_in:
        grid.add_row("Size", f"{fmt_size(size_in)} → {fmt_size(size_out)} "
                             f"([bold]{(size_out - size_in) / size_in * 100:+.0f}%[/])")
    grid.add_row("Output", str(out_dir))
    style = "yellow" if cancelled else ("red" if failed else "green")
    console.print()
    console.print(Panel(grid, title="[bold]Summary", border_style=style, expand=False, padding=(1, 3)))
    pause()


# ─────────────────────────────────────────────────────────────────────────────
#  UI
# ─────────────────────────────────────────────────────────────────────────────

AVAILABLE: set[str] = set()


def pause() -> None:
    try:
        questionary.press_any_key_to_continue("Press any key to continue…", style=QSTYLE).ask()
    except Exception:  # no interactive console (redirected / unusual terminal)
        try:
            input("Press Enter to continue…")
        except EOFError:
            pass


def header(s: Settings) -> None:
    console.clear()
    status = Text()
    for codec in CODECS:
        status.append(f"{codec:<6}", style="bold")
        for backend in BACKENDS:
            enc = next(e for e in ENCODERS.values() if e.codec == codec and e.backend == backend)
            okay = enc.name in AVAILABLE
            status.append(f"  {'✓' if okay else '✗'} {backend}", style="green" if okay else "grey42")
        status.append("\n")

    body = Group(
        Text(s.summary(), style=f"bold {ACCENT2}"),
        Text(f"Audio  {s.audio_summary()}", style=ACCENT2),
        Text(f"Input  {s.input_path()}", style="dim"),
        Text(f"Output {s.output_path()}", style="dim"),
        Text(""),
        status,
    )
    title = Text.assemble(("▶ ", ACCENT2), (APP_NAME.upper(), f"bold {ACCENT}"), (f" v{APP_VERSION}", "dim"))
    console.print(Panel(body, title=title, subtitle="[dim]mix audio · encode AV1 / H.264",
                        border_style=ACCENT, box=box.ROUNDED, padding=(0, 2)))


def show_encoders(s: Settings) -> None:
    header(s)
    table = Table(box=box.ROUNDED, header_style=f"bold {ACCENT2}", border_style="grey42")
    table.add_column("Backend")
    for codec in CODECS:
        table.add_column(codec)
    for backend in BACKENDS:
        row = [backend]
        for codec in CODECS:
            enc = next(e for e in ENCODERS.values() if e.codec == codec and e.backend == backend)
            row.append(f"[green]✓ {enc.name}[/]" if enc.name in AVAILABLE else f"[grey42]✗ {enc.name}[/]")
        table.add_row(*row)
    console.print(table)
    console.print(f"[dim]ffmpeg:  {FFMPEG}\nffprobe: {FFPROBE}[/]\n")
    pause()


def ask_text(msg: str, default: str, validate=None) -> str | None:
    return questionary.text(msg, default=default, validate=validate, style=QSTYLE).ask()


def select(message: str, choices, back=None, **kwargs):
    """questionary.select where Backspace goes back: it answers `back` (None = cancel, nothing changes)."""
    kwargs.setdefault("style", QSTYLE)
    kwargs.setdefault("instruction", "(↑↓ enter · ⌫ back)")
    question = questionary.select(message, choices=choices, **kwargs)
    keys = KeyBindings()

    @keys.add("backspace", eager=True)
    def _(event):
        event.app.exit(result=back)

    app = question.application
    app.key_bindings = merge_key_bindings([app.key_bindings, keys]) if app.key_bindings else keys
    return question


def settings_menu(s: Settings) -> None:
    while True:
        header(s)
        spec = s.spec
        av1 = s.codec == "AV1"
        row = lambda k, v: f"{k:<22}{v}"  # noqa: E731
        choices = [
            Choice(row("Codec", s.codec), "codec"),
            Choice(row("Encoder", f"{spec.backend} · {spec.label}  ({spec.name})"), "encoder"),
            Choice(row(f"Quality ({spec.quality_name})", s.quality), "quality"),
            Choice(row("Preset", s.preset), "preset"),
            Choice(row("10-bit color", ("on" if s.ten_bit else "off") if av1 else "8-bit"),
                   "ten_bit", disabled=None if av1 else "AV1 only"),
            Choice(row("Output frame rate", "keep source" if s.fps == "source" else f"{s.fps} fps"), "fps"),
            Choice(row("Audio bitrate", s.audio_bitrate), "bitrate"),
            Choice(row("Audio tracks", s.audio_summary()), "tracks"),
            Separator(),
            Choice(row("Input folder", s.input_dir or "(current folder)"), "input"),
            Choice(row("Output folder", s.output_dir), "output"),
            Choice(row("Output suffix", s.suffix), "suffix"),
            Separator(),
            Choice("Reset to defaults", "reset"),
            Choice("← Back", "back"),
        ]
        key = select("Settings", choices=choices, back="back", use_shortcuts=False).ask()
        if key in (None, "back"):
            return

        if key == "codec":
            codec = select("Video codec", choices=list(CODECS), default=s.codec, style=QSTYLE).ask()
            if codec and codec != s.codec:
                same = next((e.name for e in ENCODERS.values()
                             if e.codec == codec and e.backend == spec.backend and e.name in AVAILABLE), None)
                cpu = next(e.name for e in ENCODERS.values() if e.codec == codec and e.backend == "CPU")
                s.apply_encoder(same or cpu)

        elif key == "encoder":
            opts = [Choice(f"{e.backend:<8}{e.label:<12}({e.name})", e.name,
                           disabled=None if e.name in AVAILABLE else "not available on this PC")
                    for e in ENCODERS.values() if e.codec == s.codec]
            name = select("Encoder", choices=opts, style=QSTYLE,
                                      default=s.encoder if s.encoder in AVAILABLE else None).ask()
            if name and name != s.encoder:
                s.apply_encoder(name)

        elif key == "quality":
            lo, hi = spec.quality_range
            val = ask_text(f"{spec.quality_name} ({lo}-{hi}, lower = better quality / bigger file):", str(s.quality),
                           lambda v: (v.isdigit() and lo <= int(v) <= hi) or f"Enter a number from {lo} to {hi}")
            if val:
                s.quality = int(val)

        elif key == "preset":
            val = select(f"Preset   [{spec.preset_hint}]", choices=list(spec.presets),
                                     default=s.preset if s.preset in spec.presets else None, style=QSTYLE).ask()
            if val:
                s.preset = val

        elif key == "ten_bit":
            s.ten_bit = not s.ten_bit

        elif key == "fps":
            opts = [Choice("keep source", "source")] + [Choice(f"{f} fps", f) for f in
                                                        ("24", "25", "30", "50", "60", "120")] + [
                       Choice("custom…", "custom")]
            val = select("Output frame rate", choices=opts, style=QSTYLE).ask()
            if val == "custom":
                val = ask_text("Frame rate:", s.fps if s.fps != "source" else "60",
                               lambda v: _num(v) > 0 or "Enter a positive number")
            if val:
                s.fps = val

        elif key == "bitrate":
            val = select("AAC audio bitrate", choices=["96k", "128k", "160k", "192k", "256k", "320k"],
                                     default=s.audio_bitrate if s.audio_bitrate.endswith("k") else None,
                                     style=QSTYLE).ask()
            if val:
                s.audio_bitrate = val

        elif key == "tracks":
            tracks_menu(s)

        elif key == "input":
            val = questionary.path("Input folder (empty = current folder):", default=s.input_dir,
                                   only_directories=True, style=QSTYLE,
                                   validate=lambda v: not v or Path(v).expanduser().is_dir() or "Folder not found").ask()
            if val is not None:
                s.input_dir = val.strip().strip('"')

        elif key == "output":
            console.print("[dim]Relative paths are inside the input folder. Placeholders: {backend}, {codec}[/]")
            val = ask_text("Output folder:", s.output_dir, lambda v: bool(v.strip()) or "Cannot be empty")
            if val:
                s.output_dir = val.strip().strip('"')

        elif key == "suffix":
            val = ask_text("Suffix added to output names (files ending with it are skipped):", s.suffix,
                           lambda v: bool(v.strip()) or "Cannot be empty")
            if val:
                s.suffix = val.strip()

        elif key == "reset":
            if questionary.confirm("Reset all settings to defaults?", default=False, style=QSTYLE).ask():
                s.__dict__.update(asdict(Settings()))
                ensure_available_encoder(s)

        s.save()


@dataclass
class FolderScan:
    videos: int = 0
    track_counts: dict[int, int] = field(default_factory=dict)  # audio tracks per file → number of files
    with_external: int = 0
    error: str = ""

    def files_with_track(self, i: int) -> int:
        """How many videos have source track i (0-based)."""
        return sum(files for n, files in self.track_counts.items() if n > i)

    def describe(self) -> str:
        if self.error:
            return f"[yellow]{self.error}[/]"
        if not self.videos:
            return "[yellow]No videos found in the input folder[/]"
        counts = " · ".join(f"{files} {'has' if files == 1 else 'have'} "
                            + (f"{n} track{'s' if n != 1 else ''}" if n else "no audio")
                            for n, files in sorted(self.track_counts.items(), reverse=True))
        text = f"Found in input folder: [bold]{self.videos}[/] video{'s' if self.videos != 1 else ''} → {counts}"
        if self.with_external:
            text += f" · {self.with_external} with an external audio file"
        return text


def scan_folder(s: Settings) -> FolderScan:
    """Count the audio tracks of every video in the input folder."""
    if not s.input_path().is_dir():
        return FolderScan(error=f"Input folder not found: {s.input_path()}")
    todo, _ = list_inputs(s)
    with console.status("Scanning the input folder…", spinner="dots"), ThreadPoolExecutor(max_workers=8) as pool:
        infos = list(pool.map(probe, todo))
    scan = FolderScan(videos=len(todo), with_external=sum(1 for p in todo if find_external(p)))
    for info in infos:
        scan.track_counts[info.audio_streams] = scan.track_counts.get(info.audio_streams, 0) + 1
    return scan


def edit_track(s: Settings, rule: dict, label: str, external: bool = False) -> None:
    """Toggle In Mix / Keep, set the volumes and rename one track."""
    while True:
        header(s)
        onoff = lambda b: "on" if b else "off"  # noqa: E731
        no_mix = "Mix is off" if not s.mix_enabled else None if rule["mix"] else "not in the Mix"
        choices = [
            Choice(f"{'In Mix':<20}{onoff(rule['mix'])}", "mix", disabled=None if s.mix_enabled else "Mix is off"),
            Choice(f"{'Volume in Mix':<20}{rule['mix_volume']}%", "mix_volume", disabled=no_mix),
            Choice(f"{'Keep':<20}{onoff(rule['keep'])}  (as its own track)", "keep"),
            Choice(f"{'Volume (own track)':<20}{rule['volume']}%  (also used for 1-track files)", "volume"),
            Choice(f"{'Name':<20}{rule['name']}", "name"),
        ]
        if external:
            choices.append(Choice("Remove this track", "remove"))
        choices += [Separator(), Choice("← Back", "back")]
        key = select(label, choices=choices, back="back", use_shortcuts=False).ask()
        if key in (None, "back"):
            return
        if key in ("mix", "keep"):
            rule[key] = not rule[key]
        elif key == "name":
            val = ask_text("Track name:", rule["name"], lambda v: bool(v.strip()) or "Name cannot be empty")
            if val:
                rule["name"] = val.strip()
        elif key in ("mix_volume", "volume"):
            lo, hi = VOLUME_RANGE
            where = "in the Mix" if key == "mix_volume" else "of its own track"
            val = ask_text(f"Volume {where} in % ({lo}-{hi}, 100 = unchanged, 50 = half, 200 = double):",
                           str(rule[key]),
                           lambda v: (v.strip().rstrip("%").isdigit() and lo <= int(v.strip().rstrip("%")) <= hi)
                           or f"Enter a number from {lo} to {hi}")
            if val:
                rule[key] = int(val.strip().rstrip("%"))
        elif key == "remove":
            s.external_audio = False
            s.save()
            return
        s.save()


def add_external(s: Settings) -> None:
    header(s)
    exts = " ".join(EXTERNAL_EXTENSIONS)
    console.print(Panel(Text.from_markup(
        "For every video, the app looks for an [bold]audio file with the same name[/] next to it:\n\n"
        "    [yellow]recording.mp4[/]  +  [yellow]recording.wav[/]   [dim](or " + exts + ")[/]\n\n"
        "That file is added as an extra track, and goes into the Mix.\n"
        "Videos without such a file are processed as usual.\n\n"
        "[dim]The audio file should start at the same moment as the video. "
        "If it's longer, it's cut at the end of the video.[/]"),
        title="[bold]Add a track: audio file with the same name", border_style=ACCENT2, expand=False,
        padding=(1, 3)))
    if questionary.confirm("Add this track?", default=True, style=QSTYLE).ask():
        s.external_audio = True
        s.save()


MIX_LABEL = 'Create "Mix" track'


def tracks_menu(s: Settings) -> None:
    """Audio track options: scan the input folder, then Mix / Keep / name per track."""
    scan = scan_folder(s)
    while True:
        header(s)
        console.print(f"  {scan.describe()}\n")
        rows = max(len(s.tracks), max(scan.track_counts, default=0), 2)
        def track_row(label: str, rule: dict, note: str) -> str:
            name = rule["name"] if len(rule["name"]) <= 20 else rule["name"][:19] + "…"
            in_mix = ("✓ " + f"{rule['mix_volume']}%" if rule["mix"] else "✗") if s.mix_enabled else "–"
            keep = "✓ " + f"{rule['volume']}%" if rule["keep"] else "✗"
            return f"{label:<11}{name:<22}{in_mix:<10}{keep:<10}{note}"

        choices = [
            Choice(f"{MIX_LABEL:<33}{'on' if s.mix_enabled else 'off'}", "mix"),
            Separator(f"  {'Track':<11}{'Name':<22}{'In Mix':<10}{'Keep':<10}"),
        ]
        for i in range(rows):
            note = f"in {scan.files_with_track(i)}/{scan.videos} videos" if scan.videos else ""
            choices.append(Choice(track_row(f"Track {i + 1}", s.rule(i), note), f"track:{i}"))
        if s.external_audio:
            note = f"found for {scan.with_external}/{scan.videos} videos" if scan.videos else ""
            choices.append(Choice(track_row("External", s.external, note), "external"))
        else:
            choices.append(Choice("+ Add a track…  (audio file with the same name)", "add"))
        choices += [
            Separator(),
            Choice(f"{'Files with 1 audio track':<33}{SINGLE_TRACK_MODES[s.single_track]}", "single"),
            Choice(f"{'Files with no audio':<33}{NO_AUDIO_MODES[s.no_audio]}", "none"),
            Choice(f"{'Mix / 1-track names':<33}{s.mix_title} / {s.single_title}", "titles"),
            Separator(),
            Choice("↻ Rescan input folder", "rescan"),
            Choice("← Back", "back"),
        ]
        key = select("Audio tracks", choices=choices, back="back", use_shortcuts=False).ask()
        if key in (None, "back"):
            return

        if key == "mix":
            s.mix_enabled = not s.mix_enabled
        elif key.startswith("track:"):
            i = int(key.partition(":")[2])
            while len(s.tracks) <= i:  # save a rule for this track number so it can be changed
                s.tracks.append(default_rule(len(s.tracks)))
            edit_track(s, s.tracks[i], f"Track {i + 1}")
        elif key == "external":
            edit_track(s, s.external, "External audio file", external=True)
        elif key == "add":
            add_external(s)
        elif key == "rescan":
            scan = scan_folder(s)
        elif key == "single":
            val = select("Files with only 1 audio track (nothing to mix)",
                                     choices=[Choice(v, k) for k, v in SINGLE_TRACK_MODES.items()],
                                     default=s.single_track, style=QSTYLE).ask()
            if val:
                s.single_track = val
        elif key == "none":
            val = select("Files with no audio track",
                                     choices=[Choice(v, k) for k, v in NO_AUDIO_MODES.items()],
                                     default=s.no_audio, style=QSTYLE).ask()
            if val:
                s.no_audio = val
        elif key == "titles":
            nonempty = lambda v: bool(v.strip()) or "Name cannot be empty"  # noqa: E731
            for attr, label in (("mix_title", "Mixed track"), ("single_title", "Only track (1-track files)")):
                val = ask_text(f"{label}:", getattr(s, attr), nonempty)
                if val is None:
                    break
                setattr(s, attr, val.strip())
        s.save()


def ensure_available_encoder(s: Settings) -> str | None:
    """If the saved encoder doesn't work on this PC, fall back to the CPU encoder. Returns a notice."""
    if s.encoder in AVAILABLE or not AVAILABLE:
        return None
    old = s.encoder
    fallback = next((e.name for e in ENCODERS.values() if e.codec == s.codec and e.name in AVAILABLE),
                    next(iter(AVAILABLE)))
    s.apply_encoder(fallback)
    s.save()
    return f"'{old}' is not available on this PC — switched to '{fallback}'."


def main_menu(s: Settings, notice: str | None) -> None:
    while True:
        header(s)
        if notice:
            console.print(f"[yellow]⚠ {notice}[/]\n")
            notice = None
        action = questionary.select(
            "What do you want to do?",
            choices=[
                Choice("▶  Start processing", "run"),
                Choice("⚙  Settings", "settings"),
                Choice("◎  Detected encoders", "encoders"),
                Choice("✕  Exit", "exit"),
            ],
            style=QSTYLE, instruction="(↑↓ enter)",
        ).ask()
        if action in (None, "exit"):
            return
        if action == "run":
            if not AVAILABLE:
                console.print("[red]No working video encoder was found in your ffmpeg build.[/]")
                pause()
                continue
            process_all(s)
        elif action == "settings":
            settings_menu(s)
        elif action == "encoders":
            show_encoders(s)


def show_ffmpeg_missing(ffmpeg: str | None, ffprobe: str | None) -> None:
    """Explain which FFmpeg tool is missing and how to install it / add it to PATH."""
    console.clear()
    found = Table.grid(padding=(0, 2))
    for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        found.add_row(f"[bold]{name}[/]", f"[green]✓ {escape(path)}[/]" if path else "[red]✗ not found[/]")

    folders = dict.fromkeys(str(p) for p in (APP_DIR, APP_DIR / "ffmpeg" / "bin", APP_DIR / "bin", Path.cwd()))
    searched = "\n".join(f"  • {escape(p)}" for p in folders)
    parts = [
        Text("This app needs FFmpeg (ffmpeg + ffprobe) to work.", style="bold red"),
        Text(""),
        found,
        Text(""),
        Text.from_markup(f"[dim]Searched in these folders and in the system PATH:\n{searched}[/]"),
        Text(""),
    ]

    if IS_WINDOWS:
        path_cmd = escape('$p = [Environment]::GetEnvironmentVariable("Path", "User")\n'
                          '     [Environment]::SetEnvironmentVariable("Path", "$p;C:\\ffmpeg\\bin", "User")')
        parts.append(Text.from_markup(
            "[bold cyan]Option 1 — Install automatically (easiest)[/]\n"
            "  Open PowerShell and run:\n"
            "    [yellow]winget install Gyan.FFmpeg[/]\n"
            "\n"
            "[bold cyan]Option 2 — Download and add to PATH manually[/]\n"
            "  1. Download [yellow]ffmpeg-release-essentials.zip[/] from:\n"
            "       [underline]https://www.gyan.dev/ffmpeg/builds/[/]\n"
            "  2. Extract it and rename the folder to [yellow]C:\\ffmpeg[/]\n"
            "     (so this file exists: [yellow]C:\\ffmpeg\\bin\\ffmpeg.exe[/])\n"
            "  3. Add [yellow]C:\\ffmpeg\\bin[/] to PATH:\n"
            "     • Press [bold]Win + R[/], type [yellow]sysdm.cpl[/], press Enter\n"
            "     • [bold]Advanced[/] tab → [bold]Environment Variables…[/]\n"
            "     • Under [italic]User variables[/] select [bold]Path[/] → [bold]Edit[/] → [bold]New[/]\n"
            "     • Type [yellow]C:\\ffmpeg\\bin[/] → OK → OK → OK\n"
            f"     [dim]or run in PowerShell:[/]\n     [yellow]{path_cmd}[/]\n"
            "\n"
            "[bold cyan]Option 3 — No install[/]\n"
            "  Copy [yellow]ffmpeg.exe[/] and [yellow]ffprobe.exe[/] into the same folder as this app.\n"
            "\n"
            "[bold]After installing, close this window and open a new one[/] "
            "[dim](PATH changes only apply to new windows).[/]\n"
            "[dim]Check with:[/] [yellow]ffmpeg -version[/]"
        ))
    else:
        parts.append(Text.from_markup(
            "[bold cyan]Install FFmpeg[/]\n"
            "  macOS : [yellow]brew install ffmpeg[/]\n"
            "  Linux : [yellow]sudo apt install ffmpeg[/]  [dim](or your distro's package manager)[/]\n"
            "\n"
            "Or copy [yellow]ffmpeg[/] and [yellow]ffprobe[/] into the same folder as this app.\n"
            "[dim]Check with:[/] [yellow]ffmpeg -version[/]"
        ))

    console.print(Panel(Group(*parts), title=Text.assemble(("⚠ ", "red"), (f"{APP_NAME} — FFmpeg not found", "bold red")),
                        border_style="red", expand=False, padding=(1, 3)))


def main() -> None:
    global FFMPEG, FFPROBE, AVAILABLE
    if IS_WINDOWS:
        os.system("")  # enable ANSI escape handling in older consoles
    console.set_window_title(APP_NAME)

    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        show_ffmpeg_missing(ffmpeg, ffprobe)
        pause()
        sys.exit(1)
    FFMPEG, FFPROBE = ffmpeg, ffprobe

    with console.status("[bold]Detecting available encoders (CPU / NVIDIA / Intel / AMD)…", spinner="dots12"):
        AVAILABLE = detect_encoders()

    settings = Settings.load()
    notice = ensure_available_encoder(settings)
    if not SETTINGS_FILE.exists():
        settings.save()
    main_menu(settings, notice)
    console.print(f"[{ACCENT}]Bye![/]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print(f"\n[{ACCENT}]Bye![/]")

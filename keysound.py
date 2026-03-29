#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ctypes
import glob
import io
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import wave

_ASSET_VOICES = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets",
    "voices",
)
_MEOW_WAV = "meow.wav"


def _downloads_dir() -> str:
    xdg = os.environ.get("XDG_DOWNLOAD_DIR", "").strip()
    if xdg and os.path.isdir(xdg):
        return os.path.normpath(xdg)
    return os.path.normpath(os.path.expanduser("~/Downloads"))


def resolve_sound_path() -> str:
    """
    Prefer ``Downloads/meow.wav`` (or ``$XDG_DOWNLOAD_DIR/meow.wav``) if present,
    else the bundled ``assets/voices/meow.wav``.
    """
    dl = os.path.join(_downloads_dir(), _MEOW_WAV)
    if os.path.isfile(dl):
        return os.path.normpath(dl)
    bundled = os.path.normpath(os.path.join(_ASSET_VOICES, _MEOW_WAV))
    if os.path.isfile(bundled):
        return bundled
    print(
        f"missing sound file: add {_MEOW_WAV} under Downloads or {bundled}",
        file=sys.stderr,
    )
    sys.exit(2)


_play_warned = False


def _linux_audio_env_for(uid: int) -> dict[str, str]:
    env = os.environ.copy()
    xdg = f"/run/user/{uid}"
    if os.path.isdir(xdg):
        env["XDG_RUNTIME_DIR"] = xdg
    pulse_dir = f"/run/user/{uid}/pulse"
    if os.path.isdir(pulse_dir):
        env["PULSE_RUNTIME_PATH"] = pulse_dir
    pulse = f"/run/user/{uid}/pulse/native"
    if os.path.exists(pulse):
        env["PULSE_SERVER"] = f"unix:{pulse}"
    bus = f"/run/user/{uid}/bus"
    if os.path.exists(bus):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def _linux_ensure_sound_readable(
    path: str, tup: tuple[int, int, dict[str, str]] | None
) -> None:
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    if tup is None:
        return
    uid, gid, _ = tup
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def _linux_invoke_user_for_audio() -> tuple[int, int, dict[str, str]] | None:
    import pwd

    if os.geteuid() != 0:
        return None
    raw = os.environ.get("SUDO_UID")
    if not raw:
        return None
    uid = int(raw)
    if uid == 0:
        return None
    try:
        pw = pwd.getpwuid(uid)
    except KeyError:
        return None
    env = _linux_audio_env_for(uid)
    env["HOME"] = pw.pw_dir
    env["USER"] = pw.pw_name
    env["LOGNAME"] = pw.pw_name
    for k in (
        "SUDO_COMMAND",
        "SUDO_GID",
        "SUDO_UID",
        "SUDO_USER",
    ):
        env.pop(k, None)
    return uid, pw.pw_gid, env


def _try_linux_audio_argv(
    argv: list[str],
    env: dict[str, str],
    tup: tuple[int, int, dict[str, str]] | None,
) -> bool:
    import pwd

    common: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }
    if tup is None:
        try:
            subprocess.Popen(argv, env=env, **common)
            return True
        except OSError:
            return False
    uid, gid, _ = tup
    pw = pwd.getpwuid(uid)
    try:
        subprocess.Popen(
            argv,
            env=env,
            user=uid,
            group=gid,
            **common,
        )
        return True
    except OSError:
        pass
    ru = shutil.which("runuser")
    if ru:
        try:
            subprocess.Popen(
                [ru, "-u", pw.pw_name, "-E", "--", *argv],
                env=env,
                **common,
            )
            return True
        except OSError:
            pass
    return False


def _try_linux_audio_stdin(
    argv: list[str],
    data: bytes,
    env: dict[str, str],
    tup: tuple[int, int, dict[str, str]] | None,
) -> bool:
    import pwd

    common: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
        "stdin": subprocess.PIPE,
    }

    def _write(p: subprocess.Popen) -> None:
        if p.stdin:
            p.stdin.write(data)
            p.stdin.close()

    if tup is None:
        try:
            p = subprocess.Popen(argv, env=env, **common)
            _write(p)
            return True
        except OSError:
            return False
    uid, gid, _ = tup
    pw = pwd.getpwuid(uid)
    try:
        p = subprocess.Popen(
            argv,
            env=env,
            user=uid,
            group=gid,
            **common,
        )
        _write(p)
        return True
    except OSError:
        pass
    ru = shutil.which("runuser")
    if ru:
        try:
            p = subprocess.Popen(
                [ru, "-u", pw.pw_name, "-E", "--", *argv],
                env=env,
                **common,
            )
            _write(p)
            return True
        except OSError:
            pass
    return False


def _wav_pcm_s16_mono(wav_bytes: bytes) -> tuple[bytes, int] | None:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            if wf.getsampwidth() != 2 or wf.getnchannels() != 1:
                return None
            rate = wf.getframerate()
            return wf.readframes(wf.getnframes()), rate
    except (OSError, wave.Error):
        return None


def _linux_audio_argv_candidates(path: str) -> list[list[str]]:
    out: list[list[str]] = []
    for name, tail in (
        ("mpv", ["--no-video", "--really-quiet", path]),
        ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet", path]),
        ("pw-play", [path]),
        ("paplay", [path]),
        ("aplay", ["-q", path]),
        ("aplay", ["-q", "-D", "pulse", path]),
        ("aplay", ["-q", "-D", "default", path]),
    ):
        exe = shutil.which(name)
        if exe:
            out.append([exe] + tail)
    return out


def _alsa_play_wav_file(path: str) -> bool:
    alsa = None
    for soname in ("libasound.so.2", "libasound.so"):
        try:
            alsa = ctypes.CDLL(soname)
            break
        except OSError:
            continue
    if alsa is None:
        return False
    try:
        with wave.open(path, "rb") as wf:
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except (OSError, wave.Error):
        return False
    if sw != 2 or nch not in (1, 2):
        return False
    SND_PCM_STREAM_PLAYBACK = 0
    SND_PCM_FORMAT_S16_LE = 2
    SND_PCM_ACCESS_RW_INTERLEAVED = 3
    alsa.snd_pcm_open.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    alsa.snd_pcm_open.restype = ctypes.c_int
    alsa.snd_pcm_set_params.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    alsa.snd_pcm_set_params.restype = ctypes.c_int
    alsa.snd_pcm_writei.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    alsa.snd_pcm_writei.restype = ctypes.c_long
    alsa.snd_pcm_drain.argtypes = [ctypes.c_void_p]
    alsa.snd_pcm_drain.restype = ctypes.c_int
    alsa.snd_pcm_close.argtypes = [ctypes.c_void_p]
    alsa.snd_pcm_close.restype = ctypes.c_int
    frame_bytes = sw * nch
    total = len(raw) // frame_bytes
    chunk_frames = 512
    for dev in (b"default", b"pipewire", b"pulse", b"plughw:0,0"):
        pcm = ctypes.c_void_p()
        if alsa.snd_pcm_open(ctypes.byref(pcm), dev, SND_PCM_STREAM_PLAYBACK, 0) < 0:
            continue
        err = alsa.snd_pcm_set_params(
            pcm,
            SND_PCM_FORMAT_S16_LE,
            SND_PCM_ACCESS_RW_INTERLEAVED,
            nch,
            rate,
            1,
            500000,
        )
        if err < 0:
            alsa.snd_pcm_close(pcm)
            continue
        off = 0
        ok = True
        while off < total:
            nf = min(chunk_frames, total - off)
            blob = raw[off * frame_bytes : (off + nf) * frame_bytes]
            buf = ctypes.create_string_buffer(blob)
            buf_ptr = ctypes.cast(buf, ctypes.c_void_p)
            wr = alsa.snd_pcm_writei(pcm, buf_ptr, ctypes.c_ulong(nf))
            if wr <= 0:
                ok = False
                break
            off += int(wr)
        if not ok:
            alsa.snd_pcm_close(pcm)
            continue
        alsa.snd_pcm_drain(pcm)
        alsa.snd_pcm_close(pcm)
        return True
    return False


def _linux_play_sound_file(
    path: str,
    tup: tuple[int, int, dict[str, str]] | None,
) -> bool:
    if tup is None:
        env = _linux_audio_env_for(os.getuid())
    else:
        _, _, env = tup

    for argv in _linux_audio_argv_candidates(path):
        if _try_linux_audio_argv(argv, env, tup):
            return True

    if not path.lower().endswith(".wav"):
        return False
    try:
        with open(path, "rb") as f:
            wav_bytes = f.read()
    except OSError:
        wav_bytes = b""

    pcm_rate = _wav_pcm_s16_mono(wav_bytes)
    if pcm_rate:
        pcm, rate = pcm_rate
        paplay = shutil.which("paplay")
        if paplay:
            for paplay_argv in (
                [
                    paplay,
                    "--format=s16le",
                    f"--rate={rate}",
                    "--channels=1",
                    "-",
                ],
                [
                    paplay,
                    "--format",
                    "s16le",
                    "--rate",
                    str(rate),
                    "--channels",
                    "1",
                    "-",
                ],
            ):
                if _try_linux_audio_stdin(paplay_argv, pcm, env, tup):
                    return True
        pacat = shutil.which("pacat")
        if pacat:
            if _try_linux_audio_stdin(
                [
                    pacat,
                    "--format=s16le",
                    f"--rate={rate}",
                    "--channels=1",
                    "-",
                ],
                pcm,
                env,
                tup,
            ):
                return True
        aplay = shutil.which("aplay")
        if aplay:
            for dev in (
                "pulse",
                "default",
                "pipewire",
                "plughw:0,0",
                "hw:0,0",
            ):
                argv = [
                    aplay,
                    "-q",
                    "-D",
                    dev,
                    "-f",
                    "S16_LE",
                    "-c",
                    "1",
                    "-r",
                    str(rate),
                    "-",
                ]
                if _try_linux_audio_stdin(argv, pcm, env, tup):
                    return True

    return _alsa_play_wav_file(path)


def _windows_play_media_path(path: str) -> bool:
    for name, extra in (
        ("mpv", ["--no-video", "--really-quiet"]),
        ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"]),
    ):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            subprocess.Popen(
                [exe, *extra, path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except OSError:
            continue
    return False


def play_sound(sound_path: str) -> None:
    global _play_warned
    if sys.platform == "win32":
        import winsound

        if sound_path.lower().endswith(".wav"):
            try:
                winsound.PlaySound(
                    sound_path,
                    winsound.SND_FILENAME
                    | winsound.SND_ASYNC
                    | winsound.SND_NODEFAULT,
                )
                return
            except OSError:
                pass
        if _windows_play_media_path(sound_path):
            return
        if not _play_warned:
            print(
                "Could not play sound (install mpv or ffplay for non-WAV).",
                file=sys.stderr,
            )
            _play_warned = True
        return

    if sys.platform == "darwin":
        try:
            subprocess.Popen(
                ["afplay", sound_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            if not _play_warned:
                print(
                    "afplay failed; cannot play sound on this Mac.",
                    file=sys.stderr,
                )
                _play_warned = True
        return

    tup = _linux_invoke_user_for_audio()
    _linux_ensure_sound_readable(sound_path, tup)
    if _linux_play_sound_file(sound_path, tup):
        return
    if not _play_warned:
        print("Could not play sound.", file=sys.stderr)
        _play_warned = True


_last_sound_time = 0.0
_lock = threading.Lock()


def maybe_play_sound(sound_path: str, min_interval_sec: float) -> None:
    global _last_sound_time
    now = time.monotonic()
    with _lock:
        if min_interval_sec > 0 and now - _last_sound_time < min_interval_sec:
            return
        _last_sound_time = now
    play_sound(sound_path)


def run_windows(sound_path: str, min_interval_sec: float) -> None:
    user32 = ctypes.windll.user32
    prev = [False] * 256
    print("Keysound is on (global). Ctrl+C to stop.", flush=True)
    while True:
        for vk in range(8, 256):
            down = (user32.GetAsyncKeyState(vk) & 0x8000) != 0
            if down and not prev[vk]:
                maybe_play_sound(sound_path, min_interval_sec)
            prev[vk] = down
        time.sleep(0.002)


EV_KEY = 0x01


def _linux_evdev_paths() -> list[str]:
    paths: set[str] = set()
    for pattern in (
        "/dev/input/event*",
        "/dev/input/by-path/*kbd*",
        "/dev/input/by-path/*-event-kbd",
        "/dev/input/by-id/*kbd*",
    ):
        for p in glob.glob(pattern):
            if os.path.islink(p):
                try:
                    rp = os.path.realpath(p)
                    if rp.startswith("/dev/input/event"):
                        paths.add(rp)
                except OSError:
                    continue
            elif re.match(r".*/event\d+$", p):
                paths.add(p)
    if not paths:
        paths.update(glob.glob("/dev/input/event*"))
    return sorted(paths)


def _open_evdev_fds(only: list[str] | None = None) -> list[int]:
    paths = only if only else _linux_evdev_paths()
    fds: list[int] = []
    for path in paths:
        try:
            fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            continue
    return fds


def _linux_xinput_env() -> dict[str, str] | None:
    exe = shutil.which("xinput")
    if not exe:
        return None
    base = os.environ.copy()
    to_try: list[dict[str, str]] = []
    if base.get("DISPLAY"):
        to_try.append(dict(base))
    for d in (":0", ":1", ":2"):
        e = dict(base)
        e["DISPLAY"] = d
        to_try.append(e)
    seen: set[str] = set()
    for e in to_try:
        disp = e.get("DISPLAY", "")
        if disp in seen:
            continue
        seen.add(disp)
        try:
            r = subprocess.run(
                [exe, "list"],
                env=e,
                capture_output=True,
                timeout=4,
                text=True,
            )
            if r.returncode == 0 and r.stdout.strip():
                return e
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _parse_evdev_chunk(
    data: bytes, sound_path: str, min_interval_sec: float
) -> None:
    off = 0
    n = len(data)
    while off + 24 <= n:
        type_, code, value = struct.unpack_from("HHi", data, off + 16)
        off += 24
        if type_ != EV_KEY or value != 1:
            continue
        maybe_play_sound(sound_path, min_interval_sec)


def run_linux_evdev_loop(
    fds: list[int], sound_path: str, min_interval_sec: float
) -> None:
    print("Keysound is on (global, evdev). Ctrl+C to stop.", flush=True)
    try:
        while True:
            readable, _, _ = select.select(fds, [], [], 0.25)
            for fd in readable:
                try:
                    data = os.read(fd, 24 * 32)
                except OSError:
                    continue
                _parse_evdev_chunk(data, sound_path, min_interval_sec)
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def _linux_xinput_keyboard_id(env: dict[str, str]) -> int | None:
    exe = shutil.which("xinput")
    if not exe:
        return None
    try:
        out = subprocess.check_output([exe, "list"], text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = out.splitlines()

    def _id_from(line: str) -> int | None:
        m = re.search(r"id=(\d+)", line)
        return int(m.group(1)) if m else None

    for line in lines:
        if "master keyboard" in line.lower():
            i = _id_from(line)
            if i is not None:
                return i
    for line in lines:
        low = line.lower()
        if "keyboard" not in low or "xtest" in low:
            continue
        i = _id_from(line)
        if i is not None:
            return i
    for line in lines:
        if "keyboard" in line.lower():
            i = _id_from(line)
            if i is not None:
                return i
    return None


def run_linux_xinput(
    sound_path: str, min_interval_sec: float, env: dict[str, str]
) -> bool:
    kid = _linux_xinput_keyboard_id(env)
    exe = shutil.which("xinput")
    if kid is None or not exe:
        return False
    print("Keysound is on (global, xinput). Ctrl+C to stop.", flush=True)
    try:
        proc = subprocess.Popen(
            [exe, "test", str(kid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )
    except OSError:
        return False
    if proc.stdout is None:
        return False
    press_re = re.compile(r"^\s*key\s+press\s+", re.I)
    try:
        for line in proc.stdout:
            if press_re.match(line):
                maybe_play_sound(sound_path, min_interval_sec)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    return True


def run_linux_global(
    sound_path: str, min_interval_sec: float, evdev_devices: list[str] | None
) -> None:
    fds = _open_evdev_fds(only=evdev_devices)
    if fds:
        run_linux_evdev_loop(fds, sound_path, min_interval_sec)
        return
    xenv = _linux_xinput_env()
    if xenv is not None and run_linux_xinput(sound_path, min_interval_sec, xenv):
        return
    print(
        "Global key capture failed. Try: sudo ./keysound  or  sudo usermod -aG input $USER  "
        "then log out and in.",
        file=sys.stderr,
    )
    sys.exit(1)


def run_macos_cgeventtap(sound_path: str, min_interval_sec: float) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        CG = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/"
            "Versions/Current/CoreGraphics"
        )
        CF = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/"
            "Versions/Current/CoreFoundation"
        )
        kCFRunLoopDefaultMode = ctypes.c_void_p.in_dll(
            CF, "kCFRunLoopDefaultMode"
        )
    except (OSError, ValueError):
        return False

    kCGSessionEventTap = 1
    kCGHeadInsertEventTap = 0
    kCGEventTapOptionDefault = 0
    kCGEventKeyDown = 10

    CGEventTapCallBack = ctypes.CFUNCTYPE(
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )

    def _callback(_proxy, _etype, event, _refcon):
        maybe_play_sound(sound_path, min_interval_sec)
        return event

    cb = CGEventTapCallBack(_callback)

    CG.CGEventTapCreate.restype = ctypes.c_void_p
    CG.CGEventTapCreate.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint64,
        CGEventTapCallBack,
        ctypes.c_void_p,
    ]

    mask = ctypes.c_uint64(1 << kCGEventKeyDown)
    tap = CG.CGEventTapCreate(
        ctypes.c_uint32(kCGSessionEventTap),
        ctypes.c_uint32(kCGHeadInsertEventTap),
        ctypes.c_uint32(kCGEventTapOptionDefault),
        mask,
        cb,
        None,
    )
    if not tap:
        return False

    if hasattr(CG, "CGEventTapEnable"):
        CG.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        CG.CGEventTapEnable.restype = None
        CG.CGEventTapEnable(tap, True)

    CF.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
    CF.CFMachPortCreateRunLoopSource.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_long,
    ]
    CF.CFRunLoopAddSource.restype = None
    CF.CFRunLoopAddSource.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    CF.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    CF.CFRunLoopGetCurrent.argtypes = []
    CF.CFRunLoopGetMain.restype = ctypes.c_void_p
    CF.CFRunLoopGetMain.argtypes = []
    CF.CFRunLoopStop.restype = None
    CF.CFRunLoopStop.argtypes = [ctypes.c_void_p]
    CF.CFRunLoopRun.restype = None
    CF.CFRunLoopRun.argtypes = []

    rl = CF.CFRunLoopGetCurrent()
    src = CF.CFMachPortCreateRunLoopSource(None, tap, 0)
    if not src:
        return False
    CF.CFRunLoopAddSource(rl, src, kCFRunLoopDefaultMode)

    def _stop(_sig: int, _frame: object | None) -> None:
        CF.CFRunLoopStop(CF.CFRunLoopGetMain())

    signal.signal(signal.SIGINT, _stop)

    print(
        "Keysound is on (global). Grant Accessibility if prompted. Ctrl+C to stop.",
        flush=True,
    )
    CF.CFRunLoopRun()
    return True


def run(
    sound_path: str,
    min_interval_sec: float,
    *,
    evdev_devices: list[str] | None = None,
) -> None:
    if sys.platform == "win32":
        try:
            run_windows(sound_path, min_interval_sec)
        except KeyboardInterrupt:
            pass
        return

    if sys.platform == "darwin":
        if run_macos_cgeventtap(sound_path, min_interval_sec):
            return
        print("Global key capture failed.", file=sys.stderr)
        sys.exit(1)

    if sys.platform.startswith("linux"):
        try:
            run_linux_global(sound_path, min_interval_sec, evdev_devices)
        except KeyboardInterrupt:
            pass
        return

    print(
        "Global capture is not implemented for this platform.",
        file=sys.stderr,
    )
    sys.exit(1)


def test_linux_audio(sound_path: str) -> int:
    if not sys.platform.startswith("linux"):
        print("--test-sound is Linux-only.", file=sys.stderr)
        return 2
    tup = _linux_invoke_user_for_audio()
    env = _linux_audio_env_for(os.getuid()) if tup is None else tup[2]
    run_kw: dict = {"env": env, "timeout": 12}
    if tup is not None:
        run_kw["user"] = tup[0]
        run_kw["group"] = tup[1]

    _linux_ensure_sound_readable(sound_path, tup)

    paplay = shutil.which("paplay")
    if paplay:
        try:
            r = subprocess.run([paplay, sound_path], check=False, **run_kw)
        except subprocess.TimeoutExpired:
            print("paplay (file): timed out", file=sys.stderr)
        else:
            if r.returncode == 0:
                print("paplay (file): OK")
                return 0
            print(f"paplay (file): exit {r.returncode}", file=sys.stderr)

    pw_play = shutil.which("pw-play")
    if pw_play:
        try:
            r = subprocess.run([pw_play, sound_path], check=False, **run_kw)
        except subprocess.TimeoutExpired:
            print("pw-play: timed out", file=sys.stderr)
        else:
            if r.returncode == 0:
                print("pw-play (file): OK")
                return 0
            print(f"pw-play: exit {r.returncode}", file=sys.stderr)

    for name, extra in (
        ("mpv", ["--no-video", "--really-quiet"]),
        ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"]),
    ):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            r = subprocess.run([exe, *extra, sound_path], check=False, **run_kw)
        except subprocess.TimeoutExpired:
            print(f"{name} (file): timed out", file=sys.stderr)
            continue
        if r.returncode == 0:
            print(f"{name} (file): OK")
            return 0
        print(f"{name} (file): exit {r.returncode}", file=sys.stderr)

    if not sound_path.lower().endswith(".wav"):
        print("no working audio path (WAV-only fallbacks skipped).", file=sys.stderr)
        return 1

    try:
        with open(sound_path, "rb") as f:
            wav_bytes = f.read()
    except OSError as e:
        print(f"read voice file: {e}", file=sys.stderr)
        return 1
    pr = _wav_pcm_s16_mono(wav_bytes)
    if not pr:
        print("internal wav decode failed.", file=sys.stderr)
        return 1
    pcm, rate = pr
    stdin_kw = {**run_kw, "input": pcm}
    if paplay:
        try:
            r = subprocess.run(
                [
                    paplay,
                    "--format=s16le",
                    f"--rate={rate}",
                    "--channels=1",
                    "-",
                ],
                check=False,
                **stdin_kw,
            )
        except subprocess.TimeoutExpired:
            print("paplay (stdin): timed out", file=sys.stderr)
        else:
            if r.returncode == 0:
                print("paplay (stdin): OK")
                return 0
            print(f"paplay (stdin): exit {r.returncode}", file=sys.stderr)

    aplay = shutil.which("aplay")
    if aplay:
        for dev in ("pulse", "default", "pipewire"):
            try:
                r = subprocess.run(
                    [
                        aplay,
                        "-q",
                        "-D",
                        dev,
                        "-f",
                        "S16_LE",
                        "-c",
                        "1",
                        "-r",
                        str(rate),
                        "-",
                    ],
                    check=False,
                    **stdin_kw,
                )
            except subprocess.TimeoutExpired:
                continue
            if r.returncode == 0:
                print(f"aplay -D {dev} (stdin): OK")
                return 0

    if _alsa_play_wav_file(sound_path):
        print("alsa (libasound): OK")
        return 0

    print("no working audio path.", file=sys.stderr)
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Play a sound on every keypress (global capture).",
        prog="keysound",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=0.0,
        metavar="SEC",
    )
    parser.add_argument(
        "--device",
        action="append",
        default=None,
        metavar="PATH",
    )
    parser.add_argument(
        "--test-sound",
        action="store_true",
    )
    args = parser.parse_args()
    if args.min_interval < 0:
        print("--min-interval must be >= 0", file=sys.stderr)
        sys.exit(2)

    sound_path = resolve_sound_path()
    if args.test_sound:
        sys.exit(test_linux_audio(sound_path))

    try:
        run(sound_path, args.min_interval, evdev_devices=args.device)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

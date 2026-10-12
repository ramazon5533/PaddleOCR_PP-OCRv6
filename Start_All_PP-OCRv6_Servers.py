"""Start the PP-OCRv6 VIN and Barcode test servers together."""

from __future__ import annotations

import ctypes
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
VIN_SERVER = BASE_DIR / "VIN_server_OCR_PP-OCRv6.py"
BARCODE_SERVER = BASE_DIR / "BARCODE_server_OCR_PP-OCRv6.py"

VIN_HOST, VIN_PORT = "127.0.0.1", 65432
BARCODE_HOST, BARCODE_PORT = "127.0.0.1", 65433

# Where result logs (OK/NG history) are written. By default each server
# writes to \result\... on its OWN install drive. Set an explicit path
# here to keep results on a DIFFERENT drive than the program itself
# (e.g. program on C:, results kept on D:). Leave both as "" to go back
# to the automatic same-drive default.
VIN_RESULT_BASE = r"D:\result\vin_result"
BARCODE_RESULT_BASE = r"D:\result\barcode_result"

# Both servers already use the GPU automatically as soon as
# paddlepaddle-gpu is installed in .venv_ppocr370 (see
# requirements_ppocr370_gpu.txt) - roughly 10-50x faster per image in
# testing (an RTX 5070: VIN ~1.5s->~0.2s, Barcode ~2-5s->under 0.3s even
# worst-case). FORCE_DEVICE only needs to be touched to override that
# auto-detection: "" (default) leaves it on auto, "cpu" forces CPU even
# with a GPU installed (e.g. for troubleshooting), "gpu" forces GPU and
# prints a warning instead of silently falling back if none is found.
FORCE_DEVICE = ""


def find_python() -> Path:
    candidates = (
        BASE_DIR.parent / ".venv_ppocr370" / "Scripts" / "python.exe",
        BASE_DIR.parent.parent / ".venv_ppocr370" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        ".venv_ppocr370 Python not found. Expected location(s):\n  "
        + "\n  ".join(str(c) for c in candidates)
    )


def is_listening(host: str, port: int) -> bool:
    """True if something is actively accepting TCP connections on
    host:port. Binding a test socket to check this (with SO_REUSEADDR)
    does NOT work on Windows: SO_REUSEADDR there lets a second socket
    bind successfully over a port another socket is already listening on
    (confirmed in testing - that approach kept reporting the port "free"
    long after VIN was actually listening, so a bind-based wait loop
    never returned), so an actual connect attempt is the only reliable
    way to detect "is this server up" here.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.5)
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def wait_until_listening(
    process: subprocess.Popen, host: str, port: int, label: str, timeout: float = 120.0
) -> None:
    """Block until `process` is listening on host:port (its model finished
    loading and warming up), it exits early (crash), or `timeout` elapses.

    On GPU, starting the VIN and Barcode processes back-to-back used to let
    both begin CUDA/cuDNN initialization at almost the same instant, which
    crashed one or both of them with an access violation inside Paddle's
    own phi.dll (confirmed via Windows Event Viewer - Faulting module:
    phi.dll, Exception code: 0xc0000005). Waiting for the first process to
    finish loading before starting the second avoids that race entirely;
    it only adds a few seconds total since model load is fast, and has no
    effect in CPU mode beyond that same short, harmless wait.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{label} exited during startup. Exit code: {process.returncode}")
        if is_listening(host, port):
            return
        time.sleep(0.5)
    raise RuntimeError(f"{label} did not start listening on {host}:{port} within {timeout:.0f}s")


def validate_environment() -> Path:
    python_exe = find_python()
    missing = [str(path) for path in (VIN_SERVER, BARCODE_SERVER) if not path.is_file()]
    if missing:
        raise FileNotFoundError("Server file not found: " + ", ".join(missing))

    occupied = []
    if is_listening(VIN_HOST, VIN_PORT):
        occupied.append(f"VIN port {VIN_HOST}:{VIN_PORT}")
    if is_listening(BARCODE_HOST, BARCODE_PORT):
        occupied.append(f"Barcode port {BARCODE_HOST}:{BARCODE_PORT}")
    if occupied:
        raise RuntimeError("Port already in use: " + ", ".join(occupied))
    return python_exe


def _detect_performance_core_logical_ids():
    """Same detection used inside the VIN/Barcode servers: return the
    logical CPU indices of the Performance-cores on a hybrid Intel CPU
    (12th/13th/14th gen), or None on a uniform CPU / if detection fails.
    Kept in sync with the copy in VIN_server_OCR_PP-OCRv6.py and
    BARCODE_server_OCR_PP-OCRv6.py.
    """
    if sys.platform != "win32":
        return None
    try:
        RELATION_PROCESSOR_CORE = 0
        kernel32 = ctypes.windll.kernel32
        kernel32.GetLogicalProcessorInformationEx.restype = ctypes.c_bool
        kernel32.GetLogicalProcessorInformationEx.argtypes = [
            ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)
        ]

        buf_len = ctypes.c_ulong(0)
        kernel32.GetLogicalProcessorInformationEx(RELATION_PROCESSOR_CORE, None, ctypes.byref(buf_len))
        if buf_len.value == 0:
            return None

        buf = ctypes.create_string_buffer(buf_len.value)
        if not kernel32.GetLogicalProcessorInformationEx(RELATION_PROCESSOR_CORE, buf, ctypes.byref(buf_len)):
            return None

        data = buf.raw
        efficiency_by_cpu = {}
        offset = 0
        while offset < len(data):
            relationship, size = struct.unpack_from("<II", data, offset)
            if size == 0:
                break
            if relationship == RELATION_PROCESSOR_CORE:
                _, efficiency_class = struct.unpack_from("<BB", data, offset + 8)
                group_count, = struct.unpack_from("<H", data, offset + 30)
                group_offset = offset + 32
                for _ in range(group_count):
                    mask, group = struct.unpack_from("<QH", data, group_offset)
                    for bit in range(64):
                        if mask & (1 << bit):
                            efficiency_by_cpu[group * 64 + bit] = efficiency_class
                    group_offset += 16
            offset += size

        if not efficiency_by_cpu:
            return None
        values = efficiency_by_cpu.values()
        if max(values) == min(values):
            return None
        top_class = max(values)
        return sorted(cpu for cpu, eff in efficiency_by_cpu.items() if eff == top_class)
    except Exception:
        return None


def split_thread_counts() -> tuple[int, int]:
    """Split the CPU budget between the VIN and Barcode processes.

    Both servers used to request round(cpu_count * 0.90) threads
    INDEPENDENTLY, so running them together oversubscribed the CPU by
    ~180%. That contention was the main reason VIN detections blew past
    the 5s limit (confirmed in production logs: VIN Stage1 dropped from
    ~5.5s to ~1.8s the moment concurrent Barcode traffic stopped).
    Splitting one shared budget keeps both processes inside real core
    counts so neither starves the other.

    On a hybrid Intel CPU (P-cores + E-cores), both child processes pin
    themselves to the SAME Performance-cores (see _apply_cpu_affinity in
    the server scripts), so the shared budget must be based on the
    P-core count, not the total logical count which includes the slow
    E-cores that neither process will actually use.
    """
    p_cores = _detect_performance_core_logical_ids()
    effective_cores = len(p_cores) if p_cores else (os.cpu_count() or 2)
    total_threads = max(2, round(effective_cores * 0.90))
    vin_threads = max(1, round(total_threads * 0.55))
    barcode_threads = max(1, total_threads - vin_threads)
    return vin_threads, barcode_threads


def _gpu_will_be_used(python_exe: Path) -> bool:
    """Mirror the servers' own OCR_DEVICE auto-detection (see
    VIN_server_OCR_PP-OCRv6.py / BARCODE_server_OCR_PP-OCRv6.py) so this
    script knows, before launching anything, whether the children will end
    up running on the GPU."""
    requested = (FORCE_DEVICE or "auto").strip().lower()
    if requested == "cpu":
        return False
    venv_dir = python_exe.parent.parent
    nvidia_dir = venv_dir / "Lib" / "site-packages" / "nvidia"
    return nvidia_dir.is_dir() and any(nvidia_dir.glob("*/bin"))


def start_server(
    python_exe: Path, script: Path, host: str, port: int, cpu_threads: int,
    extra_env: dict[str, str] | None = None, own_console: bool = True,
) -> subprocess.Popen:
    environment = os.environ.copy()
    environment["OCR_CPU_THREADS"] = str(cpu_threads)
    environment["PYTHONUTF8"] = "1"
    if extra_env:
        environment.update(extra_env)

    if os.name != "nt":
        creationflags = 0
    elif own_console:
        creationflags = subprocess.CREATE_NEW_CONSOLE
    else:
        # A genuinely separate console/process-group reliably crashes
        # CUDA/cuDNN init on this GPU/driver combo (confirmed via Windows
        # Event Viewer: an 0xC0000005 access violation inside paddle's own
        # phi.dll) - CREATE_NEW_CONSOLE (visible or -WindowStyle Hidden)
        # and even CREATE_NEW_PROCESS_GROUP alone (no new console, just
        # its own process group) all crash it every time. Plain
        # inheritance (flags=0, same as Start-Process -NoNewWindow) fixes
        # that, but does NOT fully eliminate a separate, intermittent
        # crash at CUDA init that still happens some of the time even
        # with a shared console (looks like a lower-level driver
        # race/instability on this bleeding-edge GPU+driver+CUDA13
        # combination, not something fixable from this script) - that
        # residual failure is handled by the retry loop in
        # start_server_with_retry() below. stop_process() below falls
        # back to terminate() instead of CTRL_BREAK_EVENT for a child
        # started this way, since without its own process group that
        # signal would also hit this launcher.
        creationflags = 0

    return subprocess.Popen(
        [str(python_exe), str(script), "--host", host, "--port", str(port)],
        cwd=str(BASE_DIR),
        creationflags=creationflags,
        env=environment,
    )


def stop_process(process: subprocess.Popen | None, own_console: bool = True) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt" and own_console:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=3)
    except Exception:
        process.kill()


def start_server_with_retry(
    python_exe: Path, script: Path, host: str, port: int, cpu_threads: int,
    extra_env: dict[str, str] | None, own_console: bool, label: str, max_attempts: int = 6,
) -> subprocess.Popen:
    """start_server() + wait_until_listening(), retrying the whole launch
    a few times if the process fails during CUDA init (see the GPU
    comment in start_server() - that failure is intermittent even with a
    shared console and not something fixable here; confirmed in testing
    to show up as either the 0xC0000005 crash in phi.dll or a clean
    "InvalidArgumentError: The GPU stream is nullptr" exception, with no
    single cause nailed down, just general instability in this very new
    GPU+driver+CUDA13 combination). It only ever happens during this
    initial startup window though - once a server is listening it has
    been stable every time in testing - so retrying is safe, and 1-3
    attempts was enough to get a clean run every time in testing."""
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        process = start_server(python_exe, script, host, port, cpu_threads, extra_env, own_console)
        try:
            wait_until_listening(process, host, port, label)
            return process
        except RuntimeError as exc:
            last_error = exc
            print(f"[WARN] {label} did not start cleanly (attempt {attempt}/{max_attempts}): {exc}")
            stop_process(process, own_console=own_console)
            if attempt < max_attempts:
                # A crashed CUDA process seems to need a few seconds for
                # the GPU driver to fully release its context/memory
                # before a new one can be created cleanly - retrying
                # near-instantly (the original 1.5s) left some retries
                # failing for the same reason as the attempt before them.
                # Growing the wait each time (4s, 6s, 8s, ...) gives the
                # driver more room to settle on a run that needs several
                # attempts, without slowing down the common case where
                # the very next attempt already succeeds.
                time.sleep(2.0 + attempt * 2.0)
    raise RuntimeError(f"{label} failed to start after {max_attempts} attempts: {last_error}")


def main() -> int:
    try:
        python_exe = validate_environment()
    except Exception as exc:
        print(f"[START ERROR] {exc}")
        input("Click Enter...")
        return 1

    vin_threads, barcode_threads = split_thread_counts()
    vin_process = None
    barcode_process = None
    gpu_active = _gpu_will_be_used(python_exe)

    try:
        vin_env = {"VIN_RESULT_BASE": VIN_RESULT_BASE} if VIN_RESULT_BASE else {}
        barcode_env = {"BARCODE_RESULT_BASE": BARCODE_RESULT_BASE} if BARCODE_RESULT_BASE else {}
        if FORCE_DEVICE:
            vin_env["OCR_DEVICE"] = FORCE_DEVICE
            barcode_env["OCR_DEVICE"] = FORCE_DEVICE

        device_label = FORCE_DEVICE.upper() if FORCE_DEVICE else ("GPU" if gpu_active else "auto")
        if gpu_active:
            print("GPU detected - VIN and Barcode will share THIS console window "
                  "(a separate window per server crashes GPU init on this driver; "
                  "see start_server() for details). Their logs will interleave below.")

        print(f"PP-OCRv6 VIN server is loading: 127.0.0.1:65432 (device={device_label}, threads={vin_threads})")
        # Waits for VIN to finish loading (and retries if it crashes
        # during startup - see start_server_with_retry()) before Barcode
        # is started, so the two never both initialize CUDA at once.
        vin_process = start_server_with_retry(
            python_exe, VIN_SERVER, VIN_HOST, VIN_PORT, vin_threads, vin_env or None,
            not gpu_active, "VIN server",
        )

        print(f"PP-OCRv6 Barcode server is loading: 127.0.0.1:65433 (device={device_label}, threads={barcode_threads})")
        barcode_process = start_server_with_retry(
            python_exe, BARCODE_SERVER, BARCODE_HOST, BARCODE_PORT, barcode_threads, barcode_env or None,
            not gpu_active, "Barcode server",
        )

        print("Both PP-OCRv6 servers are running.")
        print("Press Ctrl+C to stop.")

        while True:
            if vin_process.poll() is not None:
                raise RuntimeError(
                    f"VIN server is closed. Exit code: {vin_process.returncode}"
                )
            if barcode_process.poll() is not None:
                raise RuntimeError(
                    "Barcode server is closed. "
                    f"Exit code: {barcode_process.returncode}"
                )
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nServer is stopping...")
    except Exception as exc:
        print(f"[SERVER ERROR] {exc}")
    finally:
        stop_process(vin_process, own_console=not gpu_active)
        stop_process(barcode_process, own_console=not gpu_active)

    return 0


if __name__ == "__main__":
    sys.exit(main())

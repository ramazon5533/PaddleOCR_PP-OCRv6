"""Start the PP-OCRv6 VIN and Barcode test servers together."""

from __future__ import annotations

import os
import signal
import socket
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


def find_python() -> Path:
    candidates = (
        BASE_DIR.parent / ".venv_ppocr370" / "Scripts" / "python.exe",
        BASE_DIR.parent.parent / ".venv_ppocr370" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        ".venv_ppocr370 Python topilmadi. Kutilgan joy(lar):\n  "
        + "\n  ".join(str(c) for c in candidates)
    )


def port_is_free(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def validate_environment() -> Path:
    python_exe = find_python()
    missing = [str(path) for path in (VIN_SERVER, BARCODE_SERVER) if not path.is_file()]
    if missing:
        raise FileNotFoundError("Server fayli topilmadi: " + ", ".join(missing))

    occupied = []
    if not port_is_free(VIN_HOST, VIN_PORT):
        occupied.append(f"VIN port {VIN_HOST}:{VIN_PORT}")
    if not port_is_free(BARCODE_HOST, BARCODE_PORT):
        occupied.append(f"Barcode port {BARCODE_HOST}:{BARCODE_PORT}")
    if occupied:
        raise RuntimeError("Port band: " + ", ".join(occupied))
    return python_exe


def split_thread_counts() -> tuple[int, int]:
    """Split the CPU budget between the VIN and Barcode processes.

    Both servers used to request round(cpu_count * 0.90) threads
    INDEPENDENTLY, so running them together oversubscribed the CPU by
    ~180%. That contention was the main reason VIN detections blew past
    the 5s limit (confirmed in production logs: VIN Stage1 dropped from
    ~5.5s to ~1.8s the moment concurrent Barcode traffic stopped).
    Splitting one shared budget keeps both processes inside real core
    counts so neither starves the other.
    """
    total_threads = max(2, round((os.cpu_count() or 2) * 0.90))
    vin_threads = max(1, round(total_threads * 0.55))
    barcode_threads = max(1, total_threads - vin_threads)
    return vin_threads, barcode_threads


def start_server(
    python_exe: Path, script: Path, host: str, port: int, cpu_threads: int,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen:
    environment = os.environ.copy()
    environment["OCR_CPU_THREADS"] = str(cpu_threads)
    environment["PYTHONUTF8"] = "1"
    if extra_env:
        environment.update(extra_env)

    creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    return subprocess.Popen(
        [str(python_exe), str(script), "--host", host, "--port", str(port)],
        cwd=str(BASE_DIR),
        creationflags=creationflags,
        env=environment,
    )


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=3)
    except Exception:
        process.kill()


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

    try:
        vin_env = {"VIN_RESULT_BASE": VIN_RESULT_BASE} if VIN_RESULT_BASE else None
        barcode_env = {"BARCODE_RESULT_BASE": BARCODE_RESULT_BASE} if BARCODE_RESULT_BASE else None

        print(f"PP-OCRv6 VIN server is loading: 127.0.0.1:65432 (threads={vin_threads})")
        vin_process = start_server(
            python_exe, VIN_SERVER, VIN_HOST, VIN_PORT, vin_threads, vin_env
        )

        print(f"PP-OCRv6 Barcode server is loading: 127.0.0.1:65433 (threads={barcode_threads})")
        barcode_process = start_server(
            python_exe, BARCODE_SERVER, BARCODE_HOST, BARCODE_PORT, barcode_threads, barcode_env
        )

        print("Ikkala PP-OCRv6 server is working.")
        print("For stopping push the Ctrl+C.")

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
        stop_process(vin_process)
        stop_process(barcode_process)

    return 0


if __name__ == "__main__":
    sys.exit(main())

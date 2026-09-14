"""
TCP Server for OCR Inference - PP-OCRv6
=========================================
Based on V3 (sliding window, weighted deskew, gamma) but:
  - Explicit PP-OCRv6 model
  - SPEED FIX: Stage 2 hard-capped at 3 angles only (max 4 OCR calls total)
  - Guaranteed < 7 seconds per image
"""

# =================================================================
# [START] SILENCER
# =================================================================
import os
import sys
import warnings
warnings.filterwarnings("ignore")
os.environ["CPP_MIN_LOG_LEVEL"] = "3"
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["FLAGS_use_mkldnn"] = "1"
os.environ["FLAGS_enable_pir_in_executor"] = "0"
os.environ["FLAGS_pir_apply_inplace_pass"] = "0"
os.environ["PADDLE_PDX_LOG_LEVEL"] = "ERROR"
os.environ["GLOG_minloglevel"] = "3"
os.environ["GLOG_logtostderr"] = "0"
import logging
logging.getLogger("ppocr").setLevel(logging.ERROR)
logging.getLogger("paddlex").setLevel(logging.ERROR)
logging.getLogger("paddle").setLevel(logging.ERROR)
logging.getLogger("root").setLevel(logging.ERROR)
# =================================================================
# [END] SILENCER
# =================================================================

import time
import json
import socket
import struct
import ctypes
import numpy as np
import cv2
from paddleocr import PaddleOCR
from datetime import datetime
import re

# ===== TUNING FLAGS =====
PROFILE_MODE   = True
DARK_THRESHOLD = 40      # brightness below this → apply gamma
DARK_GAMMA     = 2.5

# Per-machine speed knobs (env-overridable, no code change needed on a
# slower PC). Defaults match the previous hardcoded behavior exactly.
RESIZE_WIDTH = int(os.environ.get("OCR_RESIZE_WIDTH", "800"))
USE_TEXTLINE_ORIENTATION = os.environ.get("VIN_TEXTLINE_ORIENTATION", "0") not in ("0", "false", "False")

# Stage 2: HARD CAP — only 2 angles, confirmed KIA field failure zone
STAGE2_ANGLES = [-15, -25, -33]

# Hard wall-clock cap for the whole fallback chain (plaque crop recovery +
# angle retries). Stage 1 always runs to completion — it is the accurate,
# cheap path and succeeds on the vast majority of frames. Without this cap,
# a hard image could chain up to ~20 plaque OCR calls plus 3 angle calls,
# which is what pushed worst-case NG/edge-case latency well past the
# director's 5s limit.
TIME_BUDGET_SECONDS = 4.3

# Fallback crop windows for small VIN plaques that are readable to a person
# but too small after full-frame resizing. Coordinates are normalized x1,y1,x2,y2.
PLAQUE_REGIONS = [
    ("right_plate", (0.48, 0.25, 0.96, 0.62)),
    ("right_mid",   (0.55, 0.32, 0.95, 0.55)),
    ("center_low",  (0.16, 0.48, 0.60, 0.78)),
    ("wide_mid",    (0.25, 0.30, 0.98, 0.67)),
    ("lower_wide",  (0.10, 0.42, 0.82, 0.82)),
]

# ===== VIN PATTERNS =====
VALID_PREFIXES = ("KN", "KNA", "KNC")
VIN_REGEX = re.compile(r"[A-HJ-NPR-Z0-9]{17}")

# ========== COLOR SYSTEM ==========
class Colors:
    RESET          = '\033[0m'
    BOLD           = '\033[1m'
    RED            = '\033[31m'
    GREEN          = '\033[32m'
    YELLOW         = '\033[33m'
    MAGENTA        = '\033[35m'
    CYAN           = '\033[36m'
    WHITE          = '\033[37m'
    BRIGHT_BLACK   = '\033[90m'
    BRIGHT_RED     = '\033[91m'
    BRIGHT_GREEN   = '\033[92m'
    BRIGHT_YELLOW  = '\033[93m'
    BRIGHT_BLUE    = '\033[94m'
    BRIGHT_MAGENTA = '\033[95m'
    BRIGHT_CYAN    = '\033[96m'
    BRIGHT_WHITE   = '\033[97m'

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except:
        pass

def print_banner():
    print(f"""
{Colors.BRIGHT_CYAN}╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║     {Colors.BRIGHT_YELLOW}🚗  OCR SERVER - PP-OCRv6 MEDIUM MODELS  🚗{Colors.BRIGHT_CYAN}      ║
║                                                              ║
║  {Colors.BRIGHT_WHITE}Det: PP-OCRv6_medium_det{Colors.BRIGHT_CYAN}                             ║
║  {Colors.BRIGHT_WHITE}Rec: PP-OCRv6_medium_rec{Colors.BRIGHT_CYAN}                             ║
║  {Colors.BRIGHT_WHITE}Weighted Deskew  •  Gamma Dark Boost{Colors.BRIGHT_CYAN}                 ║
║  {Colors.BRIGHT_GREEN}Stage 2 = {len(STAGE2_ANGLES)} angles MAX  •  Sliding Window VIN{Colors.BRIGHT_CYAN}    ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝{Colors.RESET}
""")

def log(message, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    icons  = {"INFO": f"{Colors.BRIGHT_BLUE}ℹ{Colors.RESET}", "SUCCESS": f"{Colors.BRIGHT_GREEN}✓{Colors.RESET}",
              "ERROR": f"{Colors.BRIGHT_RED}✗{Colors.RESET}", "WARN": f"{Colors.BRIGHT_YELLOW}⚠{Colors.RESET}",
              "OCR": f"{Colors.BRIGHT_MAGENTA}🔎{Colors.RESET}", "CLIENT": f"{Colors.BRIGHT_CYAN}👤{Colors.RESET}",
              "PIPELINE": f"{Colors.BRIGHT_YELLOW}⚙{Colors.RESET}", "VIN": f"{Colors.BRIGHT_GREEN}🚗{Colors.RESET}",
              "PROFILE": f"{Colors.BRIGHT_MAGENTA}⏱{Colors.RESET}", "MONITOR": f"{Colors.BRIGHT_CYAN}📊{Colors.RESET}"}
    colors = {"INFO": Colors.BRIGHT_WHITE, "SUCCESS": Colors.GREEN, "ERROR": Colors.RED,
              "WARN": Colors.YELLOW, "OCR": Colors.MAGENTA, "CLIENT": Colors.CYAN,
              "PIPELINE": Colors.YELLOW, "VIN": Colors.BRIGHT_GREEN,
              "PROFILE": Colors.BRIGHT_MAGENTA, "MONITOR": Colors.BRIGHT_CYAN}
    print(f"{Colors.BRIGHT_BLACK}[{timestamp}]{Colors.RESET} {icons.get(level,'•')} {colors.get(level,Colors.WHITE)}{message}{Colors.RESET}")

def print_separator(char="─", length=60):
    print(f"{Colors.BRIGHT_BLACK}{char * length}{Colors.RESET}")


# ========== PROFILER ==========
class Profiler:
    def __init__(self, label):
        self.label = label
        self.t0 = 0

    def __enter__(self):
        self.t0 = time.monotonic()
        return self

    def __exit__(self, *args):
        if PROFILE_MODE:
            dt = (time.monotonic() - self.t0) * 1000
            bar_len = min(int(dt / 50), 30)
            bar = "█" * bar_len + "░" * (30 - bar_len)
            color = Colors.BRIGHT_GREEN if dt < 500 else (Colors.BRIGHT_YELLOW if dt < 2000 else Colors.BRIGHT_RED)
            log(f"{self.label:<32} {color}{dt:7.1f}ms{Colors.RESET}  [{bar}]", "PROFILE")


# ========== TCP SERVER ==========
class con:
    def __init__(self, Host="127.0.0.1", Port=65432):
        self.host = Host
        self.port = Port
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen()

    def start_server(self):
        print_separator("═")
        log(f"Listening on {Colors.BRIGHT_YELLOW}{self.host}:{self.port}{Colors.RESET}", "INFO")
        self.client_socket, self.client_address = self.server_socket.accept()
        self.client_socket.settimeout(60.0)
        log(f"Client connected: {Colors.BRIGHT_GREEN}{self.client_address}{Colors.RESET}", "CLIENT")
        print_separator()

    def send(self, message):
        self.client_socket.sendall(message.encode('utf-8'))

    def recv(self, buffer_size=4096):
        return self.client_socket.recv(buffer_size).decode('utf-8')

    def recv_Json(self, buffer_size=4096):
        data = self.recv(buffer_size)
        if not data:
            log("Client disconnected", "CLIENT")
            return False
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            log(f"Invalid JSON: {data[:80]}", "WARN")
            return False

    def recv_all(self, size):
        buf = bytearray(size)
        pos = 0
        while pos < size:
            chunk = self.client_socket.recv(min(size - pos, 65536))
            if not chunk:
                raise ConnectionError("Connection closed before receiving all data")
            buf[pos:pos+len(chunk)] = chunk
            pos += len(chunk)
        return buf

    def send_ok(self, msg):
        self.send(f'{{"{msg}":"OK"}}')
        log(f"OK → {Colors.BRIGHT_GREEN}{msg}{Colors.RESET}", "SUCCESS")

    def send_err(self, msg):
        self.send(f'{{"{msg}":"ERROR"}}')
        log(f"ERROR → {Colors.BRIGHT_RED}{msg}{Colors.RESET}", "ERROR")

    def close(self):
        self.client_socket.close()
        self.server_socket.close()
        log("Server shutdown complete", "WARN")


def _log_cpu_diagnostics(cpu_threads, resize_width, use_textline_orientation):
    """Print the effective per-machine speed settings so a slow PC's
    console log can be compared directly against a fast one (thread
    count alone does not explain cross-machine speed differences)."""
    logical = os.cpu_count() or 0
    try:
        import psutil
        physical = psutil.cpu_count(logical=False) or 0
        affinity = psutil.Process().cpu_affinity()
    except ImportError:
        physical = "N/A (psutil o'rnatilmagan)"
        affinity = "N/A"
    log(f"CPU: logical={logical}  physical={physical}  cpu_threads={cpu_threads}", "MONITOR")
    log(f"RESIZE_WIDTH={resize_width}  USE_TEXTLINE_ORIENTATION={use_textline_orientation}", "MONITOR")
    log(f"Active CPU affinity (cores this process may run on): {affinity}", "MONITOR")


def _detect_performance_core_logical_ids():
    """Return the logical CPU indices belonging to the highest
    EfficiencyClass (Intel Performance-cores on a hybrid 12th/13th/14th
    gen CPU), or None when this machine has no P/E-core split (uniform
    CPU) or the info can't be read.

    Uses the official Windows CPU-set API (GetLogicalProcessorInformationEx)
    so this works on ANY PC the server is installed on, automatically —
    no per-machine tuning. Important: P-core logical ids are NOT a fixed
    contiguous range (e.g. on one tested machine they were
    [0, 1, 10, 11, 12, 13, 22, 23]), so they must always be detected,
    never hardcoded.
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
            return None  # uniform CPU (e.g. most laptops) -> no P/E split
        top_class = max(values)
        return sorted(cpu for cpu, eff in efficiency_by_cpu.items() if eff == top_class)
    except Exception:
        return None


def _apply_cpu_affinity():
    """Keep heavy OpenMP/MKLDNN inference threads off slow Efficiency-
    cores. Fully automatic and machine-independent:
      - Hybrid CPU (P-cores + E-cores present)  -> pinned to P-cores only.
      - Uniform CPU (laptop / older desktop)     -> untouched, all cores
        stay available, exactly like before this change.
    OCR_CPU_AFFINITY can force a specific comma-separated core list
    instead, but this is rarely needed since detection is automatic.

    Returns the number of logical cores actually usable by this process,
    so the CPU-thread default below matches the real available hardware
    on whichever PC the server happens to run on.
    """
    total_logical = os.cpu_count() or 1
    manual = os.environ.get("OCR_CPU_AFFINITY", "").strip()
    try:
        import psutil
        if manual:
            cores = [int(c) for c in manual.split(",") if c.strip() != ""]
            psutil.Process().cpu_affinity(cores)
            log(f"CPU affinity manually pinned to cores: {cores}", "SUCCESS")
            return len(cores)

        p_cores = _detect_performance_core_logical_ids()
        if p_cores:
            psutil.Process().cpu_affinity(p_cores)
            log(f"Hybrid CPU detected (P-core/E-core) - pinned to Performance-cores: {p_cores}", "SUCCESS")
            return len(p_cores)

        log("No hybrid P/E-core split detected - using all CPU cores", "INFO")
        return total_logical
    except Exception as e:
        log(f"CPU affinity auto-detect skipped ({e}) - using all CPU cores", "WARN")
        return total_logical


# ========== OCR MODEL ==========
class OCRModel:
    def __init__(self, effective_cpu_count=None):
        log("Initializing PP-OCRv6 medium...", "INFO")
        t0 = time.monotonic()

        base_count = effective_cpu_count or (os.cpu_count() or 1)
        cpu_threads = max(1, int(os.environ.get(
            "OCR_CPU_THREADS", str(max(1, round(base_count * 0.90)))
        )))
        _log_cpu_diagnostics(cpu_threads, RESIZE_WIDTH, USE_TEXTLINE_ORIENTATION)

        _devnull_fd = os.open(os.devnull, os.O_WRONLY)
        _saved_stdout_fd = os.dup(1)
        _saved_stderr_fd = os.dup(2)
        os.dup2(_devnull_fd, 1)
        os.dup2(_devnull_fd, 2)
        os.close(_devnull_fd)
        sys.stdout = open(os.devnull, 'w')
        sys.stderr = open(os.devnull, 'w')
        try:
            self.ocr = PaddleOCR(
                lang='en',
                ocr_version='PP-OCRv6',
                text_detection_model_name='PP-OCRv6_medium_det',
                text_recognition_model_name='PP-OCRv6_medium_rec',
                use_doc_unwarping=False,
                use_textline_orientation=USE_TEXTLINE_ORIENTATION,
                cpu_threads=cpu_threads,
                enable_mkldnn=True, # CPU acceleration
                device='cpu',
            )
        finally:
            sys.stdout.close()
            sys.stderr.close()
            os.dup2(_saved_stdout_fd, 1)
            os.dup2(_saved_stderr_fd, 2)
            os.close(_saved_stdout_fd)
            os.close(_saved_stderr_fd)
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__

        self.stored_image = None
        log(f"Model loaded in {time.monotonic()-t0:.2f}s", "SUCCESS")

        # Warmup
        log("Running warmup call...", "INFO")
        with Profiler("Warmup OCR call"):
            self.ocr.predict(np.zeros((64, 320, 3), dtype=np.uint8))
        log("Warmup complete — model is hot", "SUCCESS")

    # ========== PREPROCESSING ==========

    def to_gray(self, image):
        if image.ndim == 3:
            return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        return image.copy()

    def gamma_correct(self, gray):
        table = np.array([
            ((i / 255.0) ** (1.0 / DARK_GAMMA)) * 255
            for i in range(256)
        ], dtype=np.uint8)
        return cv2.LUT(gray, table)

    def apply_clahe(self, gray):
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        return clahe.apply(gray)

    def sharpen(self, gray):
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
        return cv2.filter2D(gray, -1, kernel)

    def deskew_weighted(self, gray):
        """Weighted deskew: long lines (VIN bar) dominate over short noise lines."""
        _, bin_img = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 1))
        smeared = cv2.dilate(bin_img, kernel, iterations=1)
        edges = cv2.Canny(smeared, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=50, minLineLength=50, maxLineGap=20)

        if lines is None:
            return gray, 0.0

        weighted_angles = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            angle = np.arctan2(y2 - y1, x2 - x1) * 180 / np.pi
            if abs(angle) < 5.0 or abs(abs(angle) - 180) < 5.0:
                continue
            if abs(abs(angle) - 90) < 5.0:
                continue
            length = np.sqrt((x2 - x1)**2 + (y2 - y1)**2)
            weighted_angles.append((angle, length))

        if not weighted_angles:
            return gray, 0.0

        weighted_angles.sort(key=lambda x: x[0])
        total_weight = sum(w for _, w in weighted_angles)
        cumulative = 0.0
        final_angle = weighted_angles[0][0]
        for angle, weight in weighted_angles:
            cumulative += weight
            if cumulative >= total_weight / 2:
                final_angle = angle
                break

        if abs(final_angle) < 1.0:
            return gray, 0.0

        h, w = gray.shape[:2]
        M = cv2.getRotationMatrix2D((w // 2, h // 2), final_angle, 1.0)
        rotated = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        log(f"Deskewed (weighted): {final_angle:.2f}°", "OCR")
        return rotated, final_angle

    def preprocess(self, image):
        """Full pipeline: gray → gamma(if dark) → deskew → CLAHE → sharpen → BGR"""
        with Profiler("  ToGray + Resize"):
            gray = self.to_gray(image)
            h, w = gray.shape[:2]
            if w > RESIZE_WIDTH:
                ratio = RESIZE_WIDTH / w
                gray = cv2.resize(gray, (RESIZE_WIDTH, int(h * ratio)), interpolation=cv2.INTER_AREA)

        brightness = gray.mean()
        log(f"Brightness: {brightness:.0f}/255", "PIPELINE")

        if brightness < DARK_THRESHOLD:
            with Profiler("  Gamma (dark boost)"):
                gray = self.gamma_correct(gray)
                log(f"Gamma applied (was {brightness:.0f})", "PIPELINE")

        with Profiler("  Deskew (weighted)"):
            gray, angle = self.deskew_weighted(gray)

        with Profiler("  CLAHE"):
            gray = self.apply_clahe(gray)

        with Profiler("  Sharpen"):
            gray = self.sharpen(gray)

        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), angle

    def rotate_gray(self, gray, angle):
        if angle == 90:  return cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)
        if angle == 180: return cv2.rotate(gray, cv2.ROTATE_180)
        if angle in (-90, 270): return cv2.rotate(gray, cv2.ROTATE_90_COUNTERCLOCKWISE)
        h, w = gray.shape[:2]
        M = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
        return cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

    # ========== VIN EXTRACTION (SLIDING WINDOW) ==========

    def clean_text(self, text):
        t = text.strip().upper().replace(" ", "").replace("-", "").replace("|", "I")
        for old, new in {'O': '0', 'I': '1', 'Q': '0'}.items():
            t = t.replace(old, new)
        return t

    def extract_vins(self, result, stage_label, score_threshold=0.15):
        candidates = []
        if not result or not result[0]:
            return candidates

        rec_texts  = result[0].get("rec_texts", [])
        rec_scores = result[0].get("rec_scores", [])
        det_polys  = result[0].get("det_polys", [])

        seen = set()

        for i, (text, score) in enumerate(zip(rec_texts, rec_scores)):
            if score < score_threshold:
                continue
            t = self.clean_text(text)

            # Sliding window: find VIN inside noisy OCR text
            for j in range(max(1, len(t) - 15)):
                sub = t[j:j+17]
                if len(sub) < 17:
                    continue
                if sub.startswith(VALID_PREFIXES) and VIN_REGEX.fullmatch(sub) and sub not in seen:
                    seen.add(sub)
                    position = self._get_position(det_polys, i)
                    candidates.append({"label": sub, "confidence": float(score), "position": position, "stage": stage_label})
                    conf_color = Colors.BRIGHT_GREEN if score > 0.9 else Colors.BRIGHT_YELLOW
                    log(f"VIN ({stage_label}): {Colors.BRIGHT_WHITE}'{sub}'{Colors.RESET} | {conf_color}{score:.3f}{Colors.RESET}", "VIN")

            # Regex findall as extra net
            for m in VIN_REGEX.findall(t):
                if m.startswith(VALID_PREFIXES) and m not in seen:
                    seen.add(m)
                    position = self._get_position(det_polys, i)
                    candidates.append({"label": m, "confidence": float(score), "position": position, "stage": stage_label})
                    conf_color = Colors.BRIGHT_GREEN if score > 0.9 else Colors.BRIGHT_YELLOW
                    log(f"VIN-regex ({stage_label}): {Colors.BRIGHT_WHITE}'{m}'{Colors.RESET} | {conf_color}{score:.3f}{Colors.RESET}", "VIN")

        return candidates

    def _get_position(self, det_polys, idx):
        if idx < len(det_polys):
            poly = det_polys[idx]
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]
        return [0, 0, 0, 0]

    # ========== OCR RUN ==========

    def collect_raw_texts(self, result, score_threshold=0.05):
        """Collect all raw OCR texts (cleaned) for debug display."""
        if not result or not result[0]:
            return []
        rec_texts  = result[0].get("rec_texts", [])
        rec_scores = result[0].get("rec_scores", [])
        raw = []
        for text, score in zip(rec_texts, rec_scores):
            if score >= score_threshold:
                raw.append(self.clean_text(text))
        return raw

    def _run_ocr_on(self, image_bgr, stage_label):
        candidates = []
        raw_texts = []
        try:
            with Profiler(f"PaddleOCR ({stage_label})"):
                result = self.ocr.predict(image_bgr)
            candidates = self.extract_vins(result, stage_label)
            raw_texts = self.collect_raw_texts(result)
        except Exception as e:
            log(f"OCR error ({stage_label}): {e}", "ERROR")
        return candidates, raw_texts

    def _plaque_variants(self, crop_bgr):
        """Upscaled crop variants for low-contrast VIN plaques."""
        gray = self.to_gray(crop_bgr)
        h, w = gray.shape[:2]
        if h <= 0 or w <= 0:
            return []

        variants = []
        width = 900
        scale = width / w
        resized = cv2.resize(
            gray,
            (width, max(32, int(h * scale))),
            interpolation=cv2.INTER_CUBIC
        )
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(resized)
        equalized = cv2.equalizeHist(resized)
        inverted = 255 - clahe

        for name, img in (
            ("raw900", resized),
            ("clahe900", clahe),
            ("eq900", equalized),
            ("inv900", inverted),
        ):
            variants.append((name, cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)))
        return variants

    def plaque_recovery(self, deadline=None):
        """Try targeted plaque crops only after the regular full-frame passes fail."""
        if self.stored_image is None:
            return [], []

        h, w = self.stored_image.shape[:2]
        all_raw_texts = []
        ranked_regions = []

        for region_name, (x1, y1, x2, y2) in PLAQUE_REGIONS:
            crop = self.stored_image[
                int(h * y1):int(h * y2),
                int(w * x1):int(w * x2)
            ]
            if crop.size == 0:
                continue

            # A VIN plaque normally has stronger local contrast than an empty
            # body/interior area. Rank the existing regions with this cheap
            # image statistic so the most likely crop is OCR'd first. Nothing
            # is discarded: if the first crop fails, every original region and
            # preprocessing variant is still tried exactly as before.
            gray_crop = self.to_gray(crop)
            contrast_score = float(gray_crop.std())
            ranked_regions.append((contrast_score, region_name, crop))

        ranked_regions.sort(key=lambda item: item[0], reverse=True)
        order = ", ".join(
            f"{name}:{score:.1f}" for score, name, _ in ranked_regions
        )
        log(f"Plaque priority: {order}", "PIPELINE")

        for _, region_name, crop in ranked_regions:
            for variant_name, variant in self._plaque_variants(crop):
                if deadline is not None and time.monotonic() >= deadline:
                    log("Plaque recovery: time budget reached, stopping early", "WARN")
                    return [], all_raw_texts
                stage = f"Plaque/{region_name}/{variant_name}"
                candidates, raw_texts = self._run_ocr_on(variant, stage)
                all_raw_texts += raw_texts
                if candidates:
                    log(f"Plaque recovery hit: {region_name}/{variant_name}", "SUCCESS")
                    return candidates, all_raw_texts

        return [], all_raw_texts

    # ========== IMAGE STORAGE ==========

    # Follows whichever drive this script is installed on (C:, D:, ...)
    # instead of assuming D:, so results save correctly no matter where
    # OCR_System is deployed. VIN_RESULT_BASE overrides it if ever needed.
    RESULT_BASE = os.environ.get(
        "VIN_RESULT_BASE",
        os.path.join(os.path.splitdrive(os.path.abspath(__file__))[0] + os.sep, "result", "vin_result"),
    )

    def _save_result(self, status, vin, confidence, stage):
        """
        Save detection result to 3 places:
          1. daily/YYYY-MM-DD.txt  -> append one line, see all today at once
          2. OK/ or NG/            -> individual file, all-time accumulation
        """
        try:
            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H:%M:%S")
            ms_str   = now.strftime("%H%M%S_%f")[:-3]

            # --- 1. Daily log (append) ---
            daily_dir = os.path.join(self.RESULT_BASE, "daily")
            os.makedirs(daily_dir, exist_ok=True)
            daily_file = os.path.join(daily_dir, f"{date_str}.txt")
            if status == "OK":
                daily_line = f"[{time_str}] OK  | {vin} | conf:{confidence:.4f} | {stage}\n"
            else:
                daily_line = f"[{time_str}] NG  | NOT DETECTED\n"
            with open(daily_file, "a", encoding="utf-8") as f:
                f.write(daily_line)

            # --- 2. Total OK / NG folder (individual file, all-time) ---
            total_dir = os.path.join(self.RESULT_BASE, status)
            os.makedirs(total_dir, exist_ok=True)
            detail_file = os.path.join(total_dir, f"{ms_str}.txt")
            with open(detail_file, "w", encoding="utf-8") as f:
                f.write(f"Date:   {date_str}\n")
                f.write(f"Time:   {time_str}\n")
                f.write(f"Status: {status}\n")
                if status == "OK":
                    f.write(f"VIN:    {vin}\n")
                    f.write(f"Conf:   {confidence:.4f}\n")
                    f.write(f"Stage:  {stage}\n")
                else:
                    f.write("VIN:    NOT DETECTED\n")

            log(f"Saved [{status}] → daily + {status}/", "SUCCESS")
        except Exception as e:
            log(f"Failed to save result: {e}", "WARN")

    def store_image(self, data):
        if isinstance(data, str):
            self.stored_image = None
            if not os.path.isfile(data):
                log(f"File not found: {data}", "ERROR")
                return False

            self.stored_image = cv2.imread(data, cv2.IMREAD_COLOR)
            if self.stored_image is None:
                try:
                    encoded = np.fromfile(data, dtype=np.uint8)
                    self.stored_image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                except (OSError, ValueError, cv2.error):
                    self.stored_image = None

            if self.stored_image is None or self.stored_image.size == 0:
                self.stored_image = None
                log(f"Could not decode image: {data}", "ERROR")
                return False

            log(f"Image from path: {Colors.BRIGHT_CYAN}{os.path.basename(data)}{Colors.RESET}", "SUCCESS")
            return True
        elif isinstance(data, np.ndarray):
            self.stored_image = data
            h, w = data.shape[:2]
            log(f"Image from binary: {Colors.BRIGHT_CYAN}{w}x{h}px{Colors.RESET}", "SUCCESS")
            return True
        return False

    # ========== MAIN DETECTION ==========

    def detectJSON(self):
        if self.stored_image is None:
            log("No image in memory!", "ERROR")
            return 2, "[]"

        total_timer = time.monotonic()
        deadline = total_timer + TIME_BUDGET_SECONDS
        all_candidates = []

        all_raw_texts = []

        # ===== STAGE 1: Full pipeline =====
        log("━━ Stage 1: Preprocess + OCR ━━", "OCR")
        preprocessed_bgr, deskew_angle = self.preprocess(self.stored_image)
        found1, raw1 = self._run_ocr_on(preprocessed_bgr, "Stage1")
        all_candidates += found1
        all_raw_texts += raw1

        # ===== STAGE 2: Targeted plaque crop fallback =====
        # Try the existing high-yield plaque recovery before whole-frame angles.
        if not all_candidates and time.monotonic() < deadline:
            log("Stage 2: Plaque crop recovery", "OCR")
            found_plaque, raw_plaque = self.plaque_recovery(deadline=deadline)
            all_candidates += found_plaque
            all_raw_texts += raw_plaque

        # ===== STAGE 3: 3 key angles ONLY (speed cap) =====
        if not all_candidates and time.monotonic() < deadline:
            log(f"━━ Stage 2: {len(STAGE2_ANGLES)} key angles {STAGE2_ANGLES} ━━", "OCR")
            gray_preprocessed = cv2.cvtColor(preprocessed_bgr, cv2.COLOR_BGR2GRAY)

            for angle in STAGE2_ANGLES:
                if time.monotonic() >= deadline:
                    log("Angle fallback: time budget reached, stopping early", "WARN")
                    break
                with Profiler(f"  Rotate {angle:+.0f}°"):
                    rotated_gray = self.rotate_gray(gray_preprocessed, angle)
                rotated_bgr = cv2.cvtColor(rotated_gray, cv2.COLOR_GRAY2BGR)
                found2, raw2 = self._run_ocr_on(rotated_bgr, f"S2_{angle:+.0f}°")
                all_raw_texts += raw2
                if found2:
                    all_candidates += found2
                    break  # early exit on first match

        # ===== STAGE 3: Targeted plaque crop fallback =====
        if not all_candidates:
            log("━━ Stage 3: Plaque crop recovery ━━", "OCR")
            # Plaque recovery already ran before the angle fallback.

        # ===== SELECT BEST =====
        total_elapsed = time.monotonic() - total_timer
        print_separator()
        elapsed_color = Colors.BRIGHT_GREEN if total_elapsed < 5 else (Colors.BRIGHT_YELLOW if total_elapsed < 9 else Colors.BRIGHT_RED)
        log(f"Total: {elapsed_color}{total_elapsed:.3f}s{Colors.RESET}", "PROFILE")

        # Build raw text summary for debug (deduplicated, longest first)
        seen_raw = set()
        deduped_raw = []
        for t in sorted(all_raw_texts, key=len, reverse=True):
            if t and t not in seen_raw:
                seen_raw.add(t)
                deduped_raw.append(t)
        raw_summary = " | ".join(deduped_raw[:6])  # top 6 unique texts
        if raw_summary:
            log(f"Raw OCR: {Colors.BRIGHT_CYAN}{raw_summary}{Colors.RESET}", "OCR")

        if all_candidates:
            all_candidates.sort(key=lambda x: x['confidence'], reverse=True)
            best = all_candidates[0]
            log(f"Best VIN: {Colors.BRIGHT_GREEN}{best['label']}{Colors.RESET} | {best['confidence']:.3f} | {best['stage']}", "SUCCESS")
            results_list = [{"label": best['label'], "confidence": best['confidence'],
                             "position": best['position']}]
            self._save_result("OK", best['label'], best['confidence'], best['stage'])
        else:
            log("No valid VIN detected!", "WARN")
            results_list = []
            self._save_result("NG", None, None, None)

        json_data = json.dumps(results_list)
        return len(json_data), json_data


# ========== MAIN ==========
if __name__ == "__main__":
    print_banner()
    effective_cores = _apply_cpu_affinity()

    model = OCRModel(effective_cores)
    server = con()

    reconnect_count = 0

    while True:
        try:
            server.start_server()
            reconnect_count += 1
            if reconnect_count > 1:
                log(f"Reconnected (total: {reconnect_count})", "SUCCESS")

            while True:
                try:
                    cmd = server.recv_Json()
                    if cmd is False:
                        server.client_socket.close()
                        break

                    log(f"Command: {Colors.BRIGHT_YELLOW}{cmd.get('name','?')}{Colors.RESET}", "CLIENT")

                    if cmd["name"] == "file_path":
                        if "path" in cmd and os.path.isfile(cmd["path"]):
                            server.send_ok(cmd["name"]) if model.store_image(cmd["path"]) else server.send_err(cmd["name"])
                        else:
                            log(f"File not found: {cmd.get('path','N/A')}", "ERROR")
                            server.send_err(cmd["name"])

                    elif cmd["name"] == "img_data":
                        if "size" in cmd and isinstance(cmd["size"], int):
                            server.send_ok(cmd["name"])
                            log(f"Receiving {Colors.BRIGHT_CYAN}{cmd['size']}{Colors.RESET} bytes", "INFO")
                            try:
                                buf   = server.recv_all(cmd["size"])
                                image = cv2.imdecode(np.frombuffer(buf, dtype=np.uint8), cv2.IMREAD_COLOR)
                                if image is not None and model.store_image(image):
                                    server.send_ok(cmd["name"])
                                else:
                                    server.send_err(cmd["name"])
                            except Exception as e:
                                log(f"Image receive error: {e}", "ERROR")
                                server.send_err(cmd["name"])
                        else:
                            server.send_err(cmd["name"])

                    elif cmd["name"] == "detect":
                        print_separator("═")
                        json_len, json_data = model.detectJSON()
                        server.send(json_data)
                        log(f"Sent {Colors.BRIGHT_CYAN}{json_len}{Colors.RESET} bytes", "SUCCESS")
                        print_separator("═")

                    elif cmd["name"] == "exit":
                        log("Exit command", "WARN")
                        server.close()
                        raise KeyboardInterrupt

                    else:
                        log(f"Unknown command: {cmd['name']}", "ERROR")
                        server.send_err("cmd")

                except ConnectionResetError:
                    log(f"Client dropped: {server.client_address}", "WARN")
                    break
                except socket.timeout:
                    log("Timeout (60s)", "WARN")
                    break
                except Exception as e:
                    log(f"Command error: {e}", "ERROR")
                    try:
                        server.send_err("cmd")
                    except:
                        break
# Commend out if test, to stop easily using ctrl+c
        # except KeyboardInterrupt:
        #     print()
        #     log("Shutdown (Ctrl+C)", "WARN")
        #     break
        except Exception as e:
            log(f"Server error: {e}", "ERROR")
            time.sleep(1)

    try:
        server.close()
    except:
        pass
    log(f"{Colors.BRIGHT_YELLOW}PP-OCRv6 Server - Goodbye!{Colors.RESET}", "INFO")

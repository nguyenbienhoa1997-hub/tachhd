import os
import re
import sys
import time
import glob
import queue
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import fitz  # PyMuPDF
from PIL import Image
import pytesseract

INVALID_CHARS = r'<>:"/\|?*'

# Matches the dossier code's own distinctive shape (digits/UPPERCASE-with-dash-or-dot),
# e.g. "50063/HDVV-KGALAXY.SAMCH2126005-KSG01" — rather than requiring the "Số:" label
# right before it, since that label itself gets misread in inconsistent ways
# (seen as "SO", "SÔ", "SÉ", ...) depending on the scan batch. The separating "/" is
# matched loosely too — OCR sometimes drops it entirely ("63973HDTC-...").
CODE_PATTERN = re.compile(r"(\d{3,6})[\s/\\|]{0,2}[A-Z]{2,6}[-.]")
RENDER_DPI = 150
ROTATION_CANDIDATES = (0, 90, 180, 270)

# Each document type defines: the region of the page to OCR (as a fraction box),
# and a function that tries to pull a customer name out of the OCR'd text.
DOCUMENT_TYPES = {
    "hop_dong": {
        "label": "Hợp đồng / đề nghị (nhãn + tên ở đầu trang)",
        "crop": (0.0, 0.03, 1.0, 0.65),
        "default": True,
    },
    "phong_toa": {
        "label": "Đề nghị phong tỏa chứng khoán (tên trong đoạn văn)",
        "crop": (0.0, 0.03, 1.0, 0.45),
        "default": False,
    },
}

# Different templates label the customer's name differently on page 1:
#   "ÔNG/BÀ: TÊN"                              (hợp đồng thế chấp/vay vốn)
#   "BÊN NHẬN BẢO ĐẢM ("..."): TÊN"            (đề nghị phong tỏa, mẫu 01K/PT)
# Both are "label (+ optional parenthetical) : name" on one line, so they're
# tried together as the "hop_dong" document type.
NAME_PATTERNS_HOP_DONG = [
    re.compile(
        r"(?:Ô|O)NG\s*/\s*B(?:À|A)(?:\s*/\s*C(?:Ô|O)NG\s*TY)?\s*:\s*(.+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"B[ÊE]N\s+NH[ẬA]N\s+[^:()\n]{2,30}\([^)\n]*\)\s*:\s*(.+)",
        re.IGNORECASE,
    ),
]
NAME_PATTERN_PHONG_TOA = re.compile(
    r"(?:Ô|O)ng\s*/\s*B(?:à|a)\s+(.+?)\s*[\(\"]",
    re.IGNORECASE,
)


def strip_diacritics(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_TESSERACT_PATHS = [
    os.path.join(BASE_DIR, "Tesseract-OCR", "tesseract.exe"),
    r"C:\Users\hoanb02\AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
]
for _p in DEFAULT_TESSERACT_PATHS:
    if os.path.isfile(_p):
        pytesseract.pytesseract.tesseract_cmd = _p
        break


def natural_sort_key(path: str):
    name = os.path.basename(path)
    parts = re.split(r"(\d+)", name)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def normalize_name(name: str) -> str:
    name = strip_diacritics(name.strip().upper())
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def sanitize_filename(name: str) -> str:
    name = name.strip()
    for ch in INVALID_CHARS:
        name = name.replace(ch, "_")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "Khong_ten"


def clean_captured_name(found: str) -> str:
    found = found.strip(" .:;,\"'()")
    found = re.split(r"\s{2,}", found)[0]
    found = re.split(r"[\d_|]", found)[0].strip(" .:;,\"'()-")
    # A stray mark on the page (stamp edge, pen stroke, fold...) sometimes OCRs
    # as one extra bogus single letter tacked onto the end of the name (e.g.
    # "PHẠM THỊ THANH HƯƠNG Ĩ"). Drop it so this doesn't read as a different
    # person than the same name read cleanly on another page.
    words = found.split()
    if len(words) >= 3 and len(words[-1]) <= 1:
        words = words[:-1]
    return " ".join(words)


def extract_name_hop_dong(text: str):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for pattern in NAME_PATTERNS_HOP_DONG:
            m = pattern.search(line)
            if m:
                found = clean_captured_name(m.group(1))
                if found:
                    return found
    return None


def extract_name_phong_toa(text: str):
    flat = text.replace("\n", " ")
    m = NAME_PATTERN_PHONG_TOA.search(flat)
    if m:
        found = clean_captured_name(m.group(1))
        if found:
            return found
    return None


NAME_EXTRACTORS = {
    "hop_dong": extract_name_hop_dong,
    "phong_toa": extract_name_phong_toa,
}


def extract_name_multi(text: str, enabled_types):
    for type_key in ("hop_dong", "phong_toa"):
        if type_key in enabled_types:
            found = NAME_EXTRACTORS[type_key](text)
            if found:
                return found
    return None


def codes_probably_same(a, b):
    """True if two dossier codes are equal or differ by only one OCR-mistaken digit."""
    if a == b:
        return True
    if len(a) != len(b):
        return False
    return sum(1 for x, y in zip(a, b) if x != y) <= 1


def extract_code_from_text(text: str):
    m = CODE_PATTERN.search(strip_diacritics(text).upper())
    if m:
        return m.group(1).strip()
    return None


def union_crop_box(enabled_types):
    boxes = [DOCUMENT_TYPES[t]["crop"] for t in enabled_types if t in DOCUMENT_TYPES]
    if not boxes:
        boxes = [DOCUMENT_TYPES["hop_dong"]["crop"]]
    lefts, tops, rights, bottoms = zip(*boxes)
    return (min(lefts), min(tops), max(rights), max(bottoms))


def render_page_image(page, angle) -> Image.Image:
    page.set_rotation(angle)
    pix = page.get_pixmap(dpi=RENDER_DPI)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def crop_fraction(img: Image.Image, box) -> Image.Image:
    left, top, right, bottom = box
    w, h = img.size
    return img.crop((int(w * left), int(h * top), int(w * right), int(h * bottom)))


def ocr_image(img: Image.Image) -> str:
    return pytesseract.image_to_string(img, lang="vie", config="--psm 6")


def ocr_with_confidence(img: Image.Image):
    """OCR the image once, returning (text, mean_word_confidence).

    Confidence (not just word count) is what reliably tells a correctly
    oriented page apart from an upside-down/sideways one: garbled text from
    the wrong rotation can still contain plenty of short word-shaped
    fragments, but Tesseract's own confidence on them is low.
    """
    data = pytesseract.image_to_data(
        img, lang="vie", config="--psm 6", output_type=pytesseract.Output.DICT
    )
    lines = {}
    confs = []
    for i, word in enumerate(data["text"]):
        word = word.strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        try:
            conf_val = int(float(data["conf"][i]))
        except (ValueError, TypeError):
            conf_val = -1
        if conf_val >= 0:
            confs.append(conf_val)
    text = "\n".join(" ".join(words) for words in lines.values())
    avg_conf = sum(confs) / len(confs) if confs else -1.0
    return text, avg_conf


def detect_rotation_family(page, crop_box):
    """Find which pair of opposite angles (0/180 or 90/270) reads correctly for this page."""
    best_angle, best_conf = 0, -1.0
    for angle in ROTATION_CANDIDATES:
        img = render_page_image(page, angle)
        crop = crop_fraction(img, crop_box)
        _, conf = ocr_with_confidence(crop)
        if conf > best_conf:
            best_conf, best_angle = conf, angle
    return (best_angle, (best_angle + 180) % 360)


class Cancelled(Exception):
    pass


class PdfSplitter:
    def __init__(
        self, file_paths, output_dir, enabled_types, log, progress_cb,
        max_workers=6, code_only=False, create_folders=True, is_cancelled=None,
    ):
        self.file_paths = file_paths
        self.output_dir = output_dir
        self.enabled_types = enabled_types
        self.code_only = code_only
        self.create_folders = create_folders
        self.is_cancelled = is_cancelled or (lambda: False)
        self.crop_box = union_crop_box(enabled_types) if enabled_types else DOCUMENT_TYPES["hop_dong"]["crop"]
        self.log = log
        self.progress_cb = progress_cb
        self.max_workers = max_workers

        self.docs = {}
        self.page_angles = {}  # (file_path, page_index) -> chosen rotation angle
        self.records = []  # [{"file","idx","code","name"}, ...] in scan order, before grouping
        self.groups = []  # list of {"code","name","pages":[(file_path,page_index),...]}, in first-seen order
        self.groups_by_code = {}  # code -> group
        self.current_group = None  # where pages with no code/name of their own get appended
        self.unknown_pages = []
        self.written_paths = []  # every output path actually written, for safe cleanup of old inputs

    def get_doc(self, path):
        if path not in self.docs:
            self.docs[path] = fitz.open(path)
        return self.docs[path]

    def find_group_by_name(self, found_name):
        nf = normalize_name(found_name)
        for g in self.groups:
            if g["name"] and normalize_name(g["name"]) == nf:
                return g
        return None

    def resolve_forward_names(self):
        """A page can show a document's code without its name (the name might
        only show up several pages later, e.g. after a cover/annex page).
        Borrow that upcoming name so the boundary/merge decision for the
        code-only page doesn't wrongly start a fresh group. There's no fixed
        page-count limit — we keep looking as long as every page in between
        either has no code of its own or the same (~) code, and stop the
        moment a genuinely different code shows up, since that's a real
        document boundary."""
        n = len(self.records)
        for i in range(n):
            my_code = self.records[i]["code"]
            # Only pages that already show a code of their own are worth resolving —
            # a page with neither code nor name is just a generic continuation page
            # and should stay attached to whatever group precedes it, not borrow a
            # name from further ahead (which could belong to the next document).
            if self.records[i]["name"] or not my_code:
                continue
            for j in range(i + 1, n):
                other_code = self.records[j]["code"]
                if other_code and not codes_probably_same(other_code, my_code):
                    break
                if self.records[j]["name"]:
                    self.records[i]["name"] = self.records[j]["name"]
                    break

    def assign_page(self, file_path, page_index, found_code, found_name):
        target_group = None
        if found_code:
            target_group = self.groups_by_code.get(found_code)
        if target_group is None and found_name:
            # Same customer can recur far apart in the batch (a mortgage contract and,
            # much later, an unrelated securities-freeze request), and a code can be
            # misread by OCR (e.g. a 9 read as a 0) — matching by name catches both.
            target_group = self.find_group_by_name(found_name)

        if target_group is None and (found_code or found_name):
            target_group = {"code": found_code, "name": found_name, "pages": []}
            self.groups.append(target_group)
            if found_code:
                self.groups_by_code[found_code] = target_group

        if target_group is not None:
            if found_code and not target_group["code"]:
                target_group["code"] = found_code
                self.groups_by_code[found_code] = target_group
            if found_name and not target_group["name"]:
                target_group["name"] = found_name
            target_group["pages"].append((file_path, page_index))
            self.current_group = target_group
        elif self.current_group is not None:
            self.current_group["pages"].append((file_path, page_index))
        else:
            self.unknown_pages.append((file_path, page_index))

    def scan(self):
        total_pages = 0
        page_counts = {}
        for path in self.file_paths:
            doc = self.get_doc(path)
            page_counts[path] = doc.page_count
            total_pages += doc.page_count
        self.log(f"Tổng số trang cần xử lý: {total_pages} (trong {len(self.file_paths)} file)")

        done = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for path in self.file_paths:
                if self.is_cancelled():
                    raise Cancelled()

                doc = self.get_doc(path)
                n = page_counts[path]
                self.log(f"Đang dò góc xoay: {os.path.basename(path)}...")
                family = detect_rotation_family(doc[0], self.crop_box)
                self.log(
                    f"  -> góc xoay dùng cho file này: {family[0]}° hoặc {family[1]}° "
                    f"(tự động chọn theo từng trang)"
                )

                # PyMuPDF is not thread-safe for concurrent rendering on the same
                # Document, so render every candidate crop sequentially first...
                jobs = []  # (page_index, candidate_index, image)
                for i in range(n):
                    for ci, angle in enumerate(family):
                        img = render_page_image(doc[i], angle)
                        jobs.append((i, ci, crop_fraction(img, self.crop_box)))

                # ...then run the actual OCR (external tesseract processes) in parallel,
                # checking for cancellation as results trickle in so a "Hủy" click
                # doesn't have to wait for the whole file to finish OCR-ing.
                def run_ocr(job):
                    i, ci, img = job
                    text, conf = ocr_with_confidence(img)
                    return i, ci, text, conf

                futures = [pool.submit(run_ocr, job) for job in jobs]
                best_per_page = {}
                for future in as_completed(futures):
                    if self.is_cancelled():
                        for f in futures:
                            f.cancel()
                        raise Cancelled()
                    i, ci, text, conf = future.result()
                    current = best_per_page.get(i)
                    if current is None or conf > current[0]:
                        best_per_page[i] = (conf, ci, text)

                for i in range(n):
                    _, ci, text = best_per_page[i]
                    angle = family[ci]
                    self.page_angles[(path, i)] = angle
                    found_code = extract_code_from_text(text)
                    found_name = None if self.code_only else extract_name_multi(text, self.enabled_types)
                    self.records.append({"file": path, "idx": i, "code": found_code, "name": found_name})
                    done += 1
                    if done % 10 == 0 or done == total_pages:
                        self.progress_cb(done, total_pages)
                        self.log(f"Đã xử lý {done}/{total_pages} trang...")

        self.resolve_forward_names()
        for rec in self.records:
            self.assign_page(rec["file"], rec["idx"], rec["code"], rec["name"])

        return total_pages

    def write_outputs(self):
        used_folder_names = {}
        summary_lines = []

        for group in self.groups:
            if self.is_cancelled():
                raise Cancelled()
            if self.code_only:
                label = group["code"] or "Khong_ma"
            else:
                name = group["name"] or "Khong_ten"
                code = group["code"]
                label = f"{code} {name}" if code else name

            base_name = sanitize_filename(label)
            count = used_folder_names.get(base_name, 0)
            used_folder_names[base_name] = count + 1
            display_name = base_name if count == 0 else f"{base_name} ({count + 1})"

            if self.create_folders:
                target_dir = os.path.join(self.output_dir, display_name)
                os.makedirs(target_dir, exist_ok=True)
            else:
                target_dir = self.output_dir

            out_doc = fitz.open()
            for file_path, page_index in group["pages"]:
                src_doc = self.get_doc(file_path)
                angle = self.page_angles.get((file_path, page_index), src_doc[page_index].rotation)
                src_doc[page_index].set_rotation(angle)
                out_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)

            out_path = os.path.join(target_dir, f"{display_name}.pdf")
            out_doc.save(out_path)
            out_doc.close()
            self.written_paths.append(os.path.normcase(os.path.abspath(out_path)))

            summary_lines.append(f"{label}: {len(group['pages'])} trang")
            self.log(f"Đã tạo: {out_path} ({len(group['pages'])} trang)")

        if self.unknown_pages:
            if self.create_folders:
                unknown_dir = os.path.join(self.output_dir, "Khong_xac_dinh")
                os.makedirs(unknown_dir, exist_ok=True)
            else:
                unknown_dir = self.output_dir
            out_doc = fitz.open()
            for file_path, page_index in self.unknown_pages:
                src_doc = self.get_doc(file_path)
                angle = self.page_angles.get((file_path, page_index), src_doc[page_index].rotation)
                src_doc[page_index].set_rotation(angle)
                out_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
            out_path = os.path.join(unknown_dir, "Khong_xac_dinh.pdf")
            out_doc.save(out_path)
            out_doc.close()
            self.written_paths.append(os.path.normcase(os.path.abspath(out_path)))
            summary_lines.append(
                f"Không xác định: {len(self.unknown_pages)} trang (không tìm thấy mã/tên trước trang này)"
            )
            self.log(
                f"Cảnh báo: {len(self.unknown_pages)} trang không tìm thấy mã/tên khách hàng, "
                f"đã lưu tại: {out_path}"
            )

        summary_path = os.path.join(self.output_dir, "BaoCao_TachFile.txt")
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(f"Tổng số khách hàng: {len(self.groups)}\n\n")
            f.write("\n".join(summary_lines))
        self.log(f"Đã ghi báo cáo: {summary_path}")

        for doc in self.docs.values():
            doc.close()

        return len(self.groups)


def find_pdf_files(folder: str):
    files = glob.glob(os.path.join(folder, "*.pdf"))
    files.sort(key=natural_sort_key)
    return files


def guess_parent_output_dir(path: str) -> str:
    """When re-splitting a flagged file, default the output dir one level
    above it if it looks like it lives in its own per-customer folder
    (i.e. "<name>/<name>.pdf") — otherwise the flagged file's own folder."""
    folder = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    if os.path.basename(folder) == stem:
        return os.path.dirname(folder)
    return folder


def clean_pasted_label(line: str) -> str:
    """Strip the "<N> trang" suffix off a pasted report line, e.g.
    "63838 NGUYỄN THỊ PHƯỢNG: 28 trang" -> "63838 NGUYỄN THỊ PHƯỢNG"."""
    line = line.strip().strip("-•*").strip()
    line = re.sub(r"\s*:\s*\d+\s*trang\s*$", "", line, flags=re.IGNORECASE)
    return line.strip()


def resolve_label_to_path(search_dir: str, label: str):
    """Find the actual PDF for a customer label under search_dir, whether it
    was saved flat ("<label>.pdf") or in its own folder ("<label>/<label>.pdf"),
    tolerating the " (2)" suffix write_outputs adds on a name collision."""
    sanitized = sanitize_filename(label)
    candidates = [
        os.path.join(search_dir, f"{sanitized}.pdf"),
        os.path.join(search_dir, sanitized, f"{sanitized}.pdf"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    for pattern in (
        os.path.join(search_dir, f"{sanitized}*.pdf"),
        os.path.join(search_dir, f"{sanitized}*", f"{sanitized}*.pdf"),
    ):
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


APP_TITLE = "Hòa Đã Lấy Vợ"


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("720x620")
        icon_path = os.path.join(BASE_DIR, "app_icon.ico")
        if os.path.isfile(icon_path):
            try:
                root.iconbitmap(icon_path)
            except tk.TclError:
                pass

        self.log_queue: "queue.Queue[str]" = queue.Queue()

        pad = {"padx": 10, "pady": 6}
        frame = ttk.Frame(root)
        frame.pack(fill="x", **pad)

        ttk.Label(frame, text="File PDF scan:").grid(row=0, column=0, sticky="w")
        self.input_var = tk.StringVar()
        self.selected_files = []
        ttk.Entry(frame, textvariable=self.input_var, width=45).grid(row=0, column=1, sticky="we", padx=5)
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=0, column=2)
        ttk.Button(btn_frame, text="Chọn file...", command=self.choose_input_files).pack(side="left")
        ttk.Button(btn_frame, text="Chọn thư mục...", command=self.choose_input_folder).pack(side="left", padx=(4, 0))

        ttk.Label(frame, text="Thư mục lưu kết quả:").grid(row=1, column=0, sticky="w")
        self.output_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.output_var, width=55).grid(row=1, column=1, sticky="we", padx=5)
        ttk.Button(frame, text="Chọn thư mục...", command=self.choose_output).grid(row=1, column=2)

        self.pending_delete_files = []
        ttk.Label(frame, text="Hoặc sửa file sai:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        flagged_btn_frame = ttk.Frame(frame)
        flagged_btn_frame.grid(row=2, column=1, sticky="w", pady=(4, 0))
        ttk.Button(
            flagged_btn_frame, text="Chọn file bị báo sai...", command=self.choose_flagged_files
        ).pack(side="left")
        ttk.Button(
            flagged_btn_frame, text="Dán tên file...", command=self.paste_flagged_names
        ).pack(side="left", padx=(4, 0))

        frame.columnconfigure(1, weight=1)

        type_frame = ttk.LabelFrame(root, text="Loại tài liệu cần nhận diện")
        type_frame.pack(fill="x", padx=10, pady=(0, 6))
        self.type_vars = {}
        self.type_checkbuttons = []
        for key, cfg in DOCUMENT_TYPES.items():
            var = tk.BooleanVar(value=cfg["default"])
            cb = ttk.Checkbutton(type_frame, text=cfg["label"], variable=var)
            cb.pack(anchor="w", padx=8, pady=2)
            self.type_vars[key] = var
            self.type_checkbuttons.append(cb)

        ttk.Separator(type_frame).pack(fill="x", padx=8, pady=4)
        self.code_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            type_frame,
            text="Chỉ tách theo mã HĐ (bỏ qua so khớp tên, dùng khi không chắc mẫu tên)",
            variable=self.code_only_var,
            command=self.on_code_only_toggle,
        ).pack(anchor="w", padx=8, pady=(2, 2))

        self.create_folders_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            type_frame,
            text="Tạo folder riêng cho mỗi khách hàng (bỏ tích = xuất thẳng file PDF, không tạo folder)",
            variable=self.create_folders_var,
        ).pack(anchor="w", padx=8, pady=(2, 6))

        control_frame = ttk.Frame(root)
        control_frame.pack(pady=8)
        self.start_btn = ttk.Button(control_frame, text="Bắt đầu tách", command=self.start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(control_frame, text="Hủy", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.timer_var = tk.StringVar(value="00:00:00")
        ttk.Label(control_frame, textvariable=self.timer_var, font=("Consolas", 11)).pack(side="left", padx=(12, 0))

        progress_frame = ttk.Frame(root)
        progress_frame.pack(fill="x", padx=10, pady=(0, 8))
        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.percent_var = tk.StringVar(value="0%")
        ttk.Label(progress_frame, textvariable=self.percent_var, width=5, anchor="e").pack(side="left", padx=(8, 0))

        self.timer_running = False
        self.start_time = None
        self.cancel_event = threading.Event()

        self.log_box = tk.Text(root, height=20, state="disabled")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.root.after(100, self.poll_log)

    def choose_input_files(self):
        paths = filedialog.askopenfilenames(filetypes=[("PDF files", "*.pdf")])
        if paths:
            files = sorted(paths, key=natural_sort_key)
            self.selected_files = files
            self.pending_delete_files = []
            self.input_var.set(f"{len(files)} file đã chọn")
            if not self.output_var.get():
                self.output_var.set(os.path.join(os.path.dirname(files[0]), "Ket_qua_tach"))

    def choose_input_folder(self):
        path = filedialog.askdirectory()
        if path:
            files = find_pdf_files(path)
            self.selected_files = files
            self.pending_delete_files = []
            self.input_var.set(f"{len(files)} file trong: {path}")
            if not self.output_var.get():
                self.output_var.set(os.path.join(path, "Ket_qua_tach"))

    def choose_flagged_files(self):
        paths = filedialog.askopenfilenames(
            filetypes=[("PDF files", "*.pdf")],
            title="Chọn các file PDF bị báo sai cần tách lại",
        )
        if not paths:
            return
        files = sorted(paths, key=natural_sort_key)
        self.selected_files = files
        self.pending_delete_files = list(files)
        self.input_var.set(f"{len(files)} file BỊ SAI cần tách lại")
        if not self.output_var.get():
            self.output_var.set(guess_parent_output_dir(files[0]))

    def paste_flagged_names(self):
        search_dir = self.output_var.get().strip()
        if not search_dir or not os.path.isdir(search_dir):
            search_dir = filedialog.askdirectory(title="Chọn thư mục chứa các file cần sửa")
            if not search_dir:
                return

        dialog = tk.Toplevel(self.root)
        dialog.title("Dán tên file bị báo sai")
        dialog.transient(self.root)
        ttk.Label(
            dialog,
            text="Dán mỗi tên 1 dòng — có thể dán nguyên dòng báo cáo\n"
                 "(vd: \"63838 NGUYỄN THỊ PHƯỢNG: 28 trang\") hay chỉ cần tên.",
        ).pack(padx=10, pady=(10, 4), anchor="w")
        text_box = tk.Text(dialog, width=60, height=10)
        text_box.pack(padx=10, pady=(0, 10))
        text_box.focus_set()

        def on_ok():
            raw_lines = text_box.get("1.0", "end").splitlines()
            dialog.destroy()
            self.resolve_pasted_names(raw_lines, search_dir)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=(0, 10))
        ttk.Button(btn_frame, text="Tìm & thêm", command=on_ok).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Hủy", command=dialog.destroy).pack(side="left", padx=4)

    def resolve_pasted_names(self, raw_lines, search_dir):
        found, missing = [], []
        for line in raw_lines:
            label = clean_pasted_label(line)
            if not label:
                continue
            path = resolve_label_to_path(search_dir, label)
            if path:
                found.append(path)
            else:
                missing.append(label)

        if not found and not missing:
            return

        if found:
            files = sorted(set(found), key=natural_sort_key)
            self.selected_files = files
            self.pending_delete_files = list(files)
            self.input_var.set(f"{len(files)} file BỊ SAI cần tách lại (dán tên)")

        if missing:
            messagebox.showwarning(
                "Không tìm thấy",
                "Không tìm thấy file cho các tên sau trong thư mục đã chọn:\n" + "\n".join(missing),
            )

    def choose_output(self):
        path = filedialog.askdirectory()
        if path:
            self.output_var.set(path)

    def log(self, message: str):
        self.log_queue.put(message)

    def poll_log(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", message + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_log)

    def set_progress(self, done, total):
        self.root.after(0, lambda: self._set_progress(done, total))

    def _set_progress(self, done, total):
        self.progress["maximum"] = total
        self.progress["value"] = done
        percent = int(done / total * 100) if total else 0
        self.percent_var.set(f"{percent}%")

    def start_timer(self):
        self.start_time = time.time()
        self.timer_running = True
        self.update_timer()

    def stop_timer(self):
        self.timer_running = False

    def update_timer(self):
        if not self.timer_running:
            return
        elapsed = int(time.time() - self.start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        self.timer_var.set(f"{h:02d}:{m:02d}:{s:02d}")
        self.root.after(1000, self.update_timer)

    def on_code_only_toggle(self):
        state = "disabled" if self.code_only_var.get() else "normal"
        for cb in self.type_checkbuttons:
            cb.configure(state=state)

    def start(self):
        if not os.path.isfile(pytesseract.pytesseract.tesseract_cmd or ""):
            messagebox.showerror(
                "Lỗi",
                "Không tìm thấy Tesseract OCR. Vui lòng đảm bảo thư mục 'Tesseract-OCR' "
                "nằm cùng chỗ với chương trình này.",
            )
            return

        output_dir = self.output_var.get().strip()
        code_only = self.code_only_var.get()
        enabled_types = [k for k, v in self.type_vars.items() if v.get()]

        files = self.selected_files
        if not files:
            messagebox.showerror("Lỗi", "Vui lòng chọn file PDF (hoặc thư mục chứa file PDF).")
            return
        if not output_dir:
            messagebox.showerror("Lỗi", "Vui lòng chọn thư mục lưu kết quả.")
            return
        if not code_only and not enabled_types:
            messagebox.showerror("Lỗi", "Vui lòng chọn ít nhất 1 loại tài liệu cần nhận diện.")
            return

        pending_delete = list(self.pending_delete_files)
        if pending_delete:
            ok = messagebox.askyesno(
                "Xác nhận xóa file cũ",
                f"Sau khi tách lại thành công, {len(pending_delete)} file gốc bị báo sai sẽ bị "
                "XÓA VĨNH VIỄN (chỉ những file thực sự được thay bằng file mới). Tiếp tục?",
            )
            if not ok:
                return

        os.makedirs(output_dir, exist_ok=True)

        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.cancel_event.clear()
        self.log(f"Tìm thấy {len(files)} file PDF: " + ", ".join(os.path.basename(f) for f in files))
        self.start_timer()

        create_folders = self.create_folders_var.get()
        thread = threading.Thread(
            target=self.run_split,
            args=(files, output_dir, enabled_types, code_only, create_folders, pending_delete),
            daemon=True,
        )
        thread.start()

    def delete_replaced_files(self, flagged_files, written_paths):
        written_set = set(written_paths)
        removed_dirs = set()
        for path in flagged_files:
            norm = os.path.normcase(os.path.abspath(path))
            if norm in written_set:
                continue  # this exact path was just (re)written — don't delete the fresh file
            try:
                os.remove(path)
                self.log(f"Đã xóa file cũ: {path}")
                removed_dirs.add(os.path.dirname(path))
            except OSError as e:
                self.log(f"Không xóa được {path}: {e}")
        for d in removed_dirs:
            try:
                if not os.listdir(d):
                    os.rmdir(d)
            except OSError:
                pass

    def cancel(self):
        self.cancel_event.set()
        self.cancel_btn.configure(state="disabled")
        self.log("Đang hủy... (chờ xử lý xong trang/nhóm đang dở)")

    def run_split(self, files, output_dir, enabled_types, code_only, create_folders, pending_delete):
        try:
            splitter = PdfSplitter(
                files, output_dir, enabled_types, self.log, self.set_progress,
                code_only=code_only, create_folders=create_folders,
                is_cancelled=self.cancel_event.is_set,
            )
            splitter.scan()
            self.log("Đang ghi các file kết quả...")
            num_customers = splitter.write_outputs()
            self.log(f"Hoàn tất! Đã tách thành {num_customers} khách hàng.")

            if pending_delete:
                self.log(f"Đang dọn {len(pending_delete)} file cũ bị báo sai...")
                self.delete_replaced_files(pending_delete, splitter.written_paths)
                self.pending_delete_files = []

            self.root.after(0, lambda: messagebox.showinfo(
                "Xong", f"Đã tách thành {num_customers} khách hàng.\nLưu tại: {output_dir}"
            ))
        except Cancelled:
            self.log("Đã hủy theo yêu cầu.")
            self.root.after(0, lambda: messagebox.showinfo("Đã hủy", "Đã hủy quá trình tách file."))
        except Exception as e:
            self.log(f"Lỗi: {e}")
            self.root.after(0, lambda: messagebox.showerror("Lỗi", str(e)))
        finally:
            self.root.after(0, self.stop_timer)
            self.root.after(0, lambda: self.start_btn.configure(state="normal"))
            self.root.after(0, lambda: self.cancel_btn.configure(state="disabled"))


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()

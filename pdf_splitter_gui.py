import os
import re
import sys
import glob
import queue
import shutil
import difflib
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image
from pypdf import PdfReader, PdfWriter
import pytesseract

INVALID_CHARS = r'<>:"/\|?*'
HEADER_CROP = (0.0, 0.03, 1.0, 0.28)  # (left, top, right, bottom) as fraction of page size
SIMILARITY_THRESHOLD = 0.75
DEFAULT_LABEL = "ÔNG/BÀ"
CODE_PATTERN = re.compile(r"SO\s*:?\s*(\d{3,})\s*/", re.IGNORECASE)
OUTPUT_ROTATION_FIX = 180  # /Rotate metadata in these scans is off by 180 degrees


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


def is_similar(a_norm: str, b_norm: str) -> bool:
    if not a_norm or not b_norm:
        return False
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio() >= SIMILARITY_THRESHOLD


def sanitize_filename(name: str) -> str:
    name = name.strip()
    for ch in INVALID_CHARS:
        name = name.replace(ch, "_")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "Khong_ten"


def build_name_pattern(label: str) -> re.Pattern:
    if label.strip().upper() == DEFAULT_LABEL:
        return re.compile(
            r"(?:Ô|O)NG\s*/\s*B(?:À|A)(?:\s*/\s*C(?:Ô|O)NG\s*TY)?\s*:\s*(.+)",
            re.IGNORECASE,
        )
    return re.compile(re.escape(label.strip()) + r"\s*:?\s*(.+)", re.IGNORECASE)


def extract_name_from_text(text: str, pattern: re.Pattern):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = pattern.search(line)
        if m:
            found = m.group(1).strip(" .:;,\"'()")
            found = re.split(r"\s{2,}", found)[0]
            found = re.split(r"[\d_|]", found)[0].strip(" .:;,\"'()-")
            if found:
                return found
    return None


def extract_code_from_text(text: str):
    m = CODE_PATTERN.search(strip_diacritics(text).upper())
    if m:
        return m.group(1).strip()
    return None


def get_header_crop(page) -> Image.Image:
    rotate = page.get("/Rotate", 0) or 0
    img = None
    for im in page.images:
        img = im.image
        break
    if img is None:
        raise ValueError("Trang không chứa ảnh (không phải file scan dạng ảnh).")
    img = img.rotate(rotate, expand=True)
    w, h = img.size
    left, top, right, bottom = HEADER_CROP
    return img.crop((int(w * left), int(h * top), int(w * right), int(h * bottom)))


def ocr_image(img: Image.Image) -> str:
    return pytesseract.image_to_string(img, lang="vie", config="--psm 6")


class PdfSplitter:
    def __init__(self, file_paths, output_dir, label, log, progress_cb, max_workers=6):
        self.file_paths = file_paths
        self.output_dir = output_dir
        self.pattern = build_name_pattern(label)
        self.log = log
        self.progress_cb = progress_cb
        self.max_workers = max_workers

        self.readers = {}
        self.groups = {}  # canonical_name -> list of parts; part = list of (file_path, page_index)
        self.codes = {}  # canonical_name -> mã HĐ (dossier code)
        self.order = []
        self.current_canonical = None
        self.current_normalized = None
        self.current_part = []
        self.unknown_pages = []

    def get_reader(self, path):
        if path not in self.readers:
            self.readers[path] = PdfReader(path)
        return self.readers[path]

    def close_current_part(self):
        if self.current_canonical is not None and self.current_part:
            self.groups[self.current_canonical].append(self.current_part)
        self.current_part = []

    def handle_page(self, file_path, page_index, text):
        found_name = extract_name_from_text(text, self.pattern)

        if found_name:
            found_code = extract_code_from_text(text)
            normalized_found = normalize_name(found_name)
            same_customer = self.current_canonical is not None and (
                normalized_found == self.current_normalized
                or is_similar(normalized_found, self.current_normalized)
            )
            self.close_current_part()

            if same_customer:
                if found_code and not self.codes.get(self.current_canonical):
                    self.codes[self.current_canonical] = found_code
            else:
                canonical = found_name.strip()
                if canonical not in self.groups:
                    self.groups[canonical] = []
                    self.order.append(canonical)
                    self.codes[canonical] = found_code
                self.current_canonical = canonical
                self.current_normalized = normalized_found

            self.current_part = [(file_path, page_index)]
        else:
            if self.current_canonical is None:
                self.unknown_pages.append((file_path, page_index))
            else:
                self.current_part.append((file_path, page_index))

    def scan(self):
        total_pages = 0
        page_counts = {}
        for path in self.file_paths:
            reader = self.get_reader(path)
            n = len(reader.pages)
            page_counts[path] = n
            total_pages += n
        self.log(f"Tổng số trang cần xử lý: {total_pages} (trong {len(self.file_paths)} file)")

        done = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for path in self.file_paths:
                reader = self.get_reader(path)
                n = page_counts[path]
                self.log(f"Đang đọc ảnh trang: {os.path.basename(path)} ({n} trang)...")

                crops = []
                for i in range(n):
                    try:
                        crops.append(get_header_crop(reader.pages[i]))
                    except Exception as e:
                        self.log(f"Cảnh báo trang {i + 1} ({os.path.basename(path)}): {e}")
                        crops.append(Image.new("L", (10, 10), 255))

                texts = list(pool.map(ocr_image, crops))

                for i, text in enumerate(texts):
                    self.handle_page(path, i, text)
                    done += 1
                    if done % 10 == 0 or done == total_pages:
                        self.progress_cb(done, total_pages)
                        self.log(f"Đã xử lý {done}/{total_pages} trang...")

        self.close_current_part()
        return total_pages

    def write_outputs(self):
        used_folder_names = {}
        summary_lines = []

        for canonical in self.order:
            parts = self.groups[canonical]
            code = self.codes.get(canonical)
            label = f"{code} {canonical}" if code else canonical

            folder_name = sanitize_filename(label)
            count = used_folder_names.get(folder_name, 0)
            used_folder_names[folder_name] = count + 1
            display_folder = folder_name if count == 0 else f"{folder_name} ({count + 1})"

            customer_dir = os.path.join(self.output_dir, display_folder)
            os.makedirs(customer_dir, exist_ok=True)

            total_customer_pages = sum(len(p) for p in parts)
            summary_lines.append(f"{label}: {len(parts)} tài liệu, {total_customer_pages} trang")

            writer = PdfWriter()
            for part_pages in parts:
                for file_path, page_index in part_pages:
                    reader = self.get_reader(file_path)
                    page = reader.pages[page_index]
                    page.rotate(OUTPUT_ROTATION_FIX)
                    page.transfer_rotation_to_content()
                    writer.add_page(page)

            out_path = os.path.join(customer_dir, f"{display_folder}.pdf")
            with open(out_path, "wb") as f:
                writer.write(f)
            self.log(f"Đã tạo: {out_path} ({total_customer_pages} trang)")

        if self.unknown_pages:
            unknown_dir = os.path.join(self.output_dir, "Khong_xac_dinh")
            os.makedirs(unknown_dir, exist_ok=True)
            writer = PdfWriter()
            for file_path, page_index in self.unknown_pages:
                reader = self.get_reader(file_path)
                page = reader.pages[page_index]
                page.transfer_rotation_to_content()
                writer.add_page(page)
            out_path = os.path.join(unknown_dir, "Khong_xac_dinh.pdf")
            with open(out_path, "wb") as f:
                writer.write(f)
            summary_lines.append(
                f"Không xác định: {len(self.unknown_pages)} trang (không tìm thấy tên trước trang này)"
            )
            self.log(
                f"Cảnh báo: {len(self.unknown_pages)} trang không tìm thấy tên khách hàng, "
                f"đã lưu tại: {out_path}"
            )

        summary_path = os.path.join(self.output_dir, "BaoCao_TachFile.txt")
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(f"Tổng số khách hàng: {len(self.order)}\n\n")
            f.write("\n".join(summary_lines))
        self.log(f"Đã ghi báo cáo: {summary_path}")

        return len(self.order)


def find_pdf_files(folder: str):
    files = glob.glob(os.path.join(folder, "*.pdf"))
    files.sort(key=natural_sort_key)
    return files


class App:
    def __init__(self, root):
        self.root = root
        root.title("Tách PDF theo khách hàng (OCR)")
        root.geometry("720x560")

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

        frame.columnconfigure(1, weight=1)

        self.start_btn = ttk.Button(root, text="Bắt đầu tách", command=self.start)
        self.start_btn.pack(pady=8)

        self.progress = ttk.Progressbar(root, mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=(0, 8))

        self.log_box = tk.Text(root, height=22, state="disabled")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.root.after(100, self.poll_log)

    def choose_input_files(self):
        paths = filedialog.askopenfilenames(filetypes=[("PDF files", "*.pdf")])
        if paths:
            files = sorted(paths, key=natural_sort_key)
            self.selected_files = files
            self.input_var.set(f"{len(files)} file đã chọn")
            if not self.output_var.get():
                self.output_var.set(os.path.join(os.path.dirname(files[0]), "Ket_qua_tach"))

    def choose_input_folder(self):
        path = filedialog.askdirectory()
        if path:
            files = find_pdf_files(path)
            self.selected_files = files
            self.input_var.set(f"{len(files)} file trong: {path}")
            if not self.output_var.get():
                self.output_var.set(os.path.join(path, "Ket_qua_tach"))

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

    def start(self):
        if not os.path.isfile(pytesseract.pytesseract.tesseract_cmd or ""):
            messagebox.showerror(
                "Lỗi",
                "Không tìm thấy Tesseract OCR. Vui lòng đảm bảo thư mục 'Tesseract-OCR' "
                "nằm cùng chỗ với chương trình này.",
            )
            return

        output_dir = self.output_var.get().strip()
        label = DEFAULT_LABEL

        files = self.selected_files
        if not files:
            messagebox.showerror("Lỗi", "Vui lòng chọn file PDF (hoặc thư mục chứa file PDF).")
            return
        if not output_dir:
            messagebox.showerror("Lỗi", "Vui lòng chọn thư mục lưu kết quả.")
            return

        os.makedirs(output_dir, exist_ok=True)

        self.start_btn.configure(state="disabled")
        self.log(f"Tìm thấy {len(files)} file PDF: " + ", ".join(os.path.basename(f) for f in files))

        thread = threading.Thread(
            target=self.run_split, args=(files, output_dir, label), daemon=True
        )
        thread.start()

    def run_split(self, files, output_dir, label):
        try:
            splitter = PdfSplitter(files, output_dir, label, self.log, self.set_progress)
            splitter.scan()
            self.log("Đang ghi các file kết quả...")
            num_customers = splitter.write_outputs()
            self.log(f"Hoàn tất! Đã tách thành {num_customers} khách hàng.")
            self.root.after(0, lambda: messagebox.showinfo(
                "Xong", f"Đã tách thành {num_customers} khách hàng.\nLưu tại: {output_dir}"
            ))
        except Exception as e:
            self.log(f"Lỗi: {e}")
            self.root.after(0, lambda: messagebox.showerror("Lỗi", str(e)))
        finally:
            self.root.after(0, lambda: self.start_btn.configure(state="normal"))


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()

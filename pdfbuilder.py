"""PDF builder — lossless max quality by default, OCR when available.

Designed to run on Render (512MB). Builds from page images on disk.
Splits oversized PDFs into Telegram-uploadable parts.
"""
import os
import shutil
from PIL import Image
from pypdf import PdfReader, PdfWriter

import config


def _ordered_images(folder: str) -> list[str]:
    files = [f for f in os.listdir(folder) if f.endswith((".png", ".jpg"))]
    return sorted(files)


def build_lossless(folder: str, pdf_path: str) -> float:
    """Embed original page images with NO recompression — highest quality."""
    try:
        import img2pdf
    except ImportError:
        return 0.0

    imgs = _ordered_images(folder)
    src_paths = [os.path.join(folder, fn) for fn in imgs]
    if not src_paths:
        return 0.0

    try:
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert(src_paths))
        return os.path.getsize(pdf_path) / 1_048_576
    except Exception:
        # normalize problematic PNGs (palette/alpha) losslessly, then retry
        norm, tmp = [], []
        for p in src_paths:
            try:
                if p.lower().endswith(".jpg"):
                    norm.append(p); continue
                img = Image.open(p)
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                out = p.replace(".png", "_norm.png")
                img.save(out, format="PNG")
                norm.append(out); tmp.append(out)
            except Exception:
                pass
        if not norm:
            return 0.0
        try:
            with open(pdf_path, "wb") as f:
                f.write(img2pdf.convert(norm))
            mb = os.path.getsize(pdf_path) / 1_048_576
        except Exception:
            mb = 0.0
        for t in tmp:
            try: os.remove(t)
            except: pass
        return mb


def _tesseract_ready() -> tuple[bool, list[str]]:
    try:
        import pytesseract
    except ImportError:
        return False, []
    if not shutil.which("tesseract"):
        for guess in ["/usr/bin/tesseract", "/usr/local/bin/tesseract"]:
            if os.path.exists(guess):
                pytesseract.pytesseract.tesseract_cmd = guess
                break
    try:
        pytesseract.get_tesseract_version()
        langs = pytesseract.get_languages(config="")
    except Exception:
        return False, []
    return True, langs


def _resolve_lang(requested: str, available: list[str]) -> str:
    if not available:
        return requested
    wanted = [l for l in requested.split("+") if l]
    usable = [l for l in wanted if l in available]
    if usable:
        return "+".join(usable)
    return "eng" if "eng" in available else (available[0] if available else requested)


def build_ocr(folder: str, pdf_path: str, lang: str) -> tuple[float, bool]:
    """Build searchable PDF. Returns (mb, ocr_succeeded).
    Falls back to lossless build if OCR can't run."""
    import pytesseract
    imgs = _ordered_images(folder)
    page_pdfs, tmp = [], []
    errors = 0

    for fn in imgs:
        fp = os.path.join(folder, fn)
        try:
            img = Image.open(fp)
            if img.mode != "RGB":
                img = img.convert("RGB")
            pdf_bytes = pytesseract.image_to_pdf_or_hocr(img, lang=lang, extension="pdf")
            if not pdf_bytes:
                errors += 1; continue
            out = fp + ".ocr.pdf"
            with open(out, "wb") as f:
                f.write(pdf_bytes)
            page_pdfs.append(out); tmp.append(out)
        except Exception:
            errors += 1

    if not page_pdfs:
        return build_lossless(folder, pdf_path), False

    writer = PdfWriter()
    count = 0
    for p in page_pdfs:
        try:
            reader = PdfReader(p)
            for pg in reader.pages:
                writer.add_page(pg)
            count += 1
        except Exception:
            pass

    if count == 0:
        for p in tmp:
            try: os.remove(p)
            except: pass
        return build_lossless(folder, pdf_path), False

    with open(pdf_path, "wb") as f:
        writer.write(f)
    for p in tmp:
        try: os.remove(p)
        except: pass
    return os.path.getsize(pdf_path) / 1_048_576, True


def build(folder: str, pdf_path: str, do_ocr: bool) -> tuple[float, bool]:
    """Top-level build. Returns (mb, ocr_used)."""
    if do_ocr:
        ok, available = _tesseract_ready()
        if ok:
            lang = _resolve_lang(config.OCR_LANG, available)
            return build_ocr(folder, pdf_path, lang)
        # OCR requested but unavailable -> max quality lossless
        return build_lossless(folder, pdf_path), False
    return build_lossless(folder, pdf_path), False


def split_for_telegram(pdf_path: str) -> list[str]:
    """Split a PDF into <= TG_MAX_MB chunks. Returns list of part paths
    (the original if already small enough)."""
    mb = os.path.getsize(pdf_path) / 1_048_576
    if mb <= config.TG_MAX_MB:
        return [pdf_path]

    base = os.path.splitext(pdf_path)[0]
    reader = PdfReader(pdf_path)
    total = len(reader.pages)
    avg = mb / total
    per_chunk = max(1, int((config.TG_MAX_MB * 0.9) / avg))

    parts, n, start = [], 1, 0
    while start < total:
        end = min(start + per_chunk, total)
        part_path = f"{base}_part{n}.pdf"
        while True:
            writer = PdfWriter()
            for i in range(start, end):
                writer.add_page(reader.pages[i])
            with open(part_path, "wb") as f:
                writer.write(f)
            part_mb = os.path.getsize(part_path) / 1_048_576
            if part_mb <= config.TG_MAX_MB or end - start <= 1:
                break
            end = start + max(1, (end - start) * 9 // 10)
        parts.append(part_path)
        per_chunk = end - start
        start = end
        n += 1
    return parts

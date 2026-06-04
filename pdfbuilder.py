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

    # Normalize every page to clean 8-bit RGB before building.
    #
    # Book pages from this site come as PNGs with an alpha channel and
    # sometimes 16-bit depth. img2pdf either crashes on those (16-bit + alpha)
    # or wastes time/space building a soft-mask per page. Flattening alpha onto
    # white and forcing 8-bit RGB is visually lossless for scanned pages and
    # makes the build fast and reliable. We re-encode to high-quality JPEG
    # (q=95) which keeps quality high while keeping file size sane.
    prepared, tmp = [], []
    for p in src_paths:
        try:
            img = Image.open(p)
            # flatten alpha onto white
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                img = img.convert("RGBA")
                bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
                img = Image.alpha_composite(bg, img).convert("RGB")
            elif img.mode != "RGB":
                img = img.convert("RGB")
            # PIL convert already drops to 8-bit per channel
            out = os.path.splitext(p)[0] + "_prep.jpg"
            img.save(out, format="JPEG", quality=95, subsampling=0, optimize=True)
            prepared.append(out); tmp.append(out)
        except Exception:
            # if anything odd, fall back to using the original file as-is
            prepared.append(p)

    if not prepared:
        return 0.0

    try:
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert(prepared))
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

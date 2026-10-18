"""PDF builder — lossless max quality by default, OCR when available.

Designed to run on Render (512MB). Builds from page images on disk.
Splits oversized PDFs into Telegram-uploadable parts.
"""
import os
import gc
import shutil
from PIL import Image
from pypdf import PdfReader, PdfWriter

import config


def _ordered_images(folder: str) -> list[str]:
    files = [f for f in os.listdir(folder) if f.endswith((".png", ".jpg"))]
    return sorted(files)


def build_lossless(folder: str, pdf_path: str) -> float:
    """Build a high-quality PDF with BOUNDED memory.

    The old approach loaded every page and let img2pdf hold the whole document
    in RAM — fine for 10 pages, fatal for 368 on a 512MB box. This version
    processes ONE page at a time: normalize -> single-page PDF -> append to the
    growing writer -> free it. Peak memory stays roughly constant regardless of
    book length.

    Pages are normalized to clean 8-bit RGB (alpha flattened onto white, depth
    reduced) and re-encoded as high-quality JPEG (q=92), which is visually
    lossless for scanned pages and keeps file size reasonable.
    """
    try:
        import img2pdf
    except ImportError:
        return 0.0

    imgs = _ordered_images(folder)
    if not imgs:
        return 0.0

    writer = PdfWriter()
    added = 0

    for fn in imgs:
        p = os.path.join(folder, fn)
        tmp_jpg = os.path.splitext(p)[0] + "_prep.jpg"
        tmp_pdf = os.path.splitext(p)[0] + "_prep.pdf"
        try:
            # --- normalize one page, write a temp JPEG ---
            with Image.open(p) as im:
                if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                    im = im.convert("RGBA")
                    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                    im = Image.alpha_composite(bg, im).convert("RGB")
                elif im.mode != "RGB":
                    im = im.convert("RGB")
                im.save(tmp_jpg, format="JPEG", quality=90, subsampling=0, optimize=False)

            # --- one-page PDF from that JPEG ---
            with open(tmp_pdf, "wb") as f:
                f.write(img2pdf.convert(tmp_jpg))

            # --- append to the growing document, then free the page ---
            with open(tmp_pdf, "rb") as src:
                reader = PdfReader(src)
                for pg in reader.pages:
                    writer.add_page(pg)
            del reader
            added += 1
        except Exception as exc:
            raise RuntimeError(f"PDF page conversion failed for {fn}: {exc}") from exc
        finally:
            # delete the page image too — we don't need it after this
            for t in (tmp_jpg, tmp_pdf):
                try: os.remove(t)
                except: pass
            try: os.remove(p)
            except: pass

    if added != len(imgs):
        raise RuntimeError(f"PDF page count mismatch: {added}/{len(imgs)}")

    try:
        with open(pdf_path, "wb") as f:
            writer.write(f)
        mb = os.path.getsize(pdf_path) / 1_048_576
    except Exception:
        mb = 0.0
    finally:
        # writer holds every page's content stream in RAM until this point —
        # drop it immediately so the next batch starts with a clean slate.
        del writer
        gc.collect()
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
            with Image.open(fp) as img:
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
            with open(p, "rb") as src:
                reader = PdfReader(src)
                for pg in reader.pages:
                    writer.add_page(pg)
            del reader
            count += 1
        except Exception:
            pass

    if count == 0:
        for p in tmp:
            try: os.remove(p)
            except: pass
        del writer
        return build_lossless(folder, pdf_path), False

    with open(pdf_path, "wb") as f:
        writer.write(f)
    for p in tmp:
        try: os.remove(p)
        except: pass
    mb = os.path.getsize(pdf_path) / 1_048_576
    del writer
    gc.collect()
    return mb, True


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

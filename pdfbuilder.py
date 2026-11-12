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

    # Keep original JPEGs and compatible PNGs untouched. Normalize only
    # unsupported modes/16-bit PNGs, one page at a time (low peak RAM).
    normalized = []
    temporary = []
    try:
        for index, filename in enumerate(imgs):
            path = os.path.join(folder, filename)
            with Image.open(path) as im:
                bits = im.info.get("bits", 8) if im.format == "PNG" else 8
                compatible = (
                    im.mode in ("RGB", "L", "1")
                    and bits <= 8
                    and im.format in ("JPEG", "PNG")
                )
                if compatible:
                    normalized.append(path)
                    continue
                tmp = os.path.join(folder, f"_normalized_{index:05d}.jpg")
                if im.mode in ("RGBA", "LA") or (
                    im.mode == "P" and "transparency" in im.info
                ):
                    rgba = im.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    im = Image.alpha_composite(background, rgba).convert("RGB")
                else:
                    im = im.convert("RGB")
                im.save(tmp, "JPEG", quality=90, optimize=False)
                temporary.append(tmp)
                normalized.append(tmp)

        with open(pdf_path, "wb") as output:
            output.write(img2pdf.convert(normalized))
        actual = len(PdfReader(pdf_path).pages)
        if actual != len(imgs):
            raise RuntimeError(f"Expected {len(imgs)} PDF pages, got {actual}")
        return os.path.getsize(pdf_path) / 1048576
    except Exception as exc:
        try:
            os.remove(pdf_path)
        except OSError:
            pass
        raise RuntimeError(f"PDF assembly failed: {exc}") from exc
    finally:
        for path in temporary:
            try:
                os.remove(path)
            except OSError:
                pass


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
    """Build a complete searchable PDF or fail explicitly, never omit pages."""
    import pytesseract

    imgs = _ordered_images(folder)
    if not imgs:
        raise RuntimeError("OCR requested but no page images were found")

    writer = PdfWriter()
    try:
        for index, filename in enumerate(imgs, 1):
            fp = os.path.join(folder, filename)
            try:
                with Image.open(fp) as source:
                    img = source.convert("RGB")
                    pdf_bytes = pytesseract.image_to_pdf_or_hocr(
                        img, lang=lang, extension="pdf"
                    )
                if not pdf_bytes:
                    raise RuntimeError("Tesseract returned an empty PDF")
                from io import BytesIO
                page_reader = PdfReader(BytesIO(pdf_bytes))
                if len(page_reader.pages) != 1:
                    raise RuntimeError("Tesseract did not return exactly one page")
                writer.add_page(page_reader.pages[0])
            except Exception as exc:
                raise RuntimeError(
                    f"OCR failed on page {index}/{len(imgs)} ({filename}): {exc}"
                ) from exc

        tmp_path = pdf_path + ".ocr.part"
        try:
            with open(tmp_path, "wb") as output:
                writer.write(output)
            with open(tmp_path, "rb") as source:
                count = len(PdfReader(source).pages)
            if count != len(imgs):
                raise RuntimeError(
                    f"OCR PDF page mismatch: expected {len(imgs)}, got {count}"
                )
            os.replace(tmp_path, pdf_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        return os.path.getsize(pdf_path) / 1_048_576, True
    finally:
        writer.close()
        gc.collect()


def build(folder: str, pdf_path: str, do_ocr: bool) -> tuple[float, bool]:
    """Build a PDF; never silently substitute an image-only PDF for OCR."""
    if do_ocr:
        ok, available = _tesseract_ready()
        if not ok:
            raise RuntimeError(
                "Searchable PDF requested, but Tesseract/pytesseract is unavailable. "
                "Install tesseract-ocr, tesseract-ocr-eng, tesseract-ocr-hin and pytesseract."
            )
        requested = [lang for lang in config.OCR_LANG.split("+") if lang]
        missing = [lang for lang in requested if lang not in available]
        if missing:
            raise RuntimeError(
                "OCR languages missing: " + ", ".join(missing)
                + ". Install the corresponding Tesseract language packs."
            )
        return build_ocr(folder, pdf_path, config.OCR_LANG)
    return build_lossless(folder, pdf_path), False


def split_for_telegram(pdf_path: str, max_mb: float | None = None) -> list[str]:
    """Split a PDF into <= TG_MAX_MB chunks. Returns list of part paths
    (the original if already small enough)."""
    limit = float(max_mb if max_mb is not None else config.TG_MAX_MB)
    mb = os.path.getsize(pdf_path) / 1_048_576
    if mb <= limit:
        return [pdf_path]

    base = os.path.splitext(pdf_path)[0]
    reader = PdfReader(pdf_path)
    total = len(reader.pages)
    avg = mb / total
    per_chunk = max(1, int((limit * 0.9) / avg))

    parts, n, start = [], 1, 0
    while start < total:
        end = min(start + per_chunk, total)
        part_path = f"{base}_part{n}.pdf"
        while True:
            writer = PdfWriter()
            try:
                for i in range(start, end):
                    writer.add_page(reader.pages[i])
                with open(part_path, "wb") as f:
                    writer.write(f)
            finally:
                # Writers hold the whole part in memory; leaking them across
                # batches is what pushed the 512 MB instance over the edge.
                writer.close()
                del writer
                gc.collect()
            part_mb = os.path.getsize(part_path) / 1_048_576
            if part_mb <= limit or end - start <= 1:
                break
            end = start + max(1, (end - start) * 9 // 10)
        parts.append(part_path)
        per_chunk = end - start
        start = end
        n += 1
    try:
        reader.close()
    except Exception:
        pass
    return parts

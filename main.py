"""Oath of Care OCR service — document text assistance, not verification."""
from __future__ import annotations

import hmac
import io
import os
import re
from contextlib import asynccontextmanager

import cv2
import numpy as np
import pypdfium2 as pdfium
import pytesseract
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image, ImageOps, UnidentifiedImageError

# Conservative bounds: OCR is CPU-intensive and documents may contain personal data.
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 18_000_000
MAX_PDF_PAGES = 4
MAX_RENDER_PIXELS = 9_000_000
MAX_TEXT_CHARS = 20_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def authorize(header: str | None) -> None:
    token = os.environ.get("OCR_API_TOKEN", "")
    if not token or len(token) < 32:
        # Fail securely even if a misconfigured worker is somehow serving requests.
        raise HTTPException(503, "OCR service not configured")
    scheme, sep, supplied = (header or "").partition(" ")
    if not sep or scheme.lower() != "bearer" or not hmac.compare_digest(supplied, token):
        raise HTTPException(401, "Unauthorized")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Fail deployment rather than accepting files with a missing/weak secret.
    token = os.environ.get("OCR_API_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("OCR_API_TOKEN must be configured with at least 32 characters")
    yield


app = FastAPI(title="Oath of Care OCR", version="1.0.0", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


def detect_type(data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return "pdf"
    try:
        with Image.open(io.BytesIO(data)) as probe:
            kind = probe.format
            probe.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, "Invalid or unsupported document")
    if kind not in ("JPEG", "PNG", "WEBP"):
        raise HTTPException(415, "Only PDF, JPG, PNG and WebP are supported")
    return "image"


def image_quality(image: Image.Image) -> dict:
    w, h = image.size
    if not w or not h or w * h > MAX_IMAGE_PIXELS:
        raise HTTPException(413, "Image dimensions exceed processing limit")
    gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    # Sample huge photos for bounded quality computation.
    if max(w, h) > 2400:
        scale = 2400 / max(w, h)
        gray = cv2.resize(gray, (max(1, int(w * scale)), max(1, int(h * scale))))
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    warnings: list[str] = []
    if min(w, h) < 650:
        warnings.append("Low resolution; some text may not be readable")
    if sharpness < 55:
        warnings.append("Potential blur; review readability manually")
    if brightness < 50:
        warnings.append("Image may be underexposed")
    if brightness > 220:
        warnings.append("Image may be overexposed")
    return {"width": w, "height": h, "sharpness_score": round(sharpness, 1), "brightness_score": round(brightness, 1), "warnings": warnings}


def render_images(data: bytes, kind: str) -> list[Image.Image]:
    if kind == "image":
        try:
            with Image.open(io.BytesIO(data)) as source:
                img = ImageOps.exif_transpose(source).convert("RGB")
            if img.width * img.height > MAX_IMAGE_PIXELS:
                raise HTTPException(413, "Image dimensions exceed processing limit")
            return [img]
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            raise HTTPException(422, "Unable to read image")
    try:
        document = pdfium.PdfDocument(data)
    except Exception:
        raise HTTPException(422, "Unable to read PDF")
    try:
        count = len(document)
        if count == 0 or count > MAX_PDF_PAGES:
            raise HTTPException(413, f"PDF must contain 1–{MAX_PDF_PAGES} pages")
        images: list[Image.Image] = []
        for i in range(count):
            page = document[i]
            try:
                width, height = page.get_size()
                if width <= 0 or height <= 0:
                    raise HTTPException(422, "Invalid PDF page dimensions")
                scale = min(2.0, (MAX_RENDER_PIXELS / (width * height)) ** 0.5)
                bitmap = page.render(scale=scale)
                try:
                    images.append(bitmap.to_pil().convert("RGB"))
                finally:
                    bitmap.close()
            finally:
                page.close()
        return images
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "Unable to render PDF pages")
    finally:
        document.close()


def candidate_fields(text: str) -> dict:
    # Candidates only, not authoritative matching or legal verification.
    patterns = {
        "tin": r"\b(?:TIN|Tax\s+Identification(?:\s+Number)?)\s*[:#-]?\s*([0-9-]{9,20})",
        "permit_number": r"\b(?:Permit|License|LTO)\s*(?:No\.?|Number|#)\s*[:#-]?\s*([A-Z0-9][A-Z0-9 /-]{3,30})",
        "registration_number": r"\bRegistration\s*(?:No\.?|Number|#)\s*[:#-]?\s*([A-Z0-9][A-Z0-9 /-]{3,30})",
    }
    found = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, text, re.I)
        if match:
            found[name] = {"value": match.group(1).strip(), "requires_confirmation": True, "source": "ocr_candidate"}
    return found


@app.post("/v1/analyze")
async def analyze(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    authorize(authorization)  # Authenticate before reading upload bytes.
    data = await file.read(MAX_FILE_BYTES + 1)
    await file.close()
    if not data:
        raise HTTPException(422, "Empty file")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "File exceeds 10 MB limit")
    kind = detect_type(data)
    images = render_images(data, kind)
    pages = []
    snippets = []
    for index, image in enumerate(images):
        quality = image_quality(image)
        try:
            recognized = pytesseract.image_to_string(image, lang="eng", config="--psm 3", timeout=18)
        except RuntimeError:
            raise HTTPException(422, "OCR processing timed out or failed")
        if len(recognized.strip()) < 25:
            quality["warnings"].append("Very little readable text; manual review needed")
        pages.append({"page": index + 1, "quality": quality})
        snippets.append(recognized)
        image.close()
    text = "\n\n".join(snippets)[:MAX_TEXT_CHARS]
    return JSONResponse(
        {
            "document_kind": kind,
            "page_count": len(pages),
            "extracted_text": text,
            "candidates": candidate_fields(text),
            "pages": pages,
            "requires_manual_confirmation": True,
            "authenticity_verified": False,
            "disclaimer": "OCR and image-quality checks do not establish document authenticity; owner confirmation and authorized admin review are required.",
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )

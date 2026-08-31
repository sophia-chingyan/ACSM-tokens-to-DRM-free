#!/usr/bin/env python3
"""
ACSM to DRM-free EPUB/PDF Converter

Converts Adobe ACSM ebook tokens to DRM-free EPUB or PDF files
for personal offline reading.

How it works:
    1. adept_activate registers an anonymous Adobe device
    2. acsmdownloader fulfills the ACSM token -> encrypted EPUB/PDF
    3. adept_remove decrypts the file in-place

Content preservation:
    adept_remove operates at the encryption layer only. It does NOT
    re-encode, transcode, re-render or otherwise transform content.

    For EPUB it works at the ZIP level: each encrypted entry (XHTML,
    images, CSS, fonts) is decrypted and encryption.xml is removed.
    All images, links, paragraph structure, writing modes (horizontal,
    vertical) and CJK text (Traditional/Simplified Chinese, Japanese,
    Korean) are retained exactly as the publisher created them.

    For PDF the document is decrypted, so all images, text, paragraph
    structure, fonts, links, bookmarks and annotations are preserved.

Prerequisites (Docker handles these):
    libgourou (built from source)
    pip install PyMuPDF pypdf     (PDF verification only)
"""

import argparse
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LIBGOUROU_DIR = SCRIPT_DIR / "libgourou"
LIBGOUROU_BIN = LIBGOUROU_DIR / "utils"

# All mutable state lives under DATA_DIR so a single mounted volume
# (one Railway volume at /app/data) persists books *and* the Adobe
# device registration across redeploys.
DATA_DIR = Path(os.environ.get("DATA_DIR", SCRIPT_DIR / "data"))

# ADEPT credential directory.
# libgourou v0.8.1+ reads this from the $ADEPT_DIR environment variable.
# We set it explicitly so all tools (adept_activate, acsmdownloader,
# adept_remove) use the same path without needing per-tool flags.
ADEPT_DIR = Path(os.environ.get("ADEPT_DIR", DATA_DIR / ".adept"))

SUPPORTED_FORMATS = ("epub", "pdf")


def _set_adept_env():
    """Ensure $ADEPT_DIR is set for all libgourou subprocess calls."""
    os.environ["ADEPT_DIR"] = str(ADEPT_DIR)


def run(cmd, **kwargs):
    """Run a command and return the result."""
    _set_adept_env()
    defaults = {"capture_output": True, "text": True}
    defaults.update(kwargs)
    return subprocess.run(cmd, **defaults)


def find_tool(name):
    """Find a tool, checking local build directory first."""
    local = LIBGOUROU_BIN / name
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    system = shutil.which(name)
    if system:
        return system
    return None


# --- Conversion ------------------------------------------------------------


def detect_format(acsm_path):
    """Parse the ACSM file to detect if the download is EPUB or PDF."""
    tree = ET.parse(acsm_path)
    root = tree.getroot()
    ns = {"adept": "http://ns.adobe.com/adept"}

    src_elem = root.find(".//adept:src", ns)
    if src_elem is not None and src_elem.text:
        src = src_elem.text.lower()
        if ".pdf" in src or "output=pdf" in src:
            return "pdf"
        if ".epub" in src or "output=epub" in src:
            return "epub"

    # Also check metadata/resourceItemInfo/resource
    resource_elem = root.find(".//adept:resource", ns)
    if resource_elem is not None and resource_elem.text:
        res = resource_elem.text.lower()
        if ".pdf" in res:
            return "pdf"
        if ".epub" in res:
            return "epub"

    # Check metadata format element
    for meta in root.iter():
        tag = meta.tag.split("}")[-1] if "}" in meta.tag else meta.tag
        if tag == "format":
            fmt_text = (meta.text or "").lower()
            if "pdf" in fmt_text:
                return "pdf"
            if "epub" in fmt_text:
                return "epub"

    return "epub"


def register_device():
    """Register an Adobe device (one-time setup).

    Uses $ADEPT_DIR env var so all libgourou tools share the same
    credential directory automatically.
    """
    device_file = ADEPT_DIR / "device.xml"
    activation_file = ADEPT_DIR / "activation.xml"

    if device_file.exists() and activation_file.exists():
        print("[OK] Adobe device already registered.", flush=True)
        return

    print("Registering Adobe device (anonymous)...", flush=True)
    tool = find_tool("adept_activate")
    if not tool:
        raise RuntimeError("adept_activate not found. libgourou not built.")

    # Remove any partial/stale ADEPT directory so adept_activate
    # can create it fresh (it requires the output dir to NOT exist
    # when using --output-dir).
    if ADEPT_DIR.exists():
        shutil.rmtree(ADEPT_DIR)
    ADEPT_DIR.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        tool,
        "--anonymous",
        "--random-serial",
        "--output-dir", str(ADEPT_DIR),
    ]
    print(f"[DEBUG] Running: {' '.join(cmd)}", flush=True)
    print(f"[DEBUG] ADEPT_DIR={os.environ.get('ADEPT_DIR', 'NOT SET')}", flush=True)

    try:
        result = run(cmd, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Device registration timed out (60s). "
            "Adobe's activation server may be unreachable from this host."
        )

    stdout = result.stdout.strip() if result.stdout else ""
    stderr = result.stderr.strip() if result.stderr else ""
    print(f"[DEBUG] adept_activate exit={result.returncode}", flush=True)
    if stdout:
        print(f"[DEBUG] stdout: {stdout}", flush=True)
    if stderr:
        print(f"[DEBUG] stderr: {stderr}", flush=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"Device registration failed (exit {result.returncode}): "
            f"{stderr or stdout}"
        )

    # Check both possible output locations
    if not device_file.exists():
        # adept_activate might have written to ~/.config/adept instead
        home_adept = Path.home() / ".config" / "adept"
        home_device = home_adept / "device.xml"
        if home_device.exists():
            print(f"[DEBUG] Found credentials at {home_adept}, "
                  f"copying to {ADEPT_DIR}", flush=True)
            if ADEPT_DIR.exists():
                shutil.rmtree(ADEPT_DIR)
            shutil.copytree(home_adept, ADEPT_DIR)
        else:
            # List what was actually created
            for search_dir in [ADEPT_DIR, home_adept, Path.cwd() / ".adept"]:
                if search_dir.exists():
                    contents = list(search_dir.iterdir())
                    print(f"[DEBUG] {search_dir} contains: "
                          f"{[f.name for f in contents]}", flush=True)
            raise RuntimeError(
                "Device registration command succeeded but device.xml "
                "was not created in any expected location."
            )

    print("[OK] Adobe device registered.", flush=True)


def fulfill_acsm(acsm_path, output_path):
    """Download the DRM-protected ebook by fulfilling the ACSM token."""
    print(f"Fulfilling ACSM: {acsm_path.name}", flush=True)
    tool = find_tool("acsmdownloader")
    if not tool:
        raise RuntimeError("acsmdownloader not found. libgourou not built.")

    cmd = [
        tool,
        "-f", str(acsm_path),
        "-o", str(output_path),
    ]
    print(f"[DEBUG] Running: {' '.join(cmd)}", flush=True)

    try:
        result = run(cmd, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Download timed out (120s). The ACSM token may be expired "
            "or the server is unreachable."
        )

    stdout = result.stdout.strip() if result.stdout else ""
    stderr = result.stderr.strip() if result.stderr else ""
    print(f"[DEBUG] acsmdownloader exit={result.returncode}", flush=True)
    if stdout:
        print(f"[DEBUG] stdout: {stdout}", flush=True)
    if stderr:
        print(f"[DEBUG] stderr: {stderr}", flush=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"ACSM download failed (exit {result.returncode}): "
            f"{(stderr or stdout)[:500]}"
        )

    if not output_path.exists():
        raise RuntimeError(
            "Download completed but output file not found. "
            f"stdout: {stdout[:200]}"
        )

    size_kb = output_path.stat().st_size / 1024
    print(f"[OK] Downloaded: {output_path.name} ({size_kb:.0f} KB)", flush=True)


def remove_drm(input_path, output_path):
    """Remove DRM from the downloaded ebook.

    adept_remove decrypts using the AES key from the ADEPT credentials.
    It does NOT re-encode, transcode, or transform any content.
    """
    print(f"Removing DRM: {input_path.name}", flush=True)
    tool = find_tool("adept_remove")
    if not tool:
        raise RuntimeError("adept_remove not found. libgourou not built.")

    cmd = [
        tool,
        "-f", str(input_path),
        "-o", str(output_path),
    ]
    print(f"[DEBUG] Running: {' '.join(cmd)}", flush=True)

    try:
        result = run(cmd, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("DRM removal timed out (60s).")

    stdout = result.stdout.strip() if result.stdout else ""
    stderr = result.stderr.strip() if result.stderr else ""
    print(f"[DEBUG] adept_remove exit={result.returncode}", flush=True)
    if stdout:
        print(f"[DEBUG] stdout: {stdout}", flush=True)
    if stderr:
        print(f"[DEBUG] stderr: {stderr}", flush=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"DRM removal failed (exit {result.returncode}): "
            f"{(stderr or stdout)[:300]}"
        )

    print(f"[OK] DRM removed: {output_path.name}", flush=True)


# --- PDF verification ------------------------------------------------------


class PDFCheckResult:
    def __init__(self):
        self.total_pages: int = 0
        self.pages_with_text: int = 0
        self.pages_image_only: list = []
        self.sample_text: str = ""
        self.warnings: list = []
        self.encrypted: bool = False
        self.has_fonts: bool = False
        self.has_bookmarks: bool = False
        self.link_count: int = 0

    @property
    def has_errors(self) -> bool:
        return self.encrypted

    @property
    def needs_ocr(self) -> bool:
        return len(self.pages_image_only) > 0

    @property
    def probably_image_only(self) -> bool:
        return (
            self.total_pages > 0
            and self.pages_with_text == 0
            and not self.has_fonts
        )

    @property
    def text_ratio(self) -> float:
        if self.total_pages == 0:
            return 0.0
        return self.pages_with_text / self.total_pages

    def summary(self) -> str:
        lines = [
            f"Total pages    : {self.total_pages}",
            f"Pages with text: {self.pages_with_text}",
            f"Image-only     : {len(self.pages_image_only)}",
            f"Text ratio     : {self.text_ratio:.0%}",
            f"Bookmarks      : {'Yes' if self.has_bookmarks else 'No'}",
            f"Links          : {self.link_count}",
        ]
        if self.encrypted:
            lines.append("! PDF is still encrypted!")
        if self.pages_image_only:
            pages_str = ", ".join(str(p) for p in self.pages_image_only[:10])
            if len(self.pages_image_only) > 10:
                pages_str += f" ... and {len(self.pages_image_only) - 10} more"
            lines.append(f"Image-only pages: {pages_str}")
        if self.warnings:
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"  {w}")
        return "\n".join(lines)


def _extract_text_pymupdf(pdf_path: Path, result: PDFCheckResult) -> bool:
    try:
        import fitz
    except ImportError:
        return False
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        result.warnings.append(f"PyMuPDF cannot open PDF: {e}")
        return False
    if doc.is_encrypted:
        result.encrypted = True
        result.warnings.append("PDF is still encrypted after DRM removal.")
        doc.close()
        return True
    result.total_pages = len(doc)
    result.has_fonts = False

    # Check bookmarks / TOC
    toc = doc.get_toc()
    result.has_bookmarks = len(toc) > 0

    for i, page in enumerate(doc):
        try:
            text = page.get_text("text") or ""
            clean = text.strip()
            fonts = page.get_fonts()
            links = page.get_links()
            result.link_count += len(links)
            if fonts:
                result.has_fonts = True
            if len(clean) >= 5:
                result.pages_with_text += 1
                if not result.sample_text and len(clean) > 10:
                    result.sample_text = clean[:200]
            else:
                if fonts:
                    result.pages_with_text += 1
                    if not result.sample_text:
                        result.sample_text = "(text present but not extractable -- fonts embedded)"
                else:
                    result.pages_image_only.append(i + 1)
        except Exception:
            result.pages_image_only.append(i + 1)
    doc.close()
    return True


def _extract_text_pypdf(pdf_path: Path, result: PDFCheckResult) -> bool:
    try:
        from pypdf import PdfReader
    except ImportError:
        return False
    try:
        reader = PdfReader(pdf_path)
    except Exception as e:
        result.warnings.append(f"pypdf cannot open PDF: {e}")
        return False
    if reader.is_encrypted:
        result.encrypted = True
        result.warnings.append("PDF is still encrypted after DRM removal.")
        return True
    result.total_pages = len(reader.pages)
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
            clean = text.strip()
            if len(clean) >= 5:
                result.pages_with_text += 1
                if not result.sample_text and len(clean) > 10:
                    result.sample_text = clean[:200]
            else:
                result.pages_image_only.append(i + 1)
        except Exception:
            result.pages_image_only.append(i + 1)
    return True


def verify_pdf_readability(pdf_path: Path) -> PDFCheckResult:
    result = PDFCheckResult()
    if not pdf_path.exists():
        result.warnings.append(f"PDF file not found: {pdf_path}")
        return result
    if not _extract_text_pymupdf(pdf_path, result):
        if not _extract_text_pypdf(pdf_path, result):
            result.warnings.append(
                "Neither PyMuPDF nor pypdf is installed -- skipping text verification"
            )
    return result


# --- EPUB verification -----------------------------------------------------


class EPUBCheckResult:
    """Mirrors PDFCheckResult's shape so the pipeline can branch cheaply."""

    def __init__(self):
        self.documents: int = 0
        self.images: int = 0
        self.warnings: list = []
        self.encrypted: bool = False
        self.has_fonts: bool = False
        self.valid_container: bool = False

    @property
    def has_errors(self) -> bool:
        return self.encrypted

    def summary(self) -> str:
        lines = [
            f"XHTML documents: {self.documents}",
            f"Images         : {self.images}",
            f"Embedded fonts : {'Yes' if self.has_fonts else 'No'}",
            f"Valid container: {'Yes' if self.valid_container else 'No'}",
        ]
        if self.encrypted:
            lines.append("! EPUB still contains META-INF/encryption.xml!")
        if self.warnings:
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"  {w}")
        return "\n".join(lines)


def verify_epub_readability(epub_path: Path) -> EPUBCheckResult:
    """Confirm the EPUB is a well-formed, decrypted ZIP container.

    The presence of META-INF/encryption.xml is the definitive sign that
    ADEPT DRM is still in place, so its absence is the proof that
    adept_remove succeeded.
    """
    result = EPUBCheckResult()
    if not epub_path.exists():
        result.warnings.append(f"EPUB file not found: {epub_path}")
        return result

    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            names = zf.namelist()

            if "META-INF/encryption.xml" in names:
                result.encrypted = True
                result.warnings.append(
                    "EPUB still contains META-INF/encryption.xml -- "
                    "DRM was not removed."
                )

            # A valid EPUB declares its media type in an uncompressed
            # "mimetype" entry, and routes readers via META-INF/container.xml.
            try:
                mimetype = zf.read("mimetype").decode("ascii", "replace").strip()
                result.valid_container = (
                    mimetype == "application/epub+zip"
                    and "META-INF/container.xml" in names
                )
                if mimetype != "application/epub+zip":
                    result.warnings.append(
                        f"Unexpected mimetype: {mimetype!r}"
                    )
            except KeyError:
                result.warnings.append("EPUB has no 'mimetype' entry.")

            for name in names:
                lower = name.lower()
                if lower.endswith((".xhtml", ".html", ".htm")):
                    result.documents += 1
                elif lower.endswith((".jpg", ".jpeg", ".png", ".gif",
                                     ".svg", ".webp")):
                    result.images += 1
                elif lower.endswith((".ttf", ".otf", ".woff", ".woff2")):
                    result.has_fonts = True
    except zipfile.BadZipFile:
        result.warnings.append(
            "Output is not a readable ZIP archive -- the EPUB may be corrupt "
            "or still encrypted."
        )
        result.encrypted = True
    except Exception as e:
        result.warnings.append(f"Cannot inspect EPUB: {e}")

    return result


# --- Pipeline --------------------------------------------------------------


def convert_pipeline(acsm_path, output_dir, requested_format=None):
    """Generator that yields (step, message) tuples for each conversion step.

    Used by both the CLI and the web interface. Raises RuntimeError on failure.

    Args:
        acsm_path: path to the .acsm token
        output_dir: directory to write the finished book into
        requested_format: "epub", "pdf", or None. When given, the format
            detected in the token must agree, otherwise the conversion is
            refused before any network call is made. This is what backs the
            format picker in the web UI.

    Steps:
        1. Check tools
        2. Detect format (and validate against the requested one)
        3. Register device
        4. Download ebook
        5. Remove DRM
        6. Verify output
    """
    acsm_path = Path(acsm_path).resolve()
    if not acsm_path.exists():
        raise RuntimeError(f"File not found: {acsm_path}")
    if acsm_path.suffix != ".acsm":
        raise RuntimeError(f"Not an ACSM file: {acsm_path}")

    if requested_format:
        requested_format = requested_format.lower().strip()
        if requested_format not in SUPPORTED_FORMATS:
            raise RuntimeError(
                f"Unsupported format: {requested_format!r}. "
                f"Choose one of: {', '.join(SUPPORTED_FORMATS)}."
            )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = acsm_path.stem

    # Step 1: Check tools
    problems = []
    for tool_name in ("acsmdownloader", "adept_activate", "adept_remove"):
        if not find_tool(tool_name):
            problems.append(f"{tool_name} not found (libgourou not built)")
    if problems:
        raise RuntimeError("Missing components: " + "; ".join(set(problems)))
    yield (1, "All tools ready.")

    # Step 2: Detect format, and hold the user's choice to it
    fmt = detect_format(acsm_path)
    if requested_format and fmt != requested_format:
        raise RuntimeError(
            f"You selected {requested_format.upper()}, but this ACSM token is "
            f"for a {fmt.upper()} download. Choose {fmt.upper()} and try again."
        )
    yield (2, f"Detected format: {fmt.upper()}")

    # Step 3: Register device
    register_device()
    yield (3, "Device registered.")

    # Step 4: Download
    drm_file = output_dir / f"{stem}_drm.{fmt}"
    fulfill_acsm(acsm_path, drm_file)
    yield (4, f"Downloaded: {drm_file.name}")

    # Step 5: Remove DRM (decryption only -- preserves all structure)
    clean_file = output_dir / f"{stem}.{fmt}"
    remove_drm(drm_file, clean_file)
    # Clean up the still-encrypted copy
    try:
        drm_file.unlink()
    except Exception:
        pass
    yield (5, f"DRM removed: {clean_file.name}")

    # Step 6: Verify the output really is readable and decrypted
    print(f"Verifying {fmt.upper()} output...", flush=True)
    if fmt == "pdf":
        yield (6, _verify_pdf_step(clean_file))
    else:
        yield (6, _verify_epub_step(clean_file))

    size_mb = clean_file.stat().st_size / (1024 * 1024) if clean_file.exists() else 0
    yield ("done", f"{clean_file.name}|{size_mb:.1f} MB")


def _verify_pdf_step(pdf_file):
    """Run PDF verification and return the step-6 message."""
    res = verify_pdf_readability(pdf_file)

    if res.encrypted:
        raise RuntimeError("DRM removal incomplete: the PDF is still encrypted.")

    structure_parts = []
    if res.has_bookmarks:
        structure_parts.append("bookmarks intact")
    if res.link_count > 0:
        structure_parts.append(f"{res.link_count} links preserved")
    structure_info = (" -- " + ", ".join(structure_parts)) if structure_parts else ""

    if res.probably_image_only:
        return (
            f"PDF scan: 0/{res.total_pages} pages have extractable text. "
            f"Image-only PDF detected{structure_info}."
        )
    if res.needs_ocr:
        img_count = len(res.pages_image_only)
        return (
            f"PDF scan: {res.pages_with_text}/{res.total_pages} pages have "
            f"text, {img_count} page(s) are image-only{structure_info}."
        )
    return (
        f"PDF verified: {res.pages_with_text}/{res.total_pages} pages have "
        f"readable, selectable text{structure_info} -- all OK."
    )


def _verify_epub_step(epub_file):
    """Run EPUB verification and return the step-6 message."""
    res = verify_epub_readability(epub_file)

    if res.encrypted:
        raise RuntimeError(
            "DRM removal incomplete: the EPUB is still encrypted. "
            + "; ".join(res.warnings)
        )

    structure_parts = [f"{res.documents} documents"]
    if res.images:
        structure_parts.append(f"{res.images} images")
    if res.has_fonts:
        structure_parts.append("embedded fonts preserved")
    structure_info = ", ".join(structure_parts)

    if not res.valid_container:
        return (
            f"EPUB decrypted ({structure_info}), but the container looks "
            f"unusual -- it should still open in most readers."
        )
    return f"EPUB verified: DRM-free, valid container -- {structure_info}."


def do_convert(acsm_file, output_dir, requested_format=None):
    """Run the ACSM conversion pipeline (CLI entry point)."""
    try:
        for step, message in convert_pipeline(acsm_file, output_dir,
                                              requested_format):
            if step == "done":
                name, _, size = message.partition("|")
                print(f"\n=== Done! ===\nFile: {name} ({size})")
            else:
                print(f"\n=== Step {step}/6: {message} ===")
    except RuntimeError as e:
        print(str(e))
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Convert ACSM ebook tokens to DRM-free EPUB or PDF.",
    )
    parser.add_argument(
        "acsm_file",
        nargs="?",
        help="Path to the .acsm file to convert",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="output",
        help="Output directory (default: output)",
    )
    parser.add_argument(
        "-f", "--format",
        choices=SUPPORTED_FORMATS,
        help="Expected output format. If it disagrees with the ACSM token, "
             "the conversion is refused. Mirrors the web UI's format picker.",
    )
    parser.add_argument(
        "--verify-only",
        metavar="FILE",
        help="Audit an existing EPUB or PDF instead of converting",
    )
    args = parser.parse_args()

    if args.verify_only:
        path = Path(args.verify_only)
        if path.suffix.lower() == ".pdf":
            result = verify_pdf_readability(path)
        else:
            result = verify_epub_readability(path)
        print(result.summary())
        sys.exit(1 if result.has_errors else 0)

    if not args.acsm_file:
        parser.print_help()
        sys.exit(1)

    do_convert(args.acsm_file, args.output_dir, args.format)


if __name__ == "__main__":
    main()

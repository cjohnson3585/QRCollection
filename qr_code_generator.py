from pathlib import Path
import re

import qrcode


def clean_slug(value: str) -> str:
    """
    Keeps slugs safe for filenames and URLs.
    Example:
        302 C-0007ab -> 302_c_0007ab
    """
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_")


def save_redirect_showcase_page(
    object_slug: str,
    output_html_dir: Path,
) -> Path:
    """
    Creates a small static compatibility page.

    Your FastAPI app already supports:
        /scan/{object_slug}_showcase.html

    and redirects that to:
        /item/{object_slug}

    This file is mostly for compatibility with your original bulk QR workflow.
    """
    output_html_dir = Path(output_html_dir)
    output_html_dir.mkdir(parents=True, exist_ok=True)

    showcase_filename = f"{object_slug}_showcase.html"
    showcase_path = output_html_dir / showcase_filename

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="0; url=/item/{object_slug}">
    <title>Redirecting...</title>
</head>
<body>
    <p>Redirecting to record...</p>
    <p><a href="/item/{object_slug}">Open record</a></p>
</body>
</html>
"""

    showcase_path.write_text(html, encoding="utf-8")
    return showcase_path


def generate_qr_code(
    url: str,
    output_path: Path,
) -> Path:
    """
    Generates one QR code PNG for the given URL.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    qr = qrcode.QRCode(
        version=1,
        box_size=10,
        border=4,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
    )

    qr.add_data(url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    img.save(output_path)

    return output_path


def generate_qr_code_for_record(
    object_slug: str,
    base_url: str,
    output_qr_dir: Path,
    output_html_dir: Path | None = None,
) -> dict:
    """
    Generates the QR code for one newly created record.

    The QR points to:
        {base_url}/scan/{object_slug}_showcase.html

    Your main.py /scan route redirects that to:
        /item/{object_slug}

    Returns metadata about the generated QR.
    """
    object_slug = str(object_slug or "").strip()

    if not object_slug:
        raise ValueError("object_slug is required to generate a QR code.")

    base_url = str(base_url or "").strip().rstrip("/")

    if not base_url:
        raise ValueError("base_url is required to generate a QR code.")

    output_qr_dir = Path(output_qr_dir)
    output_qr_dir.mkdir(parents=True, exist_ok=True)

    showcase_filename = f"{object_slug}_showcase.html"
    qr_url = f"{base_url}/scan/{showcase_filename}"
    qr_output_path = output_qr_dir / f"{object_slug}_qr.png"

    showcase_path = None

    if output_html_dir is not None:
        showcase_path = save_redirect_showcase_page(
            object_slug=object_slug,
            output_html_dir=Path(output_html_dir),
        )

    generate_qr_code(
        url=qr_url,
        output_path=qr_output_path,
    )

    return {
        "object_slug": object_slug,
        "qr_url": qr_url,
        "qr_code_path": str(qr_output_path),
        "showcase_html_path": str(showcase_path) if showcase_path else "",
    }
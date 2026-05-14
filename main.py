from pathlib import Path
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from qr_code_generator import generate_qr_code_for_record


BASE_DIR = Path(__file__).resolve().parent

DB_PATH = BASE_DIR / "qr_collection.db"
RAW_IMAGES_DIR = BASE_DIR / "raw_images"
OUTPUT_HTML_DIR = BASE_DIR / "output_html"
QR_CODES_DIR = BASE_DIR / "output_qr_codes"

ADMIN_USERNAME = os.getenv("QR_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("QR_ADMIN_PASSWORD", "admin321")

# Set this on EC2 if you want QR codes to use your public URL:
# export QR_BASE_URL="http://23.20.253.156:8000"
QR_BASE_URL = os.getenv("QR_BASE_URL", "").strip().rstrip("/")

DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100

SEARCH_COLUMNS = [
    "OBJECTID",
    "ACCESSNO",
    "OBJNAME",
    "DATE",
    "MATERIAL",
    "CONDITION",
    "COLLECTION",
    "DESCRIP",
]

OBJECT_ID_PREFIXES = {
    "201": "Bedding, Flat Textiles",
    "202": "Household Accessory - Table Linens, Doilies, Pillowcases, etc.",
    "301 A": "Aprons",
    "301 B": "Lace Ribbon",
    "301 C": "Umbrellas",
    "301 D": "Jewelry",
    "301 E": "Fringe, Trims",
    "302 A": "Footwear, Female",
    "302 B": "Footwear Children's",
    "302 C": "Footwear, Male",
    "302 D": "Footwear, Socks/Stockings",
    "303 A": "Hats, Female",
    "303 B": "Hats, Caps, Child's",
    "303 C": "Hats Male",
    "303 D": "Bonnets, Caps, Female",
    "304 A": "Clothing Outerwear, Female",
    "304 B": "Clothing Outerwear, Children's",
    "304 C": "Clothing Outerwear, Male",
    "305 A": "Clothing Underwear, Bra/Chemise/Corset Cover/Corsets",
    "305 B": "Clothing Underwear, Petticoat/Half Slip/Slip",
    "305 C": "Clothing Underwear, Teddy/Sets/Bed Jackets",
    "305 D": "Clothing Underwear, Drawers-Female",
    "305 DD": "Clothing Underwear, Drawers/Union Suits-Male",
    "305 E": "Clothing Underwear, Gowns/Robes",
    "305 F": "Swimwear, Female/Male",
    "306 A": "Clothing Accessory, Purse/Muffs",
    "306 B": "Clothing Accessory, Gloves",
    "306 C": "Clothing Accessory, Combs/Hair Ornaments",
    "306 D": "Clothing Accessory, Fans",
    "306 E": "Clothing Accessory, Shawls/Stoles/Capelets",
    "306 F": "Clothing Accessory, Collars/Dickies/Jabots-Female",
    "306 G": "Clothing Accessory, Collars/Ties/Suspenders-Male",
    "306 H": "Clothing Accessory, Scarves/Handkerchiefs-Female",
    "306 I": "Clothing Accessory, Belt Buckles/Belts/Cummerbunds",
    "307": "Personal Gear, e.g. Eye Glasses",
    "309": "Toilet Articles, Perfume Bottles/Compacts/etc.",
    "902": "Ceremonial Artifacts/Uniforms/Wedding Dresses",
}

FOOTWEAR_OBJECT_ID_PREFIXES = {"302 A", "302 B", "302 C", "302 D"}

app = FastAPI(title="QR Collection")
security = HTTPBasic()

RAW_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
QR_CODES_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_HTML_DIR.mkdir(parents=True, exist_ok=True)

app.mount(
    "/raw_images",
    StaticFiles(directory=str(RAW_IMAGES_DIR)),
    name="raw_images",
)

app.mount(
    "/qr_codes",
    StaticFiles(directory=str(QR_CODES_DIR)),
    name="qr_codes",
)


def quote_identifier(name: str) -> str:
    safe_name = str(name).replace('"', '""')
    return f'"{safe_name}"'


def get_db_connection():
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail="Database not found. Run bulk_qr_code_generator.py first.",
        )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    username_ok = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    password_ok = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)

    if not username_ok or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username


def safe_value(item: sqlite3.Row, key: str, default: str = "") -> str:
    try:
        value = item[key]
    except (KeyError, IndexError):
        return default

    if value is None:
        return default

    return str(value)


def html_escape(value: str) -> str:
    value = "" if value is None else str(value)

    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def clamp_page(value: int) -> int:
    return max(1, value)


def clamp_per_page(value: int) -> int:
    if value < 1:
        return DEFAULT_PER_PAGE
    if value > MAX_PER_PAGE:
        return MAX_PER_PAGE
    return value


def get_available_columns() -> set[str]:
    conn = get_db_connection()
    columns = conn.execute("PRAGMA table_info(collection_items)").fetchall()
    conn.close()
    return {col["name"] for col in columns}


def build_home_where_clause(search_query: str, available_columns: set[str]) -> tuple[str, list[str]]:
    search_query = (search_query or "").strip()

    if not search_query:
        return "", []

    searchable_columns = [col for col in SEARCH_COLUMNS if col in available_columns]

    if not searchable_columns:
        return "", []

    like_value = f"%{search_query}%"
    conditions = [f"COALESCE({quote_identifier(col)}, '') LIKE ?" for col in searchable_columns]
    params = [like_value for _ in searchable_columns]

    return f"WHERE {' OR '.join(conditions)}", params


def get_home_items(
    search_query: str = "",
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> tuple[list[sqlite3.Row], int]:
    page = clamp_page(page)
    per_page = clamp_per_page(per_page)
    offset = (page - 1) * per_page

    available_columns = get_available_columns()
    where_clause, where_params = build_home_where_clause(search_query, available_columns)

    conn = get_db_connection()

    total = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM collection_items
        {where_clause}
        """,
        where_params,
    ).fetchone()["count"]

    order_column = "OBJECTID" if "OBJECTID" in available_columns else "id"

    items = conn.execute(
        f"""
        SELECT *
        FROM collection_items
        {where_clause}
        ORDER BY {quote_identifier(order_column)}
        LIMIT ? OFFSET ?
        """,
        where_params + [per_page, offset],
    ).fetchall()

    conn.close()

    return items, int(total)


def get_item_by_slug(object_slug: str):
    conn = get_db_connection()
    item = conn.execute(
        """
        SELECT *
        FROM collection_items
        WHERE object_slug = ?
        """,
        (object_slug,),
    ).fetchone()
    conn.close()
    return item


def get_editable_columns():
    conn = get_db_connection()
    columns = conn.execute("PRAGMA table_info(collection_items)").fetchall()
    conn.close()

    excluded = {"id", "object_slug"}

    return [
        col["name"]
        for col in columns
        if col["name"] not in excluded
    ]


def slugify(value: str) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_")


def build_base_slug_from_form(form_data) -> str:
    for column in ["OBJECTID", "ACCESSNO", "OBJNAME"]:
        value = str(form_data.get(column, "")).strip()
        slug = slugify(value)
        if slug:
            return slug

    return f"record_{secrets.token_hex(4)}"


def make_unique_object_slug(base_slug: str) -> str:
    base_slug = slugify(base_slug) or f"record_{secrets.token_hex(4)}"

    conn = get_db_connection()

    candidate = base_slug
    counter = 2

    while True:
        existing = conn.execute(
            """
            SELECT 1
            FROM collection_items
            WHERE object_slug = ?
            LIMIT 1
            """,
            (candidate,),
        ).fetchone()

        if not existing:
            conn.close()
            return candidate

        candidate = f"{base_slug}_{counter}"
        counter += 1


def normalize_object_id_prefix(prefix: str) -> str:
    prefix = str(prefix or "").strip().upper()
    return re.sub(r"\s+", " ", prefix)


def get_object_id_number_from_value(object_id: str, prefix: str) -> Optional[int]:
    object_id = str(object_id or "").strip().upper()
    prefix = normalize_object_id_prefix(prefix)

    pattern = rf"^{re.escape(prefix)}-(\d{{1,10}})(?:A|AB)?$"
    match = re.match(pattern, object_id)

    if not match:
        return None

    return int(match.group(1))


def get_next_object_id_number(prefix: str) -> int:
    prefix = normalize_object_id_prefix(prefix)

    if prefix not in OBJECT_ID_PREFIXES:
        raise HTTPException(status_code=400, detail="Invalid OBJECTID prefix.")

    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT OBJECTID
        FROM collection_items
        WHERE OBJECTID LIKE ?
        """,
        (f"{prefix}-%",),
    ).fetchall()
    conn.close()

    max_number = -1

    for row in rows:
        number = get_object_id_number_from_value(safe_value(row, "OBJECTID"), prefix)
        if number is not None and number > max_number:
            max_number = number

    return max_number + 1


def build_object_id(prefix: str, object_number: int, paired_item: bool = False) -> str:
    prefix = normalize_object_id_prefix(prefix)

    if prefix not in OBJECT_ID_PREFIXES:
        raise HTTPException(status_code=400, detail="Invalid OBJECTID prefix.")

    suffix = ""

    if prefix in FOOTWEAR_OBJECT_ID_PREFIXES:
        suffix = "ab" if paired_item else "a"

    return f"{prefix}-{object_number:04d}{suffix}"


def object_id_exists(object_id: str) -> bool:
    conn = get_db_connection()
    existing = conn.execute(
        """
        SELECT 1
        FROM collection_items
        WHERE OBJECTID = ?
        LIMIT 1
        """,
        (object_id,),
    ).fetchone()
    conn.close()

    return existing is not None


def build_create_object_id_from_form(form_data) -> str:
    prefix = normalize_object_id_prefix(form_data.get("object_id_prefix", ""))

    if prefix not in OBJECT_ID_PREFIXES:
        raise HTTPException(status_code=400, detail="Please select a valid OBJECTID prefix.")

    raw_number = str(form_data.get("object_id_number", "")).strip()

    if not raw_number:
        raise HTTPException(status_code=400, detail="OBJECTID number is required.")

    if not raw_number.isdigit():
        raise HTTPException(status_code=400, detail="OBJECTID number must contain digits only.")

    requested_number = int(raw_number)
    next_number = get_next_object_id_number(prefix)

    if requested_number < next_number:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid OBJECTID number. The next available number for {prefix} "
                f"is {next_number:04d}. You may use {next_number:04d} or a larger number."
            ),
        )

    paired_item = str(form_data.get("object_id_is_pair", "")).lower() in {"on", "true", "1", "yes"}

    object_id = build_object_id(
        prefix=prefix,
        object_number=requested_number,
        paired_item=paired_item,
    )

    if object_id_exists(object_id):
        raise HTTPException(
            status_code=400,
            detail=f"OBJECTID {object_id} already exists. Please choose a larger number.",
        )

    return object_id


def get_object_id_prefix_defaults() -> dict[str, dict[str, object]]:
    defaults = {}

    for prefix, label in OBJECT_ID_PREFIXES.items():
        next_number = get_next_object_id_number(prefix)
        defaults[prefix] = {
            "label": label,
            "nextNumber": next_number,
            "nextNumberPadded": f"{next_number:04d}",
            "defaultObjectId": build_object_id(
                prefix=prefix,
                object_number=next_number,
                paired_item=False,
            ),
            "isFootwear": prefix in FOOTWEAR_OBJECT_ID_PREFIXES,
        }

    return defaults


def get_request_base_url(request: Request) -> str:
    """
    Uses QR_BASE_URL when set. Otherwise uses the current request's base URL.

    On EC2, set QR_BASE_URL to your public app URL so printed/scanned QR codes
    do not point to localhost or an internal hostname.
    """
    if QR_BASE_URL:
        return QR_BASE_URL

    return str(request.base_url).strip().rstrip("/")


def base_css() -> str:
    return """
        body {
            margin: 0;
            font-family: Georgia, "Times New Roman", serif;
            background-color: #f6f2eb;
            color: #2f2a24;
        }

        header {
            background-color: #4b3b2a;
            color: white;
            padding: 24px 40px;
            text-align: center;
            border-bottom: 4px solid #b89b72;
        }

        header h1 {
            margin: 0;
            font-size: 32px;
            letter-spacing: 1px;
        }

        header p {
            margin: 8px 0 0;
            font-size: 16px;
            color: #e9dcc9;
        }

        .container {
            max-width: 1200px;
            margin: 40px auto;
            background: white;
            border-radius: 14px;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
            overflow: hidden;
        }

        .content {
            display: flex;
            flex-wrap: wrap;
        }

        .image-section {
            flex: 1 1 420px;
            background-color: #f3ede4;
            padding: 30px;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .image-section img {
            max-width: 100%;
            max-height: 600px;
            border-radius: 10px;
            border: 1px solid #d7c8b3;
            box-shadow: 0 4px 10px rgba(0, 0, 0, 0.08);
        }

        .image-placeholder {
            width: 100%;
            min-height: 280px;
            border: 1px dashed #b89b72;
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #7a6a58;
            background-color: #faf7f2;
            text-align: center;
            padding: 20px;
            box-sizing: border-box;
        }

        .details-section {
            flex: 1 1 500px;
            padding: 35px;
        }

        .item-title {
            margin-top: 0;
            margin-bottom: 8px;
            font-size: 34px;
            color: #3b3025;
        }

        .item-subtitle {
            font-size: 18px;
            color: #7a6a58;
            margin-bottom: 28px;
        }

        .edit-button {
            display: inline-block;
            margin-right: 8px;
            margin-bottom: 28px;
            padding: 10px 16px;
            background-color: #4b3b2a;
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-size: 14px;
            border: 1px solid #b89b72;
        }

        .edit-button:hover {
            background-color: #3b3025;
        }

        .info-grid {
            display: grid;
            grid-template-columns: 180px 1fr;
            gap: 12px 16px;
            margin-bottom: 30px;
        }

        .label {
            font-weight: bold;
            color: #5c4a38;
        }

        .value {
            color: #2f2a24;
            overflow-wrap: anywhere;
        }

        .description-box {
            margin-top: 20px;
            padding: 20px;
            background-color: #faf7f2;
            border-left: 5px solid #b89b72;
            border-radius: 8px;
        }

        .description-box h3 {
            margin-top: 0;
            color: #4b3b2a;
        }

        .description-box p {
            margin-bottom: 0;
            line-height: 1.7;
        }

        .form-grid {
            display: grid;
            grid-template-columns: 180px 1fr;
            gap: 12px 16px;
            margin-bottom: 30px;
        }

        label {
            font-weight: bold;
            color: #5c4a38;
        }

        input,
        textarea,
        select {
            width: 100%;
            font-family: Georgia, "Times New Roman", serif;
            font-size: 15px;
            padding: 8px;
            border: 1px solid #d7c8b3;
            border-radius: 6px;
            color: #2f2a24;
            box-sizing: border-box;
            background: white;
        }

        textarea {
            min-height: 130px;
            line-height: 1.5;
        }

        .button-row {
            display: flex;
            gap: 12px;
            margin-top: 20px;
            flex-wrap: wrap;
        }

        button,
        .cancel-button {
            display: inline-block;
            padding: 10px 16px;
            background-color: #4b3b2a;
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-size: 14px;
            border: 1px solid #b89b72;
            cursor: pointer;
        }

        button:hover,
        .cancel-button:hover {
            background-color: #3b3025;
        }

        footer {
            background-color: #4b3b2a;
            color: #f3e7d7;
            text-align: center;
            padding: 18px 20px;
            font-size: 14px;
            border-top: 4px solid #b89b72;
        }

        @media (max-width: 900px) {
            .content {
                flex-direction: column;
            }

            .details-section {
                padding: 25px;
            }

            .info-grid,
            .form-grid {
                grid-template-columns: 1fr;
            }

            .label {
                margin-top: 10px;
            }
        }
    """


def render_image_block(image_filename: str, object_id: str) -> str:
    image_filename = image_filename.strip()

    if image_filename:
        image_url = f"/raw_images/{html_escape(image_filename)}"
        return f"""
            <img src="{image_url}" alt="{html_escape(object_id)} image">
        """

    return """
        <div class="image-placeholder">
            No image assigned yet
        </div>
    """


def save_uploaded_image(uploaded_file: UploadFile, object_slug: str) -> str:
    if not uploaded_file or not uploaded_file.filename:
        return ""

    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp"}

    original_filename = uploaded_file.filename
    file_extension = Path(original_filename).suffix.lower()

    if file_extension not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail="Only .jpg, .jpeg, .png, and .webp images are allowed.",
        )

    RAW_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    safe_filename = f"{object_slug}{file_extension}"
    destination_path = RAW_IMAGES_DIR / safe_filename

    with destination_path.open("wb") as buffer:
        shutil.copyfileobj(uploaded_file.file, buffer)

    return safe_filename


def render_pagination_controls(search_query: str, page: int, per_page: int, total_count: int) -> str:
    total_pages = max(1, math.ceil(total_count / per_page))
    page = min(max(1, page), total_pages)

    query_value = html_escape(search_query)

    previous_page = page - 1
    next_page = page + 1

    previous_disabled = page <= 1
    next_disabled = page >= total_pages

    previous_href = f"/?q={query_value}&page={previous_page}&per_page={per_page}"
    next_href = f"/?q={query_value}&page={next_page}&per_page={per_page}"

    previous_html = (
        '<span class="page-button disabled">Previous</span>'
        if previous_disabled
        else f'<a class="page-button" href="{previous_href}">Previous</a>'
    )

    next_html = (
        '<span class="page-button disabled">Next</span>'
        if next_disabled
        else f'<a class="page-button" href="{next_href}">Next</a>'
    )

    return f"""
        <div class="pagination-row">
            <div>
                Showing page <strong>{page}</strong> of <strong>{total_pages}</strong>
                · <strong>{total_count}</strong> matching record(s)
            </div>
            <div class="pagination-buttons">
                {previous_html}
                {next_html}
            </div>
        </div>
    """


def render_home_table(
    items: list[sqlite3.Row],
    search_query: str = "",
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    total_count: int = 0,
) -> str:
    rows_html = ""

    for item in items:
        object_slug = safe_value(item, "object_slug")
        object_id = safe_value(item, "OBJECTID", object_slug)
        accession_no = safe_value(item, "ACCESSNO")
        object_name = safe_value(item, "OBJNAME")
        item_date = safe_value(item, "DATE")
        material = safe_value(item, "MATERIAL")
        condition = safe_value(item, "CONDITION")
        image_filename = safe_value(item, "image_filename")

        record_url = f"/item/{object_slug}"

        if image_filename:
            image_url = f"/raw_images/{html_escape(image_filename)}"
            image_html = f"""
                <a href="{image_url}" target="_blank" rel="noopener noreferrer">
                    <img
                        class="thumbnail"
                        src="{image_url}"
                        alt="{html_escape(object_id)} thumbnail"
                        loading="lazy"
                        decoding="async"
                    >
                </a>
            """
        else:
            image_html = """
                <div class="thumbnail-placeholder">No Image</div>
            """

        qr_filename = f"{object_slug}_qr.png"
        qr_url = f"/qr_codes/{qr_filename}"

        rows_html += f"""
            <tr>
                <td>{image_html}</td>
                <td>
                    <a class="download-link" href="{qr_url}" download>
                        Download QR
                    </a>
                    <br>
                    <a class="download-link" href="{qr_url}" target="_blank" rel="noopener noreferrer">
                        Open QR
                    </a>
                </td>
                <td>{html_escape(object_id)}</td>
                <td>{html_escape(accession_no)}</td>
                <td>{html_escape(object_name)}</td>
                <td>{html_escape(item_date)}</td>
                <td>{html_escape(material)}</td>
                <td>{html_escape(condition)}</td>
                <td>
                    <a class="record-link" href="{record_url}">
                        Open Record
                    </a>
                    <br>
                    <span class="small-url">{html_escape(record_url)}</span>
                </td>
            </tr>
        """

    if not rows_html:
        rows_html = """
            <tr>
                <td colspan="9">No matching records found.</td>
            </tr>
        """

    pagination_controls = render_pagination_controls(
        search_query=search_query,
        page=page,
        per_page=per_page,
        total_count=total_count,
    )

    search_query_escaped = html_escape(search_query)

    per_page_options = ""
    for option in [10, 25, 50, 100]:
        selected = "selected" if option == per_page else ""
        per_page_options += f'<option value="{option}" {selected}>{option}</option>'

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>QR Collection - All Records</title>
    <style>
        {base_css()}

        .table-container {{
            padding: 35px;
            overflow-x: auto;
        }}

        .records-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 18px;
            margin-bottom: 8px;
            flex-wrap: wrap;
        }}

        .records-title {{
            margin-top: 0;
            margin-bottom: 8px;
            font-size: 34px;
            color: #3b3025;
        }}

        .records-subtitle {{
            font-size: 18px;
            color: #7a6a58;
            margin-bottom: 28px;
        }}

        .create-record-button {{
            display: inline-block;
            padding: 11px 16px;
            background-color: #4b3b2a;
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-size: 14px;
            border: 1px solid #b89b72;
            white-space: nowrap;
        }}

        .create-record-button:hover {{
            background-color: #3b3025;
        }}

        .filter-form {{
            display: grid;
            grid-template-columns: 1fr 140px auto auto;
            gap: 12px;
            align-items: end;
            margin-bottom: 22px;
            padding: 18px;
            background: #faf7f2;
            border: 1px solid #eadfce;
            border-radius: 10px;
        }}

        .filter-field label {{
            display: block;
            margin-bottom: 6px;
        }}

        .clear-link {{
            display: inline-block;
            padding: 10px 16px;
            color: #4b3b2a;
            font-weight: bold;
            text-decoration: none;
        }}

        .pagination-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin: 18px 0;
            flex-wrap: wrap;
            color: #5c4a38;
        }}

        .pagination-buttons {{
            display: flex;
            gap: 8px;
        }}

        .page-button {{
            display: inline-block;
            padding: 8px 12px;
            border-radius: 8px;
            background: #4b3b2a;
            color: white;
            border: 1px solid #b89b72;
            text-decoration: none;
            font-size: 14px;
        }}

        .page-button.disabled {{
            background: #d7c8b3;
            color: #7a6a58;
            cursor: not-allowed;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 15px;
        }}

        th {{
            background-color: #4b3b2a;
            color: white;
            text-align: left;
            padding: 12px;
            border-bottom: 4px solid #b89b72;
        }}

        td {{
            padding: 12px;
            border-bottom: 1px solid #eadfce;
            vertical-align: middle;
        }}

        tr:nth-child(even) {{
            background-color: #faf7f2;
        }}

        .thumbnail {{
            width: 75px;
            height: 75px;
            object-fit: cover;
            border-radius: 8px;
            border: 1px solid #d7c8b3;
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.06);
            background: white;
        }}

        .thumbnail-placeholder {{
            width: 75px;
            height: 75px;
            border-radius: 8px;
            border: 1px dashed #d7c8b3;
            background: #faf7f2;
            color: #7a6a58;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            text-align: center;
        }}

        .record-link,
        .download-link {{
            color: #4b3b2a;
            font-weight: bold;
            text-decoration: none;
        }}

        .download-link {{
            display: inline-block;
            margin-top: 3px;
            margin-bottom: 3px;
            font-size: 13px;
        }}

        .small-url {{
            display: inline-block;
            margin-top: 4px;
            color: #7a6a58;
            font-size: 12px;
            overflow-wrap: anywhere;
        }}

        @media (max-width: 900px) {{
            .filter-form {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
</head>
<body>

    <header>
        <h1>Historic Costume and Textiles Collection</h1>
        <p>QR Collection Records Table</p>
    </header>

    <div class="container">
        <div class="table-container">
            <div class="records-header">
                <div>
                    <h2 class="records-title">All Collection Records</h2>
                    <div class="records-subtitle">
                        Search records, view image thumbnails, open record pages, or download QR codes.
                    </div>
                </div>

                <a class="create-record-button" href="/admin/create">
                    Create New Record
                </a>
            </div>

            <form class="filter-form" method="get" action="/">
                <div class="filter-field">
                    <label for="q">Search records</label>
                    <input
                        id="q"
                        name="q"
                        value="{search_query_escaped}"
                        placeholder="Search Object ID, accession no., name, material, condition, description..."
                    >
                </div>

                <div class="filter-field">
                    <label for="per_page">Rows per page</label>
                    <select id="per_page" name="per_page">
                        {per_page_options}
                    </select>
                </div>

                <input type="hidden" name="page" value="1">
                <button type="submit">Filter</button>
                <a class="clear-link" href="/">Clear</a>
            </form>

            {pagination_controls}

            <table>
                <thead>
                    <tr>
                        <th>Image</th>
                        <th>QR Code</th>
                        <th>Object ID</th>
                        <th>Accession No.</th>
                        <th>Object Name</th>
                        <th>Date</th>
                        <th>Material</th>
                        <th>Condition</th>
                        <th>Record Page</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>

            {pagination_controls}
        </div>
    </div>

    <footer>
        Historic Costume and Textiles Collection · Mississippi State University · Collection Access Page
    </footer>

</body>
</html>
"""


def render_item_page(item: sqlite3.Row) -> str:
    object_slug = safe_value(item, "object_slug")
    object_id = safe_value(item, "OBJECTID", object_slug)
    accession_no = safe_value(item, "ACCESSNO")
    description = safe_value(item, "DESCRIP")
    item_date = safe_value(item, "DATE")
    object_name = safe_value(item, "OBJNAME")
    material = safe_value(item, "MATERIAL")
    catalogued_by = safe_value(item, "CATBY")
    catalogued_date = safe_value(item, "CATDATE")
    collection = safe_value(item, "COLLECTION")
    condition = safe_value(item, "CONDITION")
    image_filename = safe_value(item, "image_filename")

    image_block = render_image_block(image_filename, object_id)
    qr_url = f"/qr_codes/{object_slug}_qr.png"

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>QR Collection - {html_escape(object_id)}</title>
    <style>{base_css()}</style>
</head>
<body>

    <header>
        <h1>Historic Costume and Textiles Collection</h1>
        <p>QR Collection Object Record</p>
    </header>

    <div class="container">
        <div class="content">
            <div class="image-section">
                {image_block}
            </div>

            <div class="details-section">
                <h2 class="item-title">{html_escape(object_id)}</h2>
                <div class="item-subtitle">{html_escape(object_name)} · {html_escape(item_date)}</div>

                <a class="edit-button" href="/admin/edit/{object_slug}">Edit this Record</a>
                <a class="edit-button" href="{qr_url}" target="_blank" rel="noopener noreferrer">Open QR Code</a>
                <a class="edit-button" href="{qr_url}" download>Download QR Code</a>
                <a class="edit-button" href="/">Back to All Records</a>

                <div class="info-grid">
                    <div class="label">Object ID</div>
                    <div class="value">{html_escape(object_id)}</div>

                    <div class="label">Accession No.</div>
                    <div class="value">{html_escape(accession_no)}</div>

                    <div class="label">Date</div>
                    <div class="value">{html_escape(item_date)}</div>

                    <div class="label">Object Name</div>
                    <div class="value">{html_escape(object_name)}</div>

                    <div class="label">Material</div>
                    <div class="value">{html_escape(material)}</div>

                    <div class="label">Catalogued By</div>
                    <div class="value">{html_escape(catalogued_by)}</div>

                    <div class="label">Catalogued Date</div>
                    <div class="value">{html_escape(catalogued_date)}</div>

                    <div class="label">Collection</div>
                    <div class="value">{html_escape(collection)}</div>

                    <div class="label">Condition</div>
                    <div class="value">{html_escape(condition)}</div>

                    <div class="label">Image Filename</div>
                    <div class="value">{html_escape(image_filename)}</div>
                </div>

                <div class="description-box">
                    <h3>Description</h3>
                    <p>{html_escape(description)}</p>
                </div>
            </div>
        </div>
    </div>

    <footer>
        Historic Costume and Textiles Collection · Mississippi State University · Collection Access Page
    </footer>

</body>
</html>
"""


def render_edit_page(item: sqlite3.Row) -> str:
    object_slug = safe_value(item, "object_slug")
    object_id = safe_value(item, "OBJECTID", object_slug)
    object_name = safe_value(item, "OBJNAME")
    item_date = safe_value(item, "DATE")
    image_filename = safe_value(item, "image_filename")

    editable_columns = get_editable_columns()
    form_fields_html = ""

    for column in editable_columns:
        value = safe_value(item, column)

        if column == "DESCRIP":
            form_fields_html += f"""
                <label for="{html_escape(column)}">{html_escape(column)}</label>
                <textarea id="{html_escape(column)}" name="{html_escape(column)}">{html_escape(value)}</textarea>
            """
        else:
            form_fields_html += f"""
                <label for="{html_escape(column)}">{html_escape(column)}</label>
                <input id="{html_escape(column)}" name="{html_escape(column)}" value="{html_escape(value)}">
            """

    image_block = render_image_block(image_filename, object_id)

    image_upload_html = f"""
        <div class="description-box">
            <h3>Image Upload</h3>
            <p>
                Current image filename:
                <strong>{html_escape(image_filename) if image_filename else "No image assigned yet"}</strong>
            </p>

            <label for="uploaded_image">Upload Image</label>
            <input
                id="uploaded_image"
                name="uploaded_image"
                type="file"
                accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp"
            >
        </div>
    """

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Edit Record - {html_escape(object_id)}</title>
    <style>{base_css()}</style>
</head>
<body>

    <header>
        <h1>Historic Costume and Textiles Collection</h1>
        <p>Edit QR Collection Object Record</p>
    </header>

    <div class="container">
        <div class="content">
            <div class="image-section">
                {image_block}
            </div>

            <div class="details-section">
                <h2 class="item-title">Edit {html_escape(object_id)}</h2>
                <div class="item-subtitle">{html_escape(object_name)} · {html_escape(item_date)}</div>

                <form method="post" action="/admin/edit/{object_slug}" enctype="multipart/form-data">
                    <div class="form-grid">
                        {form_fields_html}
                    </div>

                    {image_upload_html}

                    <div class="button-row">
                        <button type="submit">Save Changes</button>
                        <a class="cancel-button" href="/item/{object_slug}">Cancel</a>
                        <a class="cancel-button" href="/">Back to All Records</a>
                    </div>
                </form>
            </div>
        </div>
    </div>

    <footer>
        Historic Costume and Textiles Collection · Mississippi State University · Collection Access Page
    </footer>

</body>
</html>
"""


def render_create_object_id_builder() -> str:
    prefix_defaults = get_object_id_prefix_defaults()
    prefix_defaults_json = html_escape(json.dumps(prefix_defaults))

    options_html = ""

    for prefix, label in OBJECT_ID_PREFIXES.items():
        options_html += f"""
            <option value="{html_escape(prefix)}">
                {html_escape(prefix)} : {html_escape(label)}
            </option>
        """

    first_prefix = next(iter(OBJECT_ID_PREFIXES.keys()))
    first_next_number = prefix_defaults[first_prefix]["nextNumber"]
    first_default_object_id = prefix_defaults[first_prefix]["defaultObjectId"]

    return f"""
        <div class="object-id-builder">
            <h3>OBJECTID</h3>
            <p>
                Select the object type prefix first. The system will suggest the next available OBJECTID
                based on the existing records in the database.
            </p>

            <div class="object-id-grid">
                <label for="object_id_prefix">OBJECTID Prefix</label>
                <select id="object_id_prefix" name="object_id_prefix" required>
                    {options_html}
                </select>

                <label for="object_id_number">OBJECTID Number</label>
                <input
                    id="object_id_number"
                    name="object_id_number"
                    value="{int(first_next_number):04d}"
                    inputmode="numeric"
                    pattern="[0-9]+"
                    required
                >

                <label for="object_id_preview">Generated OBJECTID</label>
                <input
                    id="object_id_preview"
                    name="OBJECTID"
                    value="{html_escape(first_default_object_id)}"
                    readonly
                >

                <div id="pair_label" class="pair-label">
                    Footwear/Socks Pair
                </div>

                <div id="pair_controls" class="pair-controls">
                    <label class="checkbox-label">
                        <input id="object_id_is_pair" name="object_id_is_pair" type="checkbox">
                        This record contains two shoes/socks. Append <strong>ab</strong>.
                    </label>
                    <div class="helper-text">
                        Leave unchecked for a single shoe/sock. The OBJECTID will end in <strong>a</strong>.
                    </div>
                </div>
            </div>

            <div id="object_id_help" class="helper-text">
                You may keep the suggested number or enter a larger custom number.
            </div>
        </div>

        <script>
            const objectIdPrefixData = JSON.parse("{prefix_defaults_json}".replaceAll("&quot;", '"'));
            const footwearPrefixes = new Set({json.dumps(sorted(FOOTWEAR_OBJECT_ID_PREFIXES))});

            const prefixSelect = document.getElementById("object_id_prefix");
            const numberInput = document.getElementById("object_id_number");
            const previewInput = document.getElementById("object_id_preview");
            const pairCheckbox = document.getElementById("object_id_is_pair");
            const pairLabel = document.getElementById("pair_label");
            const pairControls = document.getElementById("pair_controls");
            const objectIdHelp = document.getElementById("object_id_help");

            function padNumber(value) {{
                const numericValue = String(value || "").replace(/\\D/g, "");
                if (!numericValue) {{
                    return "";
                }}
                return numericValue.padStart(4, "0");
            }}

            function selectedPrefixIsFootwear() {{
                return footwearPrefixes.has(prefixSelect.value);
            }}

            function buildObjectId() {{
                const prefix = prefixSelect.value;
                const number = padNumber(numberInput.value);

                if (!number) {{
                    previewInput.value = "";
                    return;
                }}

                let suffix = "";

                if (selectedPrefixIsFootwear()) {{
                    suffix = pairCheckbox.checked ? "ab" : "a";
                }}

                previewInput.value = `${{prefix}}-${{number}}${{suffix}}`;
            }}

            function updatePairVisibility() {{
                if (selectedPrefixIsFootwear()) {{
                    pairLabel.style.display = "block";
                    pairControls.style.display = "block";
                }} else {{
                    pairLabel.style.display = "none";
                    pairControls.style.display = "none";
                    pairCheckbox.checked = false;
                }}
            }}

            function resetNumberForPrefix() {{
                const prefix = prefixSelect.value;
                const data = objectIdPrefixData[prefix];

                if (!data) {{
                    return;
                }}

                numberInput.value = String(data.nextNumberPadded);
                updatePairVisibility();
                buildObjectId();

                objectIdHelp.innerHTML = `
                    Next available number for <strong>${{prefix}}</strong> is
                    <strong>${{data.nextNumberPadded}}</strong>. You may use this number or a larger number.
                `;
            }}

            prefixSelect.addEventListener("change", resetNumberForPrefix);

            numberInput.addEventListener("input", function () {{
                numberInput.value = numberInput.value.replace(/\\D/g, "");
                buildObjectId();
            }});

            numberInput.addEventListener("blur", function () {{
                if (numberInput.value) {{
                    numberInput.value = padNumber(numberInput.value);
                }}
                buildObjectId();
            }});

            pairCheckbox.addEventListener("change", buildObjectId);

            resetNumberForPrefix();
        </script>
    """


def render_create_page() -> str:
    editable_columns = get_editable_columns()
    form_fields_html = ""

    for column in editable_columns:
        if column == "OBJECTID":
            continue

        if column == "DESCRIP":
            form_fields_html += f"""
                <label for="{html_escape(column)}">{html_escape(column)}</label>
                <textarea id="{html_escape(column)}" name="{html_escape(column)}"></textarea>
            """
        else:
            form_fields_html += f"""
                <label for="{html_escape(column)}">{html_escape(column)}</label>
                <input id="{html_escape(column)}" name="{html_escape(column)}" value="">
            """

    image_block = render_image_block("", "New Record")
    object_id_builder = render_create_object_id_builder()

    image_upload_html = """
        <div class="description-box">
            <h3>Image Upload</h3>
            <p>
                Current image filename:
                <strong>No image assigned yet</strong>
            </p>

            <label for="uploaded_image">Upload Image</label>
            <input
                id="uploaded_image"
                name="uploaded_image"
                type="file"
                accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp"
            >
        </div>
    """

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Create New Record</title>
    <style>
        {base_css()}

        .object-id-builder {{
            margin-bottom: 30px;
            padding: 20px;
            background-color: #faf7f2;
            border: 1px solid #eadfce;
            border-left: 5px solid #b89b72;
            border-radius: 10px;
        }}

        .object-id-builder h3 {{
            margin-top: 0;
            color: #4b3b2a;
        }}

        .object-id-builder p {{
            color: #5c4a38;
            line-height: 1.6;
        }}

        .object-id-grid {{
            display: grid;
            grid-template-columns: 180px 1fr;
            gap: 12px 16px;
            align-items: center;
        }}

        .checkbox-label {{
            display: flex;
            gap: 8px;
            align-items: flex-start;
            font-weight: normal;
            color: #2f2a24;
        }}

        .checkbox-label input {{
            width: auto;
            margin-top: 3px;
        }}

        .helper-text {{
            margin-top: 8px;
            color: #7a6a58;
            font-size: 13px;
            line-height: 1.5;
        }}

        #object_id_preview {{
            background-color: #f3ede4;
            font-weight: bold;
        }}

        @media (max-width: 900px) {{
            .object-id-grid {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
</head>
<body>

    <header>
        <h1>Historic Costume and Textiles Collection</h1>
        <p>Create QR Collection Object Record</p>
    </header>

    <div class="container">
        <div class="content">
            <div class="image-section">
                {image_block}
            </div>

            <div class="details-section">
                <h2 class="item-title">Create New Record</h2>
                <div class="item-subtitle">
                    Start with the OBJECTID prefix. The database will determine the next valid number.
                </div>

                <form method="post" action="/admin/create" enctype="multipart/form-data">
                    {object_id_builder}

                    <div class="form-grid">
                        {form_fields_html}
                    </div>

                    {image_upload_html}

                    <div class="button-row">
                        <button type="submit">Create Record</button>
                        <a class="cancel-button" href="/">Cancel</a>
                        <a class="cancel-button" href="/">Back to All Records</a>
                    </div>
                </form>
            </div>
        </div>
    </div>

    <footer>
        Historic Costume and Textiles Collection · Mississippi State University · Collection Access Page
    </footer>

</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index(
    q: str = Query("", description="Search/filter text"),
    page: int = Query(1, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE, ge=1, le=MAX_PER_PAGE),
):
    page = clamp_page(page)
    per_page = clamp_per_page(per_page)

    items, total_count = get_home_items(
        search_query=q,
        page=page,
        per_page=per_page,
    )

    total_pages = max(1, math.ceil(total_count / per_page))
    if page > total_pages:
        return RedirectResponse(
            url=f"/?q={html_escape(q)}&page={total_pages}&per_page={per_page}",
            status_code=302,
        )

    return HTMLResponse(
        render_home_table(
            items=items,
            search_query=q,
            page=page,
            per_page=per_page,
            total_count=total_count,
        )
    )


@app.get("/scan/{filename}", response_class=HTMLResponse)
def scan_qr(filename: str):
    if not filename.endswith(".html"):
        raise HTTPException(status_code=404, detail="Only HTML files are allowed")

    object_slug = filename.replace("_showcase.html", "")

    item = get_item_by_slug(object_slug)

    if not item:
        raise HTTPException(status_code=404, detail="Collection item not found")

    return RedirectResponse(url=f"/item/{object_slug}", status_code=302)


@app.get("/item/{object_slug}", response_class=HTMLResponse)
def item_detail(object_slug: str):
    item = get_item_by_slug(object_slug)

    if not item:
        raise HTTPException(status_code=404, detail="Collection item not found")

    return HTMLResponse(render_item_page(item))


@app.get("/admin/edit/{object_slug}", response_class=HTMLResponse)
def edit_item_page(
    object_slug: str,
    admin_user=Depends(require_admin),
):
    item = get_item_by_slug(object_slug)

    if not item:
        raise HTTPException(status_code=404, detail="Collection item not found")

    return HTMLResponse(render_edit_page(item))


@app.post("/admin/edit/{object_slug}")
async def update_item(
    request: Request,
    object_slug: str,
    uploaded_image: Optional[UploadFile] = File(None),
    admin_user=Depends(require_admin),
):
    item = get_item_by_slug(object_slug)

    if not item:
        raise HTTPException(status_code=404, detail="Collection item not found")

    editable_columns = get_editable_columns()
    form_data = await request.form()

    update_columns = []
    update_values = []

    for column in editable_columns:
        if column == "image_filename":
            continue

        update_columns.append(f"{quote_identifier(column)} = ?")
        update_values.append(str(form_data.get(column, "")))

    if uploaded_image and uploaded_image.filename:
        saved_image_filename = save_uploaded_image(uploaded_image, object_slug)
        update_columns.append('"image_filename" = ?')
        update_values.append(saved_image_filename)
    else:
        if "image_filename" in editable_columns:
            update_columns.append('"image_filename" = ?')
            update_values.append(str(form_data.get("image_filename", "")))

    if not update_columns:
        return RedirectResponse(url=f"/item/{object_slug}", status_code=303)

    update_values.append(object_slug)

    update_sql = f"""
        UPDATE collection_items
        SET {", ".join(update_columns)}
        WHERE object_slug = ?
    """

    conn = get_db_connection()
    conn.execute(update_sql, update_values)
    conn.commit()
    conn.close()

    return RedirectResponse(url=f"/item/{object_slug}", status_code=303)


@app.get("/admin/create", response_class=HTMLResponse)
def create_item_page(
    admin_user=Depends(require_admin),
):
    return HTMLResponse(render_create_page())


@app.post("/admin/create")
async def create_item(
    request: Request,
    uploaded_image: Optional[UploadFile] = File(None),
    admin_user=Depends(require_admin),
):
    editable_columns = get_editable_columns()
    form_data = await request.form()

    generated_object_id = build_create_object_id_from_form(form_data)

    form_data_dict = dict(form_data)
    form_data_dict["OBJECTID"] = generated_object_id

    base_slug = build_base_slug_from_form(form_data_dict)
    object_slug = make_unique_object_slug(base_slug)

    insert_columns = ["object_slug"]
    insert_values = [object_slug]

    for column in editable_columns:
        if column == "image_filename":
            continue

        if column == "OBJECTID":
            insert_columns.append(column)
            insert_values.append(generated_object_id)
        else:
            insert_columns.append(column)
            insert_values.append(str(form_data.get(column, "")))

    if "image_filename" in editable_columns:
        if uploaded_image and uploaded_image.filename:
            saved_image_filename = save_uploaded_image(uploaded_image, object_slug)
            image_filename_value = saved_image_filename
        else:
            image_filename_value = str(form_data.get("image_filename", ""))

        insert_columns.append("image_filename")
        insert_values.append(image_filename_value)

    placeholders = ", ".join(["?" for _ in insert_columns])
    quoted_columns = ", ".join([quote_identifier(column) for column in insert_columns])

    insert_sql = f"""
        INSERT INTO collection_items ({quoted_columns})
        VALUES ({placeholders})
    """

    conn = get_db_connection()

    try:
        conn.execute(insert_sql, insert_values)
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create record in database: {exc}",
        )
    finally:
        conn.close()

    try:
        generate_qr_code_for_record(
            object_slug=object_slug,
            base_url=get_request_base_url(request),
            output_qr_dir=QR_CODES_DIR,
            output_html_dir=OUTPUT_HTML_DIR,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Record was created, but QR code generation failed for "
                f"{generated_object_id}: {exc}"
            ),
        )

    return RedirectResponse(url=f"/item/{object_slug}", status_code=303)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
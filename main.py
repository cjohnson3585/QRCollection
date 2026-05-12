from pathlib import Path
import os
import secrets
import shutil
import sqlite3
from typing import Optional
from urllib.parse import urlencode

import uvicorn
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent

DB_PATH = BASE_DIR / "qr_collection.db"
RAW_IMAGES_DIR = BASE_DIR / "raw_images"
QR_CODES_DIR = BASE_DIR / "output_qr_codes"

ADMIN_USERNAME = os.getenv("QR_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("QR_ADMIN_PASSWORD", "admin321")

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

# These are the fields the homepage knows how to display/search.
# The app still supports raw Excel column names in the database.
HOME_COLUMNS = [
    "object_slug",
    "image_filename",
    "OBJECTID",
    "ACCESSNO",
    "OBJNAME",
    "DATE",
    "MATERIAL",
    "CONDITION",
    "COLLECTION",
    "DESCRIP",
]

HOME_SEARCH_COLUMNS = [
    "object_slug",
    "OBJECTID",
    "ACCESSNO",
    "OBJNAME",
    "DATE",
    "MATERIAL",
    "CONDITION",
    "COLLECTION",
    "DESCRIP",
]

# Make static directories exist before FastAPI mounts them.
RAW_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
QR_CODES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="QR Collection")
security = HTTPBasic()

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
    """Safely quote a SQLite table or column identifier."""
    safe_name = str(name).replace('"', '""')
    return f'"{safe_name}"'


def get_db_connection() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail="Database not found. Run bulk_qr_code_generator.py first.",
        )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
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


def get_collection_columns() -> list[str]:
    conn = get_db_connection()
    columns = conn.execute("PRAGMA table_info(collection_items)").fetchall()
    conn.close()
    return [col["name"] for col in columns]


def existing_columns(preferred_columns: list[str]) -> list[str]:
    available = set(get_collection_columns())
    return [col for col in preferred_columns if col in available]


def build_home_where_clause(search_query: str, available_search_columns: list[str]) -> tuple[str, list[str]]:
    """
    Builds a simple server-side search filter.

    q=blue searches selected homepage columns using SQLite LIKE.
    Empty search returns all records.
    """
    search_query = (search_query or "").strip()

    if not search_query:
        return "", []

    like_value = f"%{search_query}%"
    conditions = [f"{quote_identifier(col)} LIKE ?" for col in available_search_columns]
    params = [like_value] * len(conditions)

    if not conditions:
        return "", []

    return f"WHERE {' OR '.join(conditions)}", params


def get_home_items(search_query: str = "", page: int = 1, per_page: int = DEFAULT_PAGE_SIZE) -> dict:
    """
    Returns one page of homepage rows instead of loading the full table.

    This is the biggest scalability improvement. With a million records, the homepage
    should fetch only 25/50/100 rows at a time, not all rows.
    """
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    offset = (page - 1) * per_page

    available_columns = existing_columns(HOME_COLUMNS)
    available_search_columns = existing_columns(HOME_SEARCH_COLUMNS)

    if not available_columns:
        available_columns = ["*"]
        select_sql = "*"
    else:
        select_sql = ", ".join(quote_identifier(col) for col in available_columns)

    where_sql, where_params = build_home_where_clause(search_query, available_search_columns)

    available_all_columns = set(get_collection_columns())
    if "OBJECTID" in available_all_columns:
        order_sql = "ORDER BY \"OBJECTID\" COLLATE NOCASE"
    elif "id" in available_all_columns:
        order_sql = "ORDER BY id"
    else:
        order_sql = ""

    conn = get_db_connection()

    count_sql = f"SELECT COUNT(*) AS total FROM collection_items {where_sql}"
    total = conn.execute(count_sql, where_params).fetchone()["total"]

    items_sql = f"""
        SELECT {select_sql}
        FROM collection_items
        {where_sql}
        {order_sql}
        LIMIT ? OFFSET ?
    """
    items = conn.execute(items_sql, [*where_params, per_page, offset]).fetchall()
    conn.close()

    total_pages = max(1, (total + per_page - 1) // per_page)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "search_query": search_query,
    }


def get_item_by_slug(object_slug: str) -> Optional[sqlite3.Row]:
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


def get_editable_columns() -> list[str]:
    columns = get_collection_columns()
    excluded = {"id", "object_slug"}
    return [col for col in columns if col not in excluded]


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

        .edit-button,
        .small-link-button,
        .cancel-button,
        button {
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

        .edit-button {
            margin-right: 8px;
            margin-bottom: 28px;
        }

        .small-link-button {
            padding: 7px 10px;
            font-size: 13px;
            margin: 2px 0;
        }

        .edit-button:hover,
        .small-link-button:hover,
        .cancel-button:hover,
        button:hover {
            background-color: #3b3025;
        }

        .info-grid,
        .form-grid {
            display: grid;
            grid-template-columns: 180px 1fr;
            gap: 12px 16px;
            margin-bottom: 30px;
        }

        .label,
        label {
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
        return f'<img src="{image_url}" alt="{html_escape(object_id)} image">'

    return '<div class="image-placeholder">No image assigned yet</div>'


def save_uploaded_image(uploaded_file: UploadFile, object_slug: str) -> str:
    """Saves an uploaded image to raw_images/ and returns the saved filename."""
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


def url_for_home(search_query: str = "", page: int = 1, per_page: int = DEFAULT_PAGE_SIZE) -> str:
    params = {
        "q": search_query or "",
        "page": page,
        "per_page": per_page,
    }
    return f"/?{urlencode(params)}"


def render_pagination(search_query: str, page: int, per_page: int, total_pages: int, total: int) -> str:
    prev_disabled = page <= 1
    next_disabled = page >= total_pages

    prev_link = url_for_home(search_query, page - 1, per_page)
    next_link = url_for_home(search_query, page + 1, per_page)

    prev_html = (
        '<span class="pagination-disabled">Previous</span>'
        if prev_disabled
        else f'<a class="pagination-link" href="{prev_link}">Previous</a>'
    )

    next_html = (
        '<span class="pagination-disabled">Next</span>'
        if next_disabled
        else f'<a class="pagination-link" href="{next_link}">Next</a>'
    )

    return f"""
        <div class="pagination-row">
            <div>
                Showing page {page} of {total_pages} · {total} matching record(s)
            </div>
            <div class="pagination-actions">
                {prev_html}
                {next_html}
            </div>
        </div>
    """


def render_home_table(home_data: dict) -> str:
    """
    Renders the homepage table.

    Important scalability change:
    - The QR column does NOT use <img src="/qr_codes/...">.
    - It only renders download/open links.
    - That means the browser does not request QR PNGs for every row on page load.
    """
    items = home_data["items"]
    search_query = home_data["search_query"]
    page = home_data["page"]
    per_page = home_data["per_page"]
    total = home_data["total"]
    total_pages = home_data["total_pages"]

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
        qr_filename = f"{object_slug}_qr.png"
        qr_url = f"/qr_codes/{qr_filename}"

        if image_filename:
            image_html = f"""
                <a class="small-link-button" href="/raw_images/{html_escape(image_filename)}" target="_blank" rel="noopener">
                    View Image
                </a>
            """
        else:
            image_html = '<div class="thumbnail-placeholder">No Image</div>'

        rows_html += f"""
            <tr>
                <td>{image_html}</td>
                <td>
                    <a class="small-link-button" href="{qr_url}" download>
                        Download QR
                    </a>
                    <br>
                    <a class="table-link" href="{qr_url}" target="_blank" rel="noopener">
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
                    <a class="table-link" href="{record_url}">
                        View Record
                    </a>
                </td>
            </tr>
        """

    if not rows_html:
        rows_html = """
            <tr>
                <td colspan="9">No matching records found.</td>
            </tr>
        """

    pagination_html = render_pagination(search_query, page, per_page, total_pages, total)

    per_page_options = ""
    for option in [10, 25, 50, 100]:
        selected = "selected" if option == per_page else ""
        per_page_options += f'<option value="{option}" {selected}>{option}</option>'

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>QR Collection - All Records</title>
    <style>
        {base_css()}

        .table-container {{
            padding: 35px;
            overflow-x: auto;
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

        .filter-box {{
            margin-bottom: 24px;
            padding: 18px;
            background: #faf7f2;
            border: 1px solid #eadfce;
            border-radius: 10px;
        }}

        .filter-form {{
            display: grid;
            grid-template-columns: 1fr 140px auto auto;
            gap: 10px;
            align-items: end;
        }}

        .filter-field label {{
            display: block;
            margin-bottom: 5px;
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

        .thumbnail-placeholder {{
            width: 75px;
            height: 40px;
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

        .table-link {{
            color: #4b3b2a;
            font-weight: bold;
            text-decoration: none;
            font-size: 13px;
        }}

        .table-link:hover {{
            text-decoration: underline;
        }}

        .pagination-row {{
            display: flex;
            justify-content: space-between;
            gap: 16px;
            align-items: center;
            margin: 18px 0;
            color: #5c4a38;
            flex-wrap: wrap;
        }}

        .pagination-actions {{
            display: flex;
            gap: 10px;
            align-items: center;
        }}

        .pagination-link,
        .pagination-disabled {{
            display: inline-block;
            padding: 8px 12px;
            border-radius: 8px;
            border: 1px solid #b89b72;
            text-decoration: none;
        }}

        .pagination-link {{
            background: #4b3b2a;
            color: white;
        }}

        .pagination-disabled {{
            background: #eee6da;
            color: #9b8b7a;
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
            <h2 class="records-title">All Collection Records</h2>
            <div class="records-subtitle">
                Search records, open record pages, and download QR codes only when needed.
            </div>

            <div class="filter-box">
                <form class="filter-form" method="get" action="/">
                    <div class="filter-field">
                        <label for="q">Filter records</label>
                        <input
                            id="q"
                            name="q"
                            value="{html_escape(search_query)}"
                            placeholder="Search object ID, accession no., name, material, condition, description..."
                        >
                    </div>

                    <div class="filter-field">
                        <label for="per_page">Rows per page</label>
                        <select id="per_page" name="per_page">
                            {per_page_options}
                        </select>
                    </div>

                    <input type="hidden" name="page" value="1">
                    <button type="submit">Search</button>
                    <a class="cancel-button" href="/">Clear</a>
                </form>
            </div>

            {pagination_html}

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

            {pagination_html}
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

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
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

                <a class="edit-button" href="/admin/edit/{object_slug}">
                    Edit this Record
                </a>

                <a class="edit-button" href="/">
                    Back to All Records
                </a>

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
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
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


@app.get("/", response_class=HTMLResponse)
def index(q: str = "", page: int = 1, per_page: int = DEFAULT_PAGE_SIZE):
    home_data = get_home_items(search_query=q, page=page, per_page=per_page)
    return HTMLResponse(render_home_table(home_data))


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
    admin_user: str = Depends(require_admin),
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
    admin_user: str = Depends(require_admin),
):
    item = get_item_by_slug(object_slug)

    if not item:
        raise HTTPException(status_code=404, detail="Collection item not found")

    editable_columns = get_editable_columns()
    form_data = await request.form()

    update_columns = []
    update_values = []

    for column in editable_columns:
        # uploaded_image is handled separately.
        if column == "image_filename":
            continue

        update_columns.append(f"{quote_identifier(column)} = ?")
        update_values.append(str(form_data.get(column, "")))

    if uploaded_image and uploaded_image.filename:
        saved_image_filename = save_uploaded_image(uploaded_image, object_slug)
        update_columns.append(f"{quote_identifier('image_filename')} = ?")
        update_values.append(saved_image_filename)
    elif "image_filename" in editable_columns:
        update_columns.append(f"{quote_identifier('image_filename')} = ?")
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


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )

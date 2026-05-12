import argparse
import json
import os
import re
import sqlite3
from pathlib import Path

import pandas as pd
import qrcode


DB_NAME = "qr_collection.db"


def clean_value(value) -> str:
    """
    Converts Excel/pandas values into clean strings for SQLite/HTML.
    """
    if pd.isna(value):
        return ""

    return str(value).strip()


def make_slug(value: str, fallback: str) -> str:
    """
    Converts an object id like '302 C-0007' into '302_C-0007'.
    """
    value = clean_value(value)

    if not value:
        value = fallback

    value = value.strip()
    value = value.replace(" ", "_")

    # Keep letters, numbers, underscores, and hyphens.
    value = re.sub(r"[^A-Za-z0-9_-]", "", value)

    return value


def quote_identifier(name: str) -> str:
    """
    Safely quotes SQLite column/table identifiers.
    Keeps original Excel field names exactly as column names.
    """
    safe_name = name.replace('"', '""')
    return f'"{safe_name}"'


def load_excel_records(excel_path: Path, limit: int = 10) -> list[dict]:
    """
    Loads records from the Excel file and returns a list of dictionaries.

    Field names are kept exactly as they appear in the Excel sheet,
    except leading/trailing whitespace is stripped from the column names.
    """
    df = pd.read_excel(excel_path, engine="openpyxl")

    # Keep names basically the same, but strip accidental leading/trailing spaces.
    df.columns = [str(col).strip() for col in df.columns]

    # Limit to first 10 records for testing.
    df = df.head(limit)

    records = []

    for index, row in df.iterrows():
        record = {}

        for col in df.columns:
            record[col] = clean_value(row[col])

        object_id = record.get("OBJECTID", "") or record.get("Object ID", "")
        object_slug = make_slug(object_id, fallback=f"record_{index + 1}")

        # Support columns needed by the app/generator.
        # image_filename intentionally stays blank for now per your request.
        record["object_slug"] = object_slug
        record["image_filename"] = ""

        records.append(record)

    return records


def create_or_update_database(db_path: Path, records: list[dict]):
    """
    Creates/updates SQLite database using the Excel field names as database columns.

    It creates one table:
        collection_items

    The table includes:
        id
        object_slug
        image_filename
        all Excel columns exactly as field names
    """

    if not records:
        raise ValueError("No records found in Excel file.")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Gather all columns from all records.
    all_columns = []
    seen = set()

    for record in records:
        for key in record.keys():
            if key not in seen:
                seen.add(key)
                all_columns.append(key)

    # Ensure support columns are first.
    ordered_columns = ["object_slug", "image_filename"]
    for col in all_columns:
        if col not in ordered_columns:
            ordered_columns.append(col)

    column_defs = []

    for col in ordered_columns:
        if col == "object_slug":
            column_defs.append(f'{quote_identifier(col)} TEXT UNIQUE NOT NULL')
        else:
            column_defs.append(f'{quote_identifier(col)} TEXT')

    create_sql = f"""
        CREATE TABLE IF NOT EXISTS collection_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {", ".join(column_defs)}
        )
    """

    cursor.execute(create_sql)

    # If table already exists from an older run, add any missing columns.
    existing_cols = cursor.execute("PRAGMA table_info(collection_items)").fetchall()
    existing_col_names = {col[1] for col in existing_cols}

    for col in ordered_columns:
        if col not in existing_col_names:
            cursor.execute(
                f"""
                ALTER TABLE collection_items
                ADD COLUMN {quote_identifier(col)} TEXT
                """
            )

    insert_columns = ordered_columns
    quoted_insert_columns = [quote_identifier(col) for col in insert_columns]

    placeholders = ", ".join(["?"] * len(insert_columns))

    update_columns = [
        col for col in insert_columns
        if col != "object_slug"
    ]

    update_sql = ", ".join(
        [
            f"{quote_identifier(col)} = excluded.{quote_identifier(col)}"
            for col in update_columns
        ]
    )

    insert_sql = f"""
        INSERT INTO collection_items (
            {", ".join(quoted_insert_columns)}
        )
        VALUES ({placeholders})
        ON CONFLICT(object_slug) DO UPDATE SET
            {update_sql}
    """

    for record in records:
        values = [record.get(col, "") for col in insert_columns]
        cursor.execute(insert_sql, values)

    conn.commit()
    conn.close()


def get_display_value(data: dict, key: str, default: str = "") -> str:
    return clean_value(data.get(key, default))


def save_showcase_page(data: dict, output_html: Path):
    """
    Creates a static HTML page for one object.

    The static HTML page is useful for compatibility with:
        /scan/{object_slug}_showcase.html

    main.py can still redirect that route to:
        /item/{object_slug}
    """

    output_html.mkdir(parents=True, exist_ok=True)

    uid = data["object_slug"]
    showcase_path = output_html / f"{uid}_showcase.html"

    object_id = get_display_value(data, "OBJECTID", uid)
    obj_name = get_display_value(data, "OBJNAME", "Collection Item")
    item_date = get_display_value(data, "DATE", "")
    description = get_display_value(data, "DESCRIP", "")

    image_filename = get_display_value(data, "image_filename", "")
    image_html = ""

    if image_filename:
        image_url = f"/raw_images/{image_filename}"
        image_html = f'<img src="{image_url}" alt="Costume Image">'
    else:
        image_html = """
        <div style="
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
        ">
            No image assigned yet
        </div>
        """

    # Show all Excel fields, except internal/support fields and description.
    skip_keys = {"object_slug", "image_filename", "DESCRIP"}

    fields_html = ""

    for key, value in data.items():
        if key in skip_keys:
            continue

        fields_html += f"""
                    <div class="label">{key}</div>
                    <div class="value">{clean_value(value)}</div>
        """

    html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>QR Collection - {object_id}</title>
    <style>
        body {{
            margin: 0;
            font-family: Georgia, "Times New Roman", serif;
            background-color: #f6f2eb;
            color: #2f2a24;
        }}

        header {{
            background-color: #4b3b2a;
            color: white;
            padding: 24px 40px;
            text-align: center;
            border-bottom: 4px solid #b89b72;
        }}

        header h1 {{
            margin: 0;
            font-size: 32px;
            letter-spacing: 1px;
        }}

        header p {{
            margin: 8px 0 0;
            font-size: 16px;
            color: #e9dcc9;
        }}

        .container {{
            max-width: 1200px;
            margin: 40px auto;
            background: white;
            border-radius: 14px;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
            overflow: hidden;
        }}

        .content {{
            display: flex;
            flex-wrap: wrap;
        }}

        .image-section {{
            flex: 1 1 420px;
            background-color: #f3ede4;
            padding: 30px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .image-section img {{
            max-width: 100%;
            max-height: 600px;
            border-radius: 10px;
            border: 1px solid #d7c8b3;
            box-shadow: 0 4px 10px rgba(0, 0, 0, 0.08);
        }}

        .details-section {{
            flex: 1 1 500px;
            padding: 35px;
        }}

        .item-title {{
            margin-top: 0;
            margin-bottom: 8px;
            font-size: 34px;
            color: #3b3025;
        }}

        .item-subtitle {{
            font-size: 18px;
            color: #7a6a58;
            margin-bottom: 28px;
        }}

        .edit-button {{
            display: inline-block;
            margin-bottom: 28px;
            padding: 10px 16px;
            background-color: #4b3b2a;
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-size: 14px;
            border: 1px solid #b89b72;
        }}

        .edit-button:hover {{
            background-color: #3b3025;
        }}

        .info-grid {{
            display: grid;
            grid-template-columns: 180px 1fr;
            gap: 12px 16px;
            margin-bottom: 30px;
        }}

        .label {{
            font-weight: bold;
            color: #5c4a38;
        }}

        .value {{
            color: #2f2a24;
        }}

        .description-box {{
            margin-top: 20px;
            padding: 20px;
            background-color: #faf7f2;
            border-left: 5px solid #b89b72;
            border-radius: 8px;
        }}

        .description-box h3 {{
            margin-top: 0;
            color: #4b3b2a;
        }}

        .description-box p {{
            margin-bottom: 0;
            line-height: 1.7;
        }}

        footer {{
            background-color: #4b3b2a;
            color: #f3e7d7;
            text-align: center;
            padding: 18px 20px;
            font-size: 14px;
            border-top: 4px solid #b89b72;
        }}

        @media (max-width: 900px) {{
            .content {{
                flex-direction: column;
            }}

            .details-section {{
                padding: 25px;
            }}

            .info-grid {{
                grid-template-columns: 1fr;
            }}

            .label {{
                margin-top: 10px;
            }}
        }}
    </style>
</head>
<body>

    <header>
        <h1>Historic Costume and Textiles Collection</h1>
        <p>QR Collection Object Record</p>
    </header>

    <div class="container">
        <div class="content">
            <div class="image-section">
                {image_html}
            </div>

            <div class="details-section">
                <h2 class="item-title">{object_id}</h2>
                <div class="item-subtitle">{obj_name} · {item_date}</div>

                <a class="edit-button" href="/admin/edit/{uid}">
                    Edit this Record
                </a>

                <div class="info-grid">
                    {fields_html}
                </div>

                <div class="description-box">
                    <h3>Description</h3>
                    <p>{description}</p>
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

    with open(showcase_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return showcase_path


def generate_qr_code(url: str, output_path: Path):
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


def main():
    parser = argparse.ArgumentParser(description="Bulk QR Collection Generator")

    parser.add_argument(
        "--excel",
        default="PPSdataUpdated.xlsx",
        help="Path to Excel file containing collection records",
    )

    parser.add_argument(
        "--base_url",
        default="http://23.20.253.156:8000",
        help="Base URL of the FastAPI app",
    )

    parser.add_argument(
        "--output_dir",
        default="output_qr_codes",
        help="Directory to store QR codes",
    )

    parser.add_argument(
        "--output_html",
        default="output_html",
        help="Directory to store generated HTML pages",
    )

    parser.add_argument(
        "--db",
        default=DB_NAME,
        help="SQLite database path",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of Excel records to ingest for testing",
    )

    args = parser.parse_args()

    excel_path = Path(args.excel)
    db_path = Path(args.db)
    output_dir = Path(args.output_dir)
    output_html = Path(args.output_html)

    if not excel_path.exists():
        raise FileNotFoundError(f"Excel file not found: {excel_path.resolve()}")

    print(f"Loading Excel file: {excel_path.resolve()}")
    records = load_excel_records(excel_path=excel_path, limit=args.limit)

    print(f"Loaded {len(records)} records.")

    print("Creating/updating SQLite database...")
    create_or_update_database(
        db_path=db_path,
        records=records,
    )

    generated_outputs = []

    for record in records:
        uid = record["object_slug"]

        print(f"Generating HTML and QR for: {uid}")

        showcase_path = save_showcase_page(
            data=record,
            output_html=output_html,
        )

        showcase_filename = showcase_path.name

        # Keep QR compatible with your existing /scan/{filename} route.
        qr_url = f"{args.base_url}/scan/{showcase_filename}"

        qr_output = output_dir / f"{uid}_qr.png"

        generate_qr_code(qr_url, qr_output)

        generated_outputs.append(
            {
                "object_slug": uid,
                "showcase_html": str(showcase_path),
                "qr_code": str(qr_output),
                "qr_url": qr_url,
            }
        )

    print()
    print("Done.")
    print(f"Database: {db_path.resolve()}")
    print(f"HTML output directory: {output_html.resolve()}")
    print(f"QR output directory: {output_dir.resolve()}")
    print()
    print("Generated records:")
    print(json.dumps(generated_outputs, indent=4))


if __name__ == "__main__":
    main()
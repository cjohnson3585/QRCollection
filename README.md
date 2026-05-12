# QR Collection

A small FastAPI + SQLite application for publishing Historic Costume and Textiles Collection records as QR-code-accessible object pages.

The project ingests collection data from an Excel workbook, creates a local SQLite database, generates one QR code per collection object, and serves a web interface where users can browse records, scan QR codes, view individual object pages, and edit records through a password-protected admin page.

---

## What This Project Does

This app is designed for a museum, archive, costume collection, or similar collection-management workflow where each physical object needs a public-facing QR code.

The workflow is:

1. Start with an Excel file of collection records.
2. Run `bulk_qr_code_generator.py`.
3. The script creates or updates `qr_collection.db`.
4. The script generates QR code images into `output_qr_codes/`.
5. Run `main.py` to start the FastAPI web app.
6. Visit the homepage to browse all records and download QR codes.
7. Scan a QR code to open the matching object record page.
8. Use the protected admin edit page to update records and upload object images.

---

## Included Files

```text
.
├── bulk_qr_code_generator.py   # Builds the SQLite DB and generates QR codes from Excel
├── main.py                     # FastAPI web app for browsing, viewing, editing, and image uploads
├── PPSdataUpdated.xlsx         # Excel collection dataset
└── README.md                   # Project documentation
```

Generated after running the scripts:

```text
.
├── qr_collection.db            # SQLite database generated from the Excel file
├── output_qr_codes/            # Generated QR code PNG files
├── output_html/                # Generated static HTML compatibility pages
└── raw_images/                 # Uploaded object images
```

---

## Data Source

The project expects an Excel workbook named:

```text
PPSdataUpdated.xlsx
```

The current workbook contains collection-style fields such as:

```text
OBJECTID
ACCESSNO
OBJNAME
DATE
DESCRIP
MATERIAL
CONDITION
CATBY
CATDATE
COLLECTION
IMAGEFILE
```

The code intentionally keeps the raw Excel column names as SQLite column names. This means fields such as `OBJECTID`, `ACCESSNO`, `OBJNAME`, and `DESCRIP` are used directly rather than being renamed to normalized Python-style names.

The generator adds two internal support fields:

```text
object_slug
image_filename
```

`object_slug` is derived from the object ID and is used in URLs and QR filenames. For example:

```text
304 A-0001 -> 304_A-0001
```

`image_filename` starts blank and is later populated when an image is uploaded through the admin edit page.

---

## Requirements

Recommended Python version:

```text
Python 3.9+
```

The current `main.py` uses `typing.Optional` instead of the newer `UploadFile | None` syntax, so it is compatible with Python 3.9 environments commonly used on EC2.

Install these Python packages:

```bash
pip install fastapi uvicorn pandas openpyxl qrcode python-multipart pillow
```

Package purpose:

| Package | Purpose |
|---|---|
| `fastapi` | Web framework |
| `uvicorn` | ASGI server for running FastAPI |
| `pandas` | Reads Excel data |
| `openpyxl` | Excel engine used by pandas |
| `qrcode` | Generates QR code PNG files |
| `python-multipart` | Required for FastAPI file uploads and form handling |
| `pillow` | Image backend used by QR/image libraries |

Optional `requirements.txt`:

```text
fastapi
uvicorn
pandas
openpyxl
qrcode
python-multipart
pillow
```

Install from it with:

```bash
pip install -r requirements.txt
```

---

## Setup

### 1. Create a project directory

```bash
mkdir qr_collection
cd qr_collection
```

Place these files in the directory:

```text
bulk_qr_code_generator.py
main.py
PPSdataUpdated.xlsx
```

### 2. Create and activate a virtual environment

On macOS/Linux:

```bash
python3 -m venv venv
source venv/bin/activate
```

On Windows PowerShell:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install fastapi uvicorn pandas openpyxl qrcode python-multipart pillow
```

### 4. Create required directories

The app and generator create some directories automatically, but it is safe to create them manually:

```bash
mkdir -p raw_images output_qr_codes output_html
```

---

## Generate the Database and QR Codes

Run the generator first:

```bash
python3 bulk_qr_code_generator.py
```

By default, the generator:

- Reads `PPSdataUpdated.xlsx`
- Loads the first 10 records for testing
- Creates or updates `qr_collection.db`
- Creates static HTML compatibility files in `output_html/`
- Creates QR code PNG files in `output_qr_codes/`
- Uses this default base URL:

```text
http://23.20.253.156:8000
```

### Generate more than 10 records

The default is intentionally limited to 10 records for testing. To generate all records, use a large limit:

```bash
python3 bulk_qr_code_generator.py --limit 5000
```

Or set a specific number:

```bash
python3 bulk_qr_code_generator.py --limit 100
```

### Use your own public server URL

The QR code URL must point to the public URL where the FastAPI app will run.

Example for an EC2 public IP:

```bash
python3 bulk_qr_code_generator.py \
  --excel PPSdataUpdated.xlsx \
  --base_url http://YOUR_EC2_PUBLIC_IP:8000 \
  --limit 5000
```

Example for a domain name:

```bash
python3 bulk_qr_code_generator.py \
  --excel PPSdataUpdated.xlsx \
  --base_url https://collection.example.org \
  --limit 5000
```

### Generator command options

```bash
python3 bulk_qr_code_generator.py \
  --excel PPSdataUpdated.xlsx \
  --base_url http://127.0.0.1:8000 \
  --output_dir output_qr_codes \
  --output_html output_html \
  --db qr_collection.db \
  --limit 10
```

| Option | Default | Description |
|---|---:|---|
| `--excel` | `PPSdataUpdated.xlsx` | Excel file to ingest |
| `--base_url` | `http://23.20.253.156:8000` | Public base URL embedded in QR codes |
| `--output_dir` | `output_qr_codes` | Directory for generated QR PNGs |
| `--output_html` | `output_html` | Directory for generated static HTML pages |
| `--db` | `qr_collection.db` | SQLite database path |
| `--limit` | `10` | Number of Excel rows to ingest |

---

## Run the Web App

After generating the database and QR codes, start the app:

```bash
python3 main.py
```

The app runs on:

```text
http://0.0.0.0:8000
```

From your local computer, open:

```text
http://127.0.0.1:8000
```

On EC2, open:

```text
http://YOUR_EC2_PUBLIC_IP:8000
```

---

## Web Routes

| Route | Purpose |
|---|---|
| `/` | Main table of all collection records |
| `/item/{object_slug}` | Public object detail page |
| `/scan/{object_slug}_showcase.html` | QR-compatible route that redirects to `/item/{object_slug}` |
| `/admin/edit/{object_slug}` | Password-protected edit page |
| `/raw_images/{filename}` | Serves uploaded object images |
| `/qr_codes/{filename}` | Serves generated QR code images |

Example object page:

```text
/item/304_A-0001
```

Example QR scan URL:

```text
/scan/304_A-0001_showcase.html
```

Example QR code image:

```text
/qr_codes/304_A-0001_qr.png
```

---

## Admin Login

The admin edit page uses HTTP Basic Authentication.

Default credentials:

```text
Username: admin
Password: admin321
```

Change these before deploying publicly.

Set environment variables:

```bash
export QR_ADMIN_USERNAME="your_admin_username"
export QR_ADMIN_PASSWORD="your_strong_password"
```

Then start the app:

```bash
python3 main.py
```

On EC2, you can put those exports in your shell profile, service file, or deployment script.

---

## Editing Records

To edit a record:

1. Go to the homepage.
2. Click the record page link, or scan the QR code.
3. Click **Edit this Record**.
4. Enter the admin username and password.
5. Edit the fields.
6. Click **Save Changes**.

The edit form is generated dynamically from the SQLite table columns. It excludes only:

```text
id
object_slug
```

All other fields can be edited, including the raw Excel fields and `image_filename`.

---

## Uploading Images

Images can be uploaded only from the authenticated admin edit page.

Supported image types:

```text
.jpg
.jpeg
.png
.webp
```

When an image is uploaded:

1. The file is saved into `raw_images/`.
2. The filename is renamed to match the object slug.
3. The `image_filename` field in SQLite is updated.
4. The object page displays the uploaded image.

Example:

```text
Object slug: 304_A-0001
Uploaded file: my-photo.jpg
Saved as: raw_images/304_A-0001.jpg
Database image_filename: 304_A-0001.jpg
```

If no image is assigned, the public item page shows:

```text
No image assigned yet
```

---

## QR Code Behavior

The generated QR codes point to this pattern:

```text
{base_url}/scan/{object_slug}_showcase.html
```

Example:

```text
http://YOUR_SERVER:8000/scan/304_A-0001_showcase.html
```

The FastAPI app then redirects that scan URL to:

```text
/item/304_A-0001
```

This keeps the QR code format compatible with the static HTML pages while allowing the live FastAPI app to serve the latest database-backed record.

---

## Database Design

The generator creates a SQLite database named:

```text
qr_collection.db
```

It creates one table:

```text
collection_items
```

The table includes:

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
object_slug TEXT UNIQUE NOT NULL
image_filename TEXT
```

Then it adds all Excel columns as `TEXT` columns using the original Excel field names.

This design keeps the database simple and avoids breaking the app when the Excel workbook contains many legacy collection-management fields.

---

## Development Notes

### Why raw Excel column names are used

The code is intentionally written to preserve the collection spreadsheet structure. Instead of renaming every column, it stores the original field names directly in SQLite.

This makes the app easier to compare against the source Excel file and avoids losing meaning from collection-specific field names.

### Why most fields are stored as text

The collection data includes mixed formats: dates, object IDs, notes, empty cells, numeric-looking accession numbers, and descriptive fields. Storing fields as text avoids accidental data loss or formatting changes.

### Why `object_slug` exists

`OBJECTID` values may contain spaces or special characters. URLs and filenames are easier to handle when each object has a safe slug.

For example:

```text
302 C-0007 -> 302_C-0007
```

---

## EC2 Deployment

### 1. Copy project files to EC2

From your local machine:

```bash
scp -i your-key.pem bulk_qr_code_generator.py main.py PPSdataUpdated.xlsx ec2-user@YOUR_EC2_PUBLIC_IP:/home/ec2-user/qr_collection/
```

If the folder does not exist yet:

```bash
ssh -i your-key.pem ec2-user@YOUR_EC2_PUBLIC_IP
mkdir -p /home/ec2-user/qr_collection
exit
```

### 2. SSH into EC2

```bash
ssh -i your-key.pem ec2-user@YOUR_EC2_PUBLIC_IP
cd /home/ec2-user/qr_collection
```

### 3. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 4. Install dependencies

```bash
pip install fastapi uvicorn pandas openpyxl qrcode python-multipart pillow
```

### 5. Generate the database and QR codes using the EC2 URL

```bash
python3 bulk_qr_code_generator.py \
  --base_url http://YOUR_EC2_PUBLIC_IP:8000 \
  --limit 5000
```

### 6. Run the app

```bash
python3 main.py
```

### 7. Open port 8000 in the EC2 security group

In the AWS console, add an inbound rule:

| Type | Protocol | Port | Source |
|---|---|---:|---|
| Custom TCP | TCP | `8000` | Your IP or `0.0.0.0/0` for public testing |

For production, prefer a domain name, HTTPS, and a reverse proxy such as Nginx.

---

## Running With Uvicorn Directly

Instead of:

```bash
python3 main.py
```

You can run:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

For development with reload:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Do not use `--reload` for production.

---

## Optional systemd Service for EC2

Create a service file:

```bash
sudo nano /etc/systemd/system/qr-collection.service
```

Example service:

```ini
[Unit]
Description=QR Collection FastAPI App
After=network.target

[Service]
User=ec2-user
WorkingDirectory=/home/ec2-user/qr_collection
Environment="QR_ADMIN_USERNAME=admin"
Environment="QR_ADMIN_PASSWORD=change-this-password"
ExecStart=/home/ec2-user/qr_collection/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable qr-collection
sudo systemctl start qr-collection
```

Check status:

```bash
sudo systemctl status qr-collection
```

View logs:

```bash
journalctl -u qr-collection -f
```

---

## Troubleshooting

### `Database not found. Run bulk_qr_code_generator.py first.`

Cause: `main.py` cannot find `qr_collection.db`.

Fix:

```bash
python3 bulk_qr_code_generator.py --limit 5000
python3 main.py
```

Make sure both scripts are in the same directory.

---

### `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`

Cause: Older Python versions do not support this type syntax:

```python
UploadFile | None
```

Fix: Use this compatible syntax instead:

```python
from typing import Optional
uploaded_image: Optional[UploadFile] = File(None)
```

The current `main.py` already uses `Optional[UploadFile]`.

---

### `RuntimeError: Form data requires "python-multipart" to be installed`

Cause: FastAPI requires `python-multipart` for form data and file uploads.

Fix:

```bash
pip install python-multipart
```

---

### Uploaded images do not appear

Check the following:

1. Confirm the image exists in `raw_images/`.
2. Confirm the database field `image_filename` is populated.
3. Confirm the app is mounting `/raw_images`.
4. Restart the app if needed.

Example check:

```bash
ls -lah raw_images
```

---

### QR codes point to the wrong server

Cause: QR codes were generated with the wrong `--base_url`.

Fix: Re-run the generator with the correct public URL:

```bash
python3 bulk_qr_code_generator.py \
  --base_url http://YOUR_CORRECT_SERVER:8000 \
  --limit 5000
```

Then use the newly generated QR code images from `output_qr_codes/`.

---

### App is running but not reachable on EC2

Check:

1. Uvicorn is listening on `0.0.0.0`, not `127.0.0.1`.
2. EC2 security group allows inbound TCP traffic on port `8000`.
3. The EC2 instance firewall is not blocking the port.
4. You are using the correct public IP or domain.

Run:

```bash
curl http://127.0.0.1:8000
```

If that works on the EC2 instance but not from your browser, the issue is probably the EC2 security group.

---

### Excel changes are not showing up

Re-run the generator:

```bash
python3 bulk_qr_code_generator.py --limit 5000
```

The generator uses `ON CONFLICT(object_slug) DO UPDATE`, so records with the same `object_slug` are updated instead of duplicated.

---

## Backup Recommendations

The most important files to back up are:

```text
PPSdataUpdated.xlsx
qr_collection.db
raw_images/
output_qr_codes/
```

A simple backup command:

```bash
tar -czvf qr_collection_backup.tar.gz \
  PPSdataUpdated.xlsx \
  qr_collection.db \
  raw_images \
  output_qr_codes
```

Restore with:

```bash
tar -xzvf qr_collection_backup.tar.gz
```

---

## Recommended Production Improvements

Before using this app publicly long-term, consider adding:

1. HTTPS with Nginx and Certbot.
2. Strong admin credentials stored as environment variables.
3. A proper process manager such as `systemd`.
4. Automated backups of `qr_collection.db` and `raw_images/`.
5. A search box on the homepage.
6. Pagination for large collections.
7. Better audit logging for admin edits.
8. User roles if multiple staff members will edit records.
9. Image resizing or thumbnail generation.
10. A CSV/Excel export of the updated database.

---

## Quick Start

For local testing:

```bash
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn pandas openpyxl qrcode python-multipart pillow
python3 bulk_qr_code_generator.py --base_url http://127.0.0.1:8000 --limit 10
python3 main.py
```

Open:

```text
http://127.0.0.1:8000
```

For EC2 testing:

```bash
python3 bulk_qr_code_generator.py --base_url http://YOUR_EC2_PUBLIC_IP:8000 --limit 5000
python3 main.py
```

Open:

```text
http://YOUR_EC2_PUBLIC_IP:8000
```

---

## Project Summary

`QR Collection` turns an Excel-based collection inventory into a web-accessible QR code system. The generator script creates the database, static compatibility pages, and QR codes. The FastAPI app serves the collection records, displays object images, allows QR downloads, and provides a protected editing workflow for updating records and uploading images.

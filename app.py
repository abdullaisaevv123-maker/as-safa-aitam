import html
import io
import os
import sqlite3
from pathlib import Path
from datetime import datetime
import cv2
import fitz
import numpy as np
import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageOps
from flask import Flask, jsonify, render_template, request, send_from_directory, url_for

# ============================================================
# ПАПКАЛАР
# ============================================================
BASE = Path(__file__).resolve().parent
TEMPLATE = BASE / "template.pdf"
GENERATED = BASE / "generated"
GENERATED.mkdir(exist_ok=True)
DATABASE = BASE / "beneficiaries.db"

# ============================================================
# FLASK
# ============================================================
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

# ============================================================
# SQLITE
# ============================================================
def get_db():
    conn = sqlite3.connect(str(DATABASE), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS beneficiaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_number TEXT NOT NULL UNIQUE,
                sponsor TEXT,
                beneficiary TEXT,
                beneficiary_number TEXT,
                internal_number TEXT,
                birth_date TEXT,
                gender TEXT,
                health TEXT,
                class_name TEXT,
                future_wish TEXT,
                impact TEXT,
                notes TEXT,
                link TEXT,
                pdf_filename TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS takrir_links (
                file_number TEXT PRIMARY KEY,
                link TEXT NOT NULL,
                person_name TEXT,
                curator TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()

init_db()

# ============================================================
# АРИП ЖАНА АРАБЧА ФОРМАТТОО (ARABTYPE / САКАЛ-МАЖЖАЛ)
# ============================================================
def _find_font_path():
    windir = os.environ.get("WINDIR", r"C:\Windows")
    fonts_dir = os.path.join(windir, "Fonts")

    candidates = [
        BASE / "arabtype.ttf",
        BASE / "ArabType.ttf",
        BASE / "amiri-regular.ttf",
        BASE / "Amiri-Regular.ttf",
        BASE / "NotoNaskhArabic-Regular.ttf",
        BASE / "times.ttf",
        Path(fonts_dir) / "arabtype.ttf",
        Path(fonts_dir) / "trado.ttf",
        Path(fonts_dir) / "times.ttf",
        Path(fonts_dir) / "timesbd.ttf",
        Path(fonts_dir) / "arial.ttf",
        Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"),
    ]
    for path in candidates:
        p = Path(path)
        if p.exists() and p.is_file():
            return str(p.resolve())
    return None

def _value_font(size_px):
    font_path = _find_font_path()
    if font_path:
        try:
            return ImageFont.truetype(font_path, size_px)
        except Exception:
            pass
    return ImageFont.load_default()

def _has_arabic(text):
    return any(
        "\u0600" <= ch <= "\u06ff" or "\u0750" <= ch <= "\u077f" or "\u08a0" <= ch <= "\u08ff"
        for ch in str(text)
    )

def _reshape_arabic_only(text):
    """Арабча тамгаларды жана харакаттарды (сакал-мажжал) бириктирет, багытын өзгөртпөйт."""
    s = str(text or "")
    if not s:
        return ""
    if _has_arabic(s):
        try:
            import arabic_reshaper
            return arabic_reshaper.reshape(s)
        except Exception:
            return s
    return s

def _format_display_text(text):
    """Арабча тамгаларды бириктирип, визуалдык багытын (RTL) тууралайт."""
    s = str(text or "")
    if not s:
        return ""
    if _has_arabic(s):
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display
            reshaped = arabic_reshaper.reshape(s)
            return get_display(reshaped)
        except Exception:
            return s
    return s

# ============================================================
# PDF FIELD NAMES
# ============================================================
TEXT = {
    "sponsor": (0, "Text1"),
    "beneficiary": (0, "Text2"),
    "beneficiary_number": (0, "Text3"),
    "internal_number": (1, "Text4"),
    "birth_date": (1, "Date2"),
    "gender": (1, "Dropdown3"),
    "health": (1, "Dropdown4"),
    "class_name": (1, "Text9"),
    "future_wish": (1, "Text5"),
    "impact": (1, "Text6"),
    "notes": (1, "Text7"),
}

COUNTRY_VALUE = "قيرغيزستان"
YEAR_VALUE = "2027"
YEAR_FIELD = "Dropdown6_year"

IMAGES = {
    "photo10": (0, "Image1"),
    "photo1": (2, "Image3"),
    "photo2": (2, "Image2"),
    "photo3": (2, "Image4"),
    "qr_code": (2, "Image5"),
    "link_area": (2, "Text8"),
}

# ============================================================
# САПТАРГА БӨЛҮҮ ЖАНА ЖАЗУУ
# ============================================================
def _wrap_logical_lines(text, max_width_pt, size_pt, font_obj=None):
    wrapped = []
    for raw_line in str(text or "").splitlines():
        clean = raw_line.strip()
        if not clean:
            continue
        words = clean.split()
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            measured_str = _format_display_text(candidate)
            if font_obj is not None:
                try:
                    w_pt = font_obj.text_length(measured_str, fontsize=size_pt)
                except Exception:
                    w_pt = len(candidate) * size_pt * 0.55
            else:
                w_pt = len(candidate) * size_pt * 0.55

            if w_pt <= max_width_pt or not current:
                current = candidate
            else:
                wrapped.append(current)
                current = word
        if current:
            wrapped.append(current)
    return wrapped or [str(text).strip()]

def _render_value(text, width_pt, height_pt, size_pt=18, align="right", top_align=False):
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None

    scale = 4
    W = max(60, int(width_pt * scale))
    H = max(40, int(height_pt * scale))
    canvas = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    draw = ImageDraw.Draw(canvas)

    font = _value_font(max(12, int(size_pt * scale)))
    padding_x = 10 * scale
    max_width = max(20, W - (padding_x * 2))

    lines = _wrap_logical_lines(raw, max_width / scale, size_pt)
    display_lines = [_format_display_text(ln) for ln in lines]

    try:
        bbox = draw.textbbox((0, 0), _format_display_text("بِسْمِ اللَّهِ"), font=font)
        line_h = max(int(size_pt * scale * 1.15), bbox[3] - bbox[1] + int(6 * scale))
    except Exception:
        line_h = int(size_pt * scale * 1.3)

    spacing = max(int(4 * scale), int(size_pt * 0.22 * scale))
    total_h = len(display_lines) * line_h + max(0, len(display_lines) - 1) * spacing

    if top_align:
        y = 4 * scale
    else:
        y = max(2 * scale, int((H - total_h) / 2))

    for d_line in display_lines:
        try:
            line_box = draw.textbbox((0, 0), d_line, font=font)
            line_w = line_box[2] - line_box[0]
        except Exception:
            line_w = len(d_line) * (size_pt * scale * 0.55)

        if align == "right":
            x = W - padding_x - line_w
        elif align == "left":
            x = padding_x
        else:
            x = int((W - line_w) / 2)

        draw.text((x, y), d_line, font=font, fill=(20, 20, 20, 255))
        y += line_h + spacing

    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()

def _insert_vector_text(page, rect, value, size=18, align="right", top_align=False):
    text = str(value or "").strip()
    if not text:
        return True

    font_path = _find_font_path()
    if not font_path:
        return False

    try:
        fontname = "arabtype_font"
        page.insert_font(fontname=fontname, fontfile=font_path)
        font_obj = fitz.Font(fontfile=font_path)

        pad_x = 8.0
        max_w = max(24.0, rect.width - pad_x * 2)
        logical_lines = _wrap_logical_lines(text, max_w, size, font_obj=font_obj)
        shaped_lines = [_format_display_text(ln) for ln in logical_lines]
        shaped_block = "\n".join(shaped_lines)

        align_map = {
            "right": getattr(fitz, "TEXT_ALIGN_RIGHT", 2),
            "left": getattr(fitz, "TEXT_ALIGN_LEFT", 0),
            "center": getattr(fitz, "TEXT_ALIGN_CENTER", 1),
        }
        align_code = align_map.get(align, 2)

        line_h = size * 1.35
        if top_align:
            box_rect = fitz.Rect(rect.x0 + pad_x, rect.y0 + 3.0, rect.x1 - pad_x, rect.y1 - 2.0)
        else:
            est_h = line_h * max(1, len(shaped_lines))
            top_pad = max(1.0, (rect.height - est_h) / 2.0)
            box_rect = fitz.Rect(rect.x0 + pad_x, rect.y0 + top_pad, rect.x1 - pad_x, rect.y1 - 2.0)

        rc = page.insert_textbox(
            box_rect,
            shaped_block,
            fontsize=size,
            fontname=fontname,
            color=(20 / 255, 20 / 255, 20 / 255),
            align=align_code,
            overlay=True,
        )
        if rc >= 0:
            return True
    except Exception:
        pass

    return False

def put_text(page, field, value, size=18, align="right", top_align=False):
    try:
        for w in list(page.widgets() or []):
            if w.field_name == field:
                rect = fitz.Rect(w.rect)
                try:
                    page.delete_widget(w)
                except Exception:
                    pass
                if value:
                    ok = _insert_vector_text(
                        page, rect, value,
                        size=size, align=align, top_align=top_align
                    )
                    if not ok:
                        png = _render_value(
                            value, rect.width, rect.height,
                            size_pt=size, align=align, top_align=top_align
                        )
                        if png:
                            page.insert_image(rect, stream=png, keep_proportion=False, overlay=True)
                return True
    except Exception:
        pass
    return False

def put_automatic_fields(doc):
    try:
        put_text(doc[0], "Dropdown2", COUNTRY_VALUE, size=18, align="right")
        put_text(doc[0], YEAR_FIELD, YEAR_VALUE, size=18, align="right")
    except Exception:
        pass

# ============================================================
# FACE DETECTOR
# ============================================================
cascade_path = (cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
if not os.path.exists(cascade_path):
    cascade_path = os.path.join(
        os.path.dirname(cv2.__file__), "data", "haarcascade_frontalface_default.xml"
    )
FACE_CASCADE = cv2.CascadeClassifier(cascade_path)

def prepare_photo10(data):
    try:
        original = Image.open(io.BytesIO(data)).convert("RGB")
        cv_image = cv2.cvtColor(np.array(original), cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)

        faces = ()
        if not FACE_CASCADE.empty():
            faces = FACE_CASCADE.detectMultiScale(
                gray, scaleFactor=1.08, minNeighbors=5, minSize=(60, 60)
            )

        W, H = original.size
        target_ratio = 4 / 6

        if len(faces) > 0:
            faces_sorted = sorted(faces, key=lambda item: item[2] * item[3], reverse=True)
            x, y, fw, fh = faces_sorted[0]
            face_cx = x + fw / 2
            face_cy = y + fh / 2

            top_space = fh * 1.15
            bottom_space = fh * 2.35
            desired_h = top_space + fh + bottom_space
            desired_w = desired_h * target_ratio

            if desired_w > W:
                desired_w = W
                desired_h = desired_w / target_ratio
            if desired_h > H:
                desired_h = H
                desired_w = desired_h * target_ratio

            left = face_cx - desired_w / 2
            top = face_cy - fh * 0.95
            left = max(0, min(left, W - desired_w))
            top = max(0, min(top, H - desired_h))

            crop = original.crop((int(left), int(top), int(left + desired_w), int(top + desired_h)))
        else:
            current_ratio = W / H
            if current_ratio > target_ratio:
                new_w = int(H * target_ratio)
                left = (W - new_w) // 2
                crop = original.crop((left, 0, left + new_w, H))
            else:
                new_h = int(W / target_ratio)
                top = (H - new_h) // 2
                crop = original.crop((0, top, W, top + new_h))

        final = ImageOps.fit(crop, (800, 1200), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        out = io.BytesIO()
        final.save(out, "JPEG", quality=95, optimize=True)
        return out.getvalue()
    except Exception:
        return data

def put_image(page, field, data, photo10=False):
    try:
        rect = None
        for w in list(page.widgets() or []):
            if w.field_name == field:
                rect = fitz.Rect(w.rect)
                try:
                    page.delete_widget(w)
                except Exception:
                    pass
                break
        if rect is None:
            return False

        if photo10:
            data = prepare_photo10(data)

        im = Image.open(io.BytesIO(data)).convert("RGB")
        im = ImageOps.fit(
            im,
            (max(300, int(rect.width * 6)), max(300, int(rect.height * 6))),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        b = io.BytesIO()
        im.save(b, "JPEG", quality=95)
        page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1), width=0, overlay=True)
        page.insert_image(rect, stream=b.getvalue(), keep_proportion=False, overlay=True)
        return True
    except Exception:
        return False

# ============================================================
# QR КОД ЖАНА ШИЛТЕМЕ
# ============================================================
def generate_qr_bytes(text):
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    b = io.BytesIO()
    img.save(b, "JPEG", quality=95)
    return b.getvalue()

def put_qr_code(page, field, link_text):
    try:
        rect = None
        for w in list(page.widgets() or []):
            if w.field_name == field:
                rect = fitz.Rect(w.rect)
                try:
                    page.delete_widget(w)
                except Exception:
                    pass
                break
        if rect is None or not link_text:
            return False

        qr_bytes = generate_qr_bytes(link_text)
        im = Image.open(io.BytesIO(qr_bytes)).convert("RGB")
        im = ImageOps.fit(
            im,
            (max(300, int(rect.width * 6)), max(300, int(rect.height * 6))),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        b = io.BytesIO()
        im.save(b, "JPEG", quality=95)
        page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1), width=0, overlay=True)
        page.insert_image(rect, stream=b.getvalue(), keep_proportion=False, overlay=True)
        return True
    except Exception:
        return False

# ============================================================
# PDF BUILD
# ============================================================
def build(form, files):
    if not TEMPLATE.exists():
        raise FileNotFoundError("template.pdf табылган жок.")

    doc = fitz.open(str(TEMPLATE))
    form_dict = dict(form)

    if not (form_dict.get("class_name") or "").strip():
        form_dict["class_name"] = (form_dict.get("class") or "").strip()

    for k, (p, f) in TEXT.items():
        value = (form_dict.get(k) or "").strip()
        top_align = k in ("impact", "notes")
        size = 18
        if k in ("impact", "notes"):
            size = 15
        elif k == "future_wish":
            size = 17

        if p < len(doc):
            put_text(doc[p], f, value, size=size, align="right", top_align=top_align)

    put_automatic_fields(doc)

    for k, (p, f) in IMAGES.items():
        if k in ("qr_code", "link_area"):
            continue
        if k in files and p < len(doc):
            put_image(doc[p], f, files[k], photo10=(k == "photo10"))

    link = (form_dict.get("link") or "").strip()
    if link and 2 < len(doc):
        put_qr_code(doc[2], IMAGES["qr_code"][1], link)

    file_number = (form_dict.get("file_number") or "NO_NUMBER").strip()
    safe_file_number = "".join(c for c in file_number if c.isalnum() or c in ("-", "_"))
    if not safe_file_number:
        safe_file_number = "NO_NUMBER"

    filename = f"{safe_file_number}.pdf"

    out = GENERATED / filename
    doc.save(str(out), deflate=True)
    doc.close()
    return out

# ============================================================
# DATABASE SAVE
# ============================================================
def save_beneficiary(form_dict, pdf_filename):
    file_number = (form_dict.get("file_number") or "").strip()
    if not file_number:
        raise ValueError("Файл № киргизилиши керек.")

    conn = get_db()
    try:
        exists = conn.execute(
            "SELECT id FROM beneficiaries WHERE file_number = ?", (file_number,)
        ).fetchone()

        if exists:
            conn.execute("""
                UPDATE beneficiaries SET
                    sponsor = ?, beneficiary = ?, beneficiary_number = ?, internal_number = ?,
                    birth_date = ?, gender = ?, health = ?, class_name = ?, future_wish = ?,
                    impact = ?, notes = ?, link = ?, pdf_filename = ?, created_at = ?
                WHERE file_number = ?
            """, (
                form_dict.get("sponsor", ""),
                form_dict.get("beneficiary", ""),
                form_dict.get("beneficiary_number", ""),
                form_dict.get("internal_number", ""),
                form_dict.get("birth_date", ""),
                form_dict.get("gender", ""),
                form_dict.get("health", ""),
                form_dict.get("class_name", ""),
                form_dict.get("future_wish", ""),
                form_dict.get("impact", ""),
                form_dict.get("notes", ""),
                form_dict.get("link", ""),
                pdf_filename,
                datetime.now().isoformat(timespec="seconds"),
                file_number,
            ))
        else:
            conn.execute("""
                INSERT INTO beneficiaries (
                    file_number, sponsor, beneficiary, beneficiary_number, internal_number,
                    birth_date, gender, health, class_name, future_wish, impact, notes,
                    link, pdf_filename, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                file_number,
                form_dict.get("sponsor", ""),
                form_dict.get("beneficiary", ""),
                form_dict.get("beneficiary_number", ""),
                form_dict.get("internal_number", ""),
                form_dict.get("birth_date", ""),
                form_dict.get("gender", ""),
                form_dict.get("health", ""),
                form_dict.get("class_name", ""),
                form_dict.get("future_wish", ""),
                form_dict.get("impact", ""),
                form_dict.get("notes", ""),
                form_dict.get("link", ""),
                pdf_filename,
                datetime.now().isoformat(timespec="seconds"),
            ))
        conn.commit()
    finally:
        conn.close()

# ============================================================
# ROUTES
# ============================================================
@app.get("/")
def home():
    pdfs = sorted([p.name for p in GENERATED.glob("*.pdf")], reverse=True)
    return render_template("index.html", pdfs=pdfs)

@app.post("/api/takrir/save_link")
def takrir_save_link():
    data = request.get_json(silent=True) or {}
    file_number = str(data.get("file_number") or "").strip()
    link = str(data.get("link") or "").strip()
    person_name = str(data.get("person_name") or "").strip()
    curator = str(data.get("curator") or "").strip()
    if not file_number or not link:
        return jsonify(ok=False, error="Файл № же ссылка жок."), 400

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO takrir_links (file_number, link, person_name, curator, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(file_number) DO UPDATE SET
                link = excluded.link,
                person_name = excluded.person_name,
                curator = excluded.curator,
                updated_at = excluded.updated_at
        """, (
            file_number,
            link,
            person_name,
            curator,
            datetime.now().isoformat(timespec="seconds"),
        ))
        conn.commit()
    finally:
        conn.close()
    return jsonify(ok=True)

@app.post("/api/takrir/sync_links")
def takrir_sync_links():
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []
    conn = get_db()
    saved = 0
    try:
        now = datetime.now().isoformat(timespec="seconds")
        for item in items:
            fn = str(item.get("file_number") or "").strip()
            lk = str(item.get("link") or "").strip()
            if fn and lk:
                conn.execute("""
                    INSERT INTO takrir_links (file_number, link, person_name, curator, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(file_number) DO UPDATE SET
                        link = excluded.link,
                        person_name = excluded.person_name,
                        curator = excluded.curator,
                        updated_at = excluded.updated_at
                """, (
                    fn,
                    lk,
                    str(item.get("person_name") or "").strip(),
                    str(item.get("curator") or "").strip(),
                    now,
                ))
                saved += 1
        conn.commit()
    finally:
        conn.close()
    return jsonify(ok=True, saved=saved)

@app.post("/api/takrir/delete_link")
def takrir_delete_link():
    data = request.get_json(silent=True) or {}
    file_number = str(data.get("file_number") or "").strip()
    if file_number:
        conn = get_db()
        try:
            conn.execute("DELETE FROM takrir_links WHERE file_number = ?", (file_number,))
            conn.commit()
        finally:
            conn.close()
    return jsonify(ok=True)

@app.get("/api/takrir/get_link")
def takrir_get_link():
    file_number = (request.args.get("file_number") or "").strip()
    if not file_number:
        return jsonify(ok=False)

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT link, person_name, curator FROM takrir_links WHERE file_number = ?",
            (file_number,)
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT link, beneficiary AS person_name, '' AS curator FROM beneficiaries WHERE file_number = ? AND link != ''",
                (file_number,)
            ).fetchone()
    finally:
        conn.close()

    if row and row["link"]:
        return jsonify(ok=True, link=row["link"], person_name=row["person_name"] or "")
    return jsonify(ok=False)

@app.post("/api/generate")
def generate():
    try:
        files = {}
        for k in ("photo10", "photo1", "photo2", "photo3"):
            f = request.files.get(k)
            if f and f.filename:
                files[k] = f.read()

        form_dict = dict(request.form)
        if not (form_dict.get("class_name") or "").strip():
            form_dict["class_name"] = (form_dict.get("class") or "").strip()

        file_number = (form_dict.get("file_number") or "").strip()
        if not file_number:
            return jsonify(ok=False, error="Файл № киргизиңиз."), 400

        out = build(request.form, files)
        try:
            save_beneficiary(form_dict, out.name)
        except Exception as e:
            out.unlink(missing_ok=True)
            return jsonify(ok=False, error=str(e)), 400

        return jsonify(
            ok=True,
            download=url_for("download", filename=out.name),
            file_number=file_number,
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.get("/api/search")
def search():
    file_number = (request.args.get("file_number") or "").strip()
    if not file_number:
        return jsonify(ok=False, error="Файл № жазыңыз."), 400

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM beneficiaries WHERE file_number = ?", (file_number,)
    ).fetchone()
    conn.close()

    if not row:
        return jsonify(ok=False, error=f"Файл № {file_number} табылган жок."), 404

    data = dict(row)
    if data.get("pdf_filename"):
        data["pdf_url"] = url_for("download", filename=data["pdf_filename"])

    return jsonify(ok=True, beneficiary=data)

@app.get("/download/<path:filename>")
def download(filename):
    return send_from_directory(GENERATED, filename, as_attachment=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

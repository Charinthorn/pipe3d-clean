from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import qrcode
from fpdf import FPDF
import os
import uuid
import math
import zipfile
import re

# OCP (OpenCascade for Python)
from OCP.gp import gp_Pnt, gp_Dir, gp_Ax2
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeCone
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
from OCP.IFSelect import IFSelect_RetDone

app = FastAPI()

# === CORS ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

# === Google Sheet Config ===
SERVICE_ACCOUNT_FILE = "service-account.json"
SPREADSHEET_ID = "1kK6sEVA9_p6dP5ot7QrszVPe_EYbFwv9E_zouOFzEMM"
RANGE_NAME = "'ชีต1'!A1:L"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ✅ สร้างไฟล์ service-account.json จาก ENV (ต้องใส่ก่อน credentials)
with open("service-account.json", "w") as f:
    f.write(os.getenv("GOOGLE_CREDS", ""))

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)

sheet_service = build("sheets", "v4", credentials=credentials)

# === Utility ===
def cleanup_file(path: str):
    if os.path.exists(path):
        os.remove(path)

def normalize_size(input_size: str) -> str:
    s = input_size.strip().lower()
    s = s.replace('"', '').replace('inch', '').replace('in', '')
    s = s.replace('″', '').replace('”', '').replace('’', '')
    s = s.replace(' ', '').replace('-', '+')  # เปลี่ยน 1-1/2 → 1+1/2

    # ตรวจจับรูปแบบเช่น 1+1/2 แล้วคำนวณ
    match = re.match(r'^(\d+)\+(\d+)/(\d+)$', s)
    if match:
        whole = int(match.group(1))
        numerator = int(match.group(2))
        denominator = int(match.group(3))
        value = whole + (numerator / denominator)
        return str(round(value, 3))  # เช่น 1.5

    # ตรวจจับ 1/2 แบบเดี่ยว
    match = re.match(r'^(\d+)/(\d+)$', s)
    if match:
        numerator = int(match.group(1))
        denominator = int(match.group(2))
        return str(round(numerator / denominator, 3))  # เช่น 0.5

    return s

# === API: ดึงข้อมูลรายการท่อ ===
@app.get("/api/pipes")
def get_pipe_data():
    result = sheet_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="'ชีต1'!A1:K"
    ).execute()

    values = result.get("values", [])
    if not values or len(values) < 2:
        return [["Spool ID", "SO", "Size", "End1", "End2", "Length", "Vent Hole", "file_dwg"]]

    header = [h.strip() for h in values[0]]
    cleaned = [["Spool ID", "SO", "Size", "End1", "End2", "Length", "Vent Hole", "file_dwg"]]

    for row in values[1:]:
        row_dict = dict(zip(header, row + [""] * (len(header) - len(row))))
        cleaned.append([
            row_dict.get("Spool ID", "").strip(),
            row_dict.get("SO", "").strip(),
            row_dict.get("Size", "").strip(),
            row_dict.get("End1", "").strip(),
            row_dict.get("End2", "").strip(),
            row_dict.get("Length", "").replace("mm", "").strip(),
            row_dict.get("Vent Hole", "").strip(),
            row_dict.get("file_dwg", "").strip()
        ])

    return cleaned

# === API: สร้าง Label PDF พร้อม QR Code ===
@app.post("/api/generate_label")
async def generate_label(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    spool_id = data.get("spool_id", "").strip()
    so_no = data.get("so_no", "").strip()
    size = str(data.get("size", "")).strip()
    length = str(data.get("length", "")).strip()

    # 🔍 ดึงข้อมูลทั้งหมดจาก Google Sheet (เพิ่ม Spool ID → คอลัมน์ A ถึง K)
    result = sheet_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="'ชีต1'!A1:K"
    ).execute()

    values = result.get("values", [])
    if not values or len(values) < 2:
        raise HTTPException(status_code=404, detail="No data found")

    header = [h.strip() for h in values[0]]
    match = None

    # 🔎 หาแถวที่ตรงกับ spool_id เป็นหลัก (หรือ fallback ด้วย so_no, size, length ถ้าจำเป็น)
    for row in values[1:]:
        row_dict = dict(zip(header, row))
        if (
            row_dict.get("Spool ID", "").strip() == spool_id or (
                row_dict.get("SO", "").strip() == so_no and
                row_dict.get("Size", "").strip() == size and
                row_dict.get("Length", "").replace("mm", "").strip() == length
            )
        ):
            match = row_dict
            break

    if not match:
        raise HTTPException(status_code=404, detail="Matching pipe not found in sheet")

    # ✅ ดึงค่าจาก row ที่เจอ
    spool_id = match.get("Spool ID", spool_id)
    end1 = match.get("End1", "-")
    end2 = match.get("End2", "-")
    vent_hole = match.get("Vent Hole", "No")
    file_dwg = match.get("file_dwg", "")

    # ✅ สร้าง QR
    qr_text = f"{spool_id} | SO: {so_no} | Ø{size} | {length}mm | {end1}/{end2} | Vent Hole: {vent_hole}"
    qr_img = qrcode.make(qr_text)
    qr_path = f"{spool_id}_qr.png"
    qr_img.save(qr_path)

    # ✅ สร้าง PDF Label ขนาด 40mm x 30mm
    pdf = FPDF(format=(40, 30))
    pdf.add_page()
    pdf.set_auto_page_break(False)
    pdf.set_font("Arial", size=5)

    start_x, start_y = 4, 3
    col1_w, col2_w = 12, 24

    pdf.set_xy(start_x, start_y)
    pdf.cell(col1_w, 4, "Pipe:", ln=0)
    pdf.cell(col2_w, 3.5, spool_id, ln=1)

    pdf.set_x(start_x)
    pdf.cell(col1_w, 4, "SO No:", ln=0)
    pdf.cell(col2_w, 3.5, so_no, ln=1)

    pdf.set_x(start_x)
    pdf.cell(col1_w, 4, "Size:", ln=0)
    pdf.cell(col2_w, 3.5, f"Ø{size}", ln=1)

    pdf.set_x(start_x)
    pdf.cell(col1_w, 4, "Length:", ln=0)
    pdf.cell(col2_w, 3.5, f"{length} mm", ln=1)

    pdf.image(qr_path, x=13.5, y=16, w=13)

    pdf_path = f"{spool_id}_label.pdf"
    pdf.output(pdf_path)
    os.remove(qr_path)
    background_tasks.add_task(cleanup_file, pdf_path)

    return FileResponse(path=pdf_path, filename=pdf_path, media_type="application/pdf")

# ✅ ตาราง NPS → OD (หน่วย mm)
NPS_TO_OD_MM = {
    "0.5": 21.34, "0.75": 26.67, "1": 33.40, "1.25": 42.16,
    "1.5": 48.26, "2": 60.33, "2.5": 73.03, "3": 88.90,
    "4": 114.30, "6": 168.28, "8": 219.08
}

# === API: สร้าง ZIP ที่รวม STEP หลายไฟล์ ===
@app.post("/api/generate_steps_zip")
async def generate_steps_zip(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()  # [{'spool_id': 'S001', 'size': '2', 'length': 1000}, ...]
    file_paths = []

    for item in data:
        try:
            raw_size = str(item.get("size", "")).strip()
            cleaned_key = normalize_size(raw_size)
            od_mm = NPS_TO_OD_MM.get(cleaned_key)
            if od_mm is None:
                continue

            od_inch = float(cleaned_key)
            length = float(item.get("length", 0))
            if length <= 0:
                continue

            wall_thickness = 3.4
            outer_radius = od_mm / 2
            inner_radius = outer_radius - wall_thickness
            bevel_angle_deg = 53
            bevel_height = wall_thickness / math.tan(math.radians(bevel_angle_deg))

            outer_cyl = BRepPrimAPI_MakeCylinder(outer_radius, length).Shape()
            inner_cyl = BRepPrimAPI_MakeCylinder(inner_radius, length).Shape()
            pipe = BRepAlgoAPI_Cut(outer_cyl, inner_cyl).Shape()

            # เจาะรู
            hole_radius = 1.5
            hole_depth = od_mm
            if length >= 500:
                hole1 = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 80), gp_Dir(1, 0, 0)), hole_radius, hole_depth).Shape()
                hole2 = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, length - 80), gp_Dir(1, 0, 0)), hole_radius, hole_depth).Shape()
                pipe = BRepAlgoAPI_Cut(pipe, hole1).Shape()
                pipe = BRepAlgoAPI_Cut(pipe, hole2).Shape()
            else:
                center_z = length / 2
                hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, center_z), gp_Dir(1, 0, 0)), hole_radius, hole_depth).Shape()
                pipe = BRepAlgoAPI_Cut(pipe, hole).Shape()

           # ✅ ใช้ pipe ตรงๆ
            final_pipe = pipe

            uid = uuid.uuid4().hex[:6]
            spool_id = item.get("spool_id", "spool")
            step_filename = f"{spool_id}_{int(length)}mm_{uid}.step"
            step_writer = STEPControl_Writer()
            if step_writer.Transfer(final_pipe, STEPControl_AsIs) == IFSelect_RetDone:
                step_writer.Write(step_filename)
                file_paths.append(step_filename)
        except:
            continue

    zip_name = f"pipes_{uuid.uuid4().hex[:6]}.zip"
    with zipfile.ZipFile(zip_name, 'w') as zipf:
        for file in file_paths:
            zipf.write(file)

    background_tasks.add_task(cleanup_file, zip_name)
    for file in file_paths:
        background_tasks.add_task(cleanup_file, file)

    return FileResponse(path=zip_name, filename=zip_name, media_type="application/zip")


# ✅ ตัวอย่างฟังก์ชันลบไฟล์ (คุณต้องกำหนดในระบบจริง)
def cleanup_file(filename):
    import os, time
    time.sleep(10)  # ให้ผู้ใช้ดาวน์โหลดก่อน
    if os.path.exists(filename):
        os.remove(filename)

@app.post("/api/generate_step")
async def generate_step(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    try:
        raw_size = str(data.get("size", "")).strip()
        cleaned_key = normalize_size(raw_size)

        od_mm = NPS_TO_OD_MM.get(cleaned_key)
        if od_mm is None:
            return {"error": f"Unsupported pipe size: '{raw_size}'"}

        od_inch = float(cleaned_key)
        length = float(data.get("length", 0))
        if length <= 0:
            return {"error": "Length must be greater than 0"}

        wall_thickness = 3.4  # For SCH40 approx.
        outer_radius = od_mm / 2
        inner_radius = outer_radius - wall_thickness
        bevel_angle_deg = 53
        bevel_height = wall_thickness / math.tan(math.radians(bevel_angle_deg))

        if inner_radius <= 0 or bevel_height <= 0:
            return {"error": "Invalid geometry values"}

        # ✅ Step 1: Hollow pipe
        outer_cyl = BRepPrimAPI_MakeCylinder(outer_radius, length).Shape()
        inner_cyl = BRepPrimAPI_MakeCylinder(inner_radius, length).Shape()
        pipe = BRepAlgoAPI_Cut(outer_cyl, inner_cyl).Shape()

        # ✅ Step 2: Hole logic
        hole_radius = 1.5 # รัศมีรูเจาะ
        hole_depth = od_mm
        if length >= 500:
            hole1 = BRepPrimAPI_MakeCylinder(
                gp_Ax2(gp_Pnt(0, 0, 80), gp_Dir(1, 0, 0)), hole_radius, hole_depth
            ).Shape()
            hole2 = BRepPrimAPI_MakeCylinder(
                gp_Ax2(gp_Pnt(0, 0, length - 80), gp_Dir(1, 0, 0)), hole_radius, hole_depth
            ).Shape()
            pipe = BRepAlgoAPI_Cut(pipe, hole1).Shape()
            pipe = BRepAlgoAPI_Cut(pipe, hole2).Shape()
        else:
            center_z = length / 2
            hole = BRepPrimAPI_MakeCylinder(
                gp_Ax2(gp_Pnt(0, 0, center_z), gp_Dir(1, 0, 0)), hole_radius, hole_depth
            ).Shape()
            pipe = BRepAlgoAPI_Cut(pipe, hole).Shape()

        # ✅ Step 3: ข้าม bevel ใช้ pipe ตรงๆ
        final_pipe = pipe

        # ✅ Step 4: Export STEP
        uid = uuid.uuid4().hex[:6]
        filename = f"pipe_{od_inch}in_{int(length)}mm_{uid}.step"
        step_writer = STEPControl_Writer()
        status = step_writer.Transfer(final_pipe, STEPControl_AsIs)

        print("raw_size:", raw_size, "→", "cleaned_key:", cleaned_key)
        print("od_mm:", od_mm)

        if status == IFSelect_RetDone:
            step_writer.Write(filename)
            background_tasks.add_task(cleanup_file, filename)
            return FileResponse(path=filename, filename=filename, media_type="application/step")
        else:
            return {"error": "STEP export failed"}

    except Exception as e:
        return {"error": str(e)}
    
@app.post("/api/update_status")
async def update_status(request: Request):
    data = await request.json()
    spool_id = data.get("spool_id", "").strip()
    status = data.get("status", "").strip()

    if not spool_id or not status:
        raise HTTPException(status_code=400, detail="Missing spool_id or status")

    # อ่านข้อมูลจาก Google Sheet
    result = sheet_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=RANGE_NAME
    ).execute()

    values = result.get("values", [])
    if not values or len(values) < 2:
        raise HTTPException(status_code=404, detail="No data found")

    header = values[0]
    spool_col = header.index("Spool ID") if "Spool ID" in header else -1
    status_col = header.index("status_cut") if "status_cut" in header else -1

    if spool_col == -1 or status_col == -1:
        raise HTTPException(status_code=500, detail="Missing required columns")

    row_index = -1
    for i, row in enumerate(values[1:], start=2):  # start=2 เพราะ row 1 = header
        if len(row) > spool_col and row[spool_col].strip() == spool_id:
            row_index = i
            break

    if row_index == -1:
        raise HTTPException(status_code=404, detail=f"Spool ID '{spool_id}' not found")

    # เขียนค่าลง Sheet
    sheet_service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"ชีต1!{chr(65 + status_col)}{row_index}",  # เช่น L5
        valueInputOption="RAW",
        body={"values": [[status]]}
    ).execute()

    return {"success": True, "spool_id": spool_id, "status": status}

@app.post("/api/update_status_assembly")
async def update_status_assembly(request: Request):
    data = await request.json()
    spool_id = data.get("spool_id", "").strip()
    status = data.get("status", "").strip()

    if not spool_id or not status:
        raise HTTPException(status_code=400, detail="Missing spool_id or status")

    # 🔍 อ่านข้อมูลทั้งหมดจากชีต
    result = sheet_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="'ชีต1'!A1:Z"
    ).execute()

    values = result.get("values", [])
    if not values or len(values) < 2:
        raise HTTPException(status_code=404, detail="No data found in spreadsheet")

    # 🔎 หา index ของคอลัมน์
    header = [h.strip().lower() for h in values[0]]
    try:
        spool_col = header.index("spool id")
        status_col = header.index("status_assembly")
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"Column not found: {e}")

    # 🔍 หาแถวที่ตรงกับ Spool ID
    row_index = -1
    for i, row in enumerate(values[1:], start=2):  # row 2 = index 1 + header
        if spool_col >= len(row):
            continue
        if row[spool_col].strip() == spool_id:
            row_index = i
            break

    if row_index == -1:
        raise HTTPException(status_code=404, detail=f"Spool ID '{spool_id}' not found")

    # ✅ อัปเดตสถานะ
    cell = f"'ชีต1'!{chr(65 + status_col)}{row_index}"  # แปลง column index → A, B, C...
    sheet_service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=cell,
        valueInputOption="RAW",
        body={"values": [[status]]}
    ).execute()

    return {"success": True, "spool_id": spool_id, "status": status}

def col_index_to_letter(index):
    result = ""
    while index >= 0:
        result = chr(index % 26 + 65) + result
        index = index // 26 - 1
    return result

@app.post("/api/update_status_blasting")
async def update_status_blasting(request: Request):
    data = await request.json()
    spool_ids = [s.strip() for s in data.get("spool_ids", [])]
    status = data.get("status", "").strip()

    if not spool_ids or not status:
        raise HTTPException(status_code=400, detail="Missing spool_ids or status")

    result = sheet_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="'ชีต1'!A1:Z"
    ).execute()

    values = result.get("values", [])
    if not values or len(values) < 2:
        raise HTTPException(status_code=404, detail="No data found in spreadsheet")

    header = [h.strip().lower() for h in values[0]]
    try:
        spool_col = header.index("spool id")
        status_col = header.index("status_blasting")
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"Column not found: {e}")

    updated = 0
    for i, row in enumerate(values[1:], start=2):
        if spool_col >= len(row):
            continue
        if row[spool_col].strip() in spool_ids:
            col_letter = col_index_to_letter(status_col)
            cell = f"'ชีต1'!{col_letter}{i}"
            sheet_service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=cell,
                valueInputOption="RAW",
                body={"values": [[status]]}
            ).execute()
            updated += 1

    return {"success": True, "updated": updated, "status": status}

@app.post("/api/update_status_lining")
async def update_status_lining(request: Request):
   
    data = await request.json()
    spool_ids = data.get("spool_ids", [])
    status = data.get("status", "").strip().lower()

    if not spool_ids or status not in ("finished", "in-progress", "pending"):
        raise HTTPException(status_code=400, detail="Missing spool_ids or invalid status")

    # ดึงข้อมูล worksheet กลับมาทั้งแผ่น
    result = sheet_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="'ชีต1'!A1:Q"   # A–Q เพื่อรวมคอลัมน์ status_lining
    ).execute()
    values = result.get("values", [])
    if not values or len(values) < 2:
        raise HTTPException(status_code=404, detail="No data in spreadsheet")

    # แปลง header เป็น lowercase เพื่อหา index ของคอลัมน์
    header = [h.strip().lower() for h in values[0]]
    try:
        spool_col = header.index("spool id")
        lining_col = header.index("status_lining")
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"Column not found: {e}")

    updated = 0
    for i, row in enumerate(values[1:], start=2):  # i=2 แทนแถวที่ 2 จริง ๆ
        if spool_col >= len(row):
            continue
        cell_spool = row[spool_col].strip()
        if cell_spool and cell_spool in spool_ids:
            # สร้างพิกัด cell เพื่ออัปเดตคอลัมน์ status_lining
            # Example: ถ้า lining_col = 14 (คอลัมน์ O คือ index 14 ถือนับ A=0,B=1,...)
            col_letter = chr(ord('A') + lining_col)
            cell_range = f"'ชีต1'!{col_letter}{i}"
            sheet_service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=cell_range,
                valueInputOption="RAW",
                body={"values": [[status]]}
            ).execute()
            updated += 1

    return {"success": True, "updated": updated, "status": status}

@app.post("/api/update_status_coating")
async def update_status_coating(request: Request):
    data = await request.json()
    spool_id = data.get("spool_id", "").strip()
    status = data.get("status", "").strip()

    if not spool_id or not status:
        raise HTTPException(status_code=400, detail="Missing spool_id or status")

    result = sheet_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="'ชีต1'!A1:Z"
    ).execute()

    values = result.get("values", [])
    header = [h.strip().lower() for h in values[0]]

    try:
        spool_col = header.index("spool id")
        status_col = header.index("status_coating")  # ✅ ต้องมีคอลัมน์นี้ใน Google Sheet
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"Column not found: {e}")

    for i, row in enumerate(values[1:], start=2):
        if spool_col >= len(row):
            continue
        if row[spool_col].strip() == spool_id:
            cell = f"'ชีต1'!{chr(65 + status_col)}{i}"
            sheet_service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=cell,
                valueInputOption="RAW",
                body={"values": [[status]]}
            ).execute()
            return {"success": True, "spool_id": spool_id, "status": status}

    raise HTTPException(status_code=404, detail="Spool ID not found")

@app.post("/api/update_status_labeling")
async def update_status_labeling(request: Request):
    data = await request.json()
    spool_ids = data.get("spool_ids", [])
    status = data.get("status", "").strip()

    if not spool_ids or not status:
        raise HTTPException(status_code=400, detail="Missing spool_ids or status")

    result = sheet_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="'ชีต1'!A1:Z"
    ).execute()

    values = result.get("values", [])
    if not values or len(values) < 2:
        raise HTTPException(status_code=404, detail="No data in sheet")

    header = [h.strip().lower() for h in values[0]]
    try:
        spool_col = header.index("spool id")
        status_col = header.index("status_labeling")
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"Column not found: {e}")

    updated = 0
    for i, row in enumerate(values[1:], start=2):
        if spool_col >= len(row):
            continue
        if row[spool_col].strip() in spool_ids:
            cell = f"'ชีต1'!{chr(65 + status_col)}{i}"
            sheet_service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=cell,
                valueInputOption="RAW",
                body={"values": [[status]]}
            ).execute()
            updated += 1

    return {"success": True, "updated": updated}

@app.get("/api/pipes_statuses")
def get_pipe_statuses():
    # อ่านข้อมูลตั้งแต่คอลัมน์ A – Q (Spool ID, SO, Size, …, status_labeling)
    result = sheet_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="'ชีต1'!A1:Q"
    ).execute()

    values = result.get("values", [])
    if not values or len(values) < 2:
        # กรณีไม่มีข้อมูลเลย ให้คืน head เดียวเท่านั้น
        return [[
            "Spool ID", "SO",
            "status_cut", "status_assembly", "status_blasting",
            "status_lining", "status_coating", "status_labeling"
        ]]

    # ดึง header จริง (ชื่อคอลัมน์จากแถวแรกของชีต)
    header = [h.strip() for h in values[0]]

    # เตรียม array ที่จะคืนกลับ: แถวแรกเป็นชื่อคอลัมน์ที่เราต้องการ
    cleaned = [[
        "Spool ID", "SO",
        "status_cut", "status_assembly", "status_blasting",
        "status_lining", "status_coating", "status_labeling"
    ]]

    # สำหรับแต่ละแถวข้อมูล (row 2 เป็นต้นไป)
    for row in values[1:]:
        # สร้าง dict mapping ชื่อหัวข้อ → ค่าในแถว (เติม "" ในกรณีที่ row สั้นกว่า header)
        row_dict = dict(zip(header, row + [""] * (len(header) - len(row))))

        cleaned.append([
            row_dict.get("Spool ID", "").strip(),
            row_dict.get("SO", "").strip(),
            row_dict.get("status_cut", "").strip().lower(),
            row_dict.get("status_assembly", "").strip().lower(),
            row_dict.get("status_blasting", "").strip().lower(),
            row_dict.get("status_lining", "").strip().lower(),
            row_dict.get("status_coating", "").strip().lower(),
            row_dict.get("status_labeling", "").strip().lower(),
        ])

    return cleaned

@app.get("/", response_class=HTMLResponse)
async def read_home(request: Request):
    return templates.TemplateResponse("Dashboard.html", {"request": request})

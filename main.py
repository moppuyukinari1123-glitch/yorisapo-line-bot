import os
import json
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Header
import httpx
from supabase import create_client
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

# --- Google Calendar API 用ライブラリ ---
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = FastAPI()
JST = timezone(timedelta(hours=9))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "your-secret-token")

# =========================================================
# 【よりサポ 専用】基本スケジュール (仮：10:00〜18:00、1時間刻み)
# =========================================================
def get_yorisapo_schedule():
    slots = []
    current = datetime.strptime("10:00", "%H:%M")
    end_time_limit = datetime.strptime("18:00", "%H:%M")
    while current < end_time_limit:
        s_str = current.strftime("%H:%M")
        end_time = current + timedelta(hours=1)
        e_str = end_time.strftime("%H:%M")
        slots.append({"start": s_str, "end": e_str, "booth": "1"})
        current = end_time
    return slots

def get_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    return create_client(url, key)

# --- Google Calendar 認証・接続関数 ---
def get_gcal_service():
    try:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if not creds_json: return None
        creds_dict = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=['https://www.googleapis.com/auth/calendar']
        )
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print("Google Calendar Init Error:", e)
        return None

def short_res_id(reservation_id: str) -> str:
    return str(reservation_id).replace("-", "")[:8]

def get_booking_limit(supabase):
    res = supabase.table("system_config").select("value").eq("key", "booking_limit").execute()
    if res.data: return res.data[0]["value"]
    return "2099-12-31"

# =========================================================
#  ユーザー側 API
# =========================================================
@app.get("/api/menus")
def api_get_menus():
    supabase = get_supabase()
    res = supabase.table("menus").select("*").order("sort_order").execute()
    return {"menus": res.data}

@app.get("/api/slots")
def api_slots(date: str):
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except:
        raise HTTPException(status_code=400, detail="Invalid date format")

    supabase = get_supabase()
    limit_date = get_booking_limit(supabase)
    if date > limit_date:
        return {"date": date, "slots": [], "pattern": "LOCKED"}

    occupied_times = set()
    
    # 1. システム内の予約を取得
    res_booked = supabase.table("reservations").select("start_at").neq("status", "cancelled").execute()
    for r in res_booked.data:
        s = datetime.fromisoformat(r['start_at'].replace("Z", "+00:00")).astimezone(JST)
        if s.strftime('%Y-%m-%d') == date:
            occupied_times.add(s.strftime("%H:%M"))

    # 2. 管理画面からのブロックを取得
    res_blocked = supabase.table("blocked_slots").select("*").eq("block_date", date).execute()
    for b in res_blocked.data:
        b_start = b.get("slot_start")
        if not b_start: 
            for s in get_yorisapo_schedule(): occupied_times.add(s['start'])
        else: occupied_times.add(b_start)

    # 3. ★ Google Calendarの予定を取得してブロック ★
    gcal_busy = []
    service = get_gcal_service()
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if service and calendar_id:
        try:
            t_min = f"{date}T00:00:00+09:00"
            t_max = f"{date}T23:59:59+09:00"
            events_res = service.events().list(
                calendarId=calendar_id, timeMin=t_min, timeMax=t_max,
                singleEvents=True, orderBy='startTime').execute()
            
            for event in events_res.get('items', []):
                start = event['start'].get('dateTime', event['start'].get('date'))
                end = event['end'].get('dateTime', event['end'].get('date'))
                if start and end:
                    s_dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(JST)
                    e_dt = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(JST)
                    gcal_busy.append((s_dt, e_dt))
        except Exception as e:
            print("Google Calendar Fetch Error:", e)

    base_schedule = get_yorisapo_schedule()
    result_slots = []
    now_jst = datetime.now(JST)
    today_str = now_jst.strftime("%Y-%m-%d")

    for s in base_schedule:
        slot_start_dt = datetime.strptime(f"{date} {s['start']}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        slot_end_dt = datetime.strptime(f"{date} {s['end']}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        
        # 過去時間チェック
        is_ok = not (date <= today_str or slot_start_dt < now_jst or s['start'] in occupied_times)

        # Googleカレンダーとの重複チェック (時間が少しでも被っていればNG)
        if is_ok:
            for g_start, g_end in gcal_busy:
                if max(slot_start_dt, g_start) < min(slot_end_dt, g_end):
                    is_ok = False
                    break

        result_slots.append({
            "start": s['start'], "end": s['end'], "booth": "1", "ok": is_ok
        })
    return {"date": date, "slots": result_slots}

class ReservationIn(BaseModel):
    user_id: str
    menu_code: str
    start: str
    customer_name: str = ""
    customer_phone: str = ""

@app.post("/api/reservations")
async def api_create_reservation(body: ReservationIn):
    supabase = get_supabase()
    
    m_res = supabase.table("menus").select("*").eq("code", body.menu_code).execute()
    if not m_res.data: raise HTTPException(status_code=404, detail="Menu not found")
    menu_data = m_res.data[0]
    duration = menu_data.get("duration_minutes", 60)
    
    s_utc = datetime.fromisoformat(body.start).astimezone(timezone.utc)
    e_utc = s_utc + timedelta(minutes=duration)
    
    target_date_str = s_utc.astimezone(JST).strftime("%Y-%m-%d")
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    if target_date_str <= today_str:
         raise HTTPException(status_code=400, detail="当日のご予約は締め切らせていただきました。")

    # システム重複チェック
    check = supabase.table("reservations").select("id").neq("status", "cancelled").eq("start_at", s_utc.isoformat()).execute()
    if check.data: raise HTTPException(status_code=409, detail="既に予約が埋まっています")

    # ★ Google Calendar に予定を追加 ★
    gcal_event_id = None
    service = get_gcal_service()
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if service and calendar_id:
        try:
            event_body = {
                'summary': f"【よりサポ面談】{body.customer_name}様",
                'description': f"電話番号: {body.customer_phone}\nメニュー: {menu_data['name']}",
                'start': {'dateTime': s_utc.isoformat()},
                'end': {'dateTime': e_utc.isoformat()},
            }
            created_event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
            gcal_event_id = created_event.get('id')
        except Exception as e:
            print("Google Calendar Insert Error:", e)

    new_res = {
        "customer_user_id": body.user_id,
        "customer_name": body.customer_name,
        "customer_phone": body.customer_phone,
        "menu_code": body.menu_code,
        "start_at": s_utc.isoformat(),
        "end_at": e_utc.isoformat(),
        "booth": "1",
        "status": "confirmed",
        "gcal_event_id": gcal_event_id  # GCalのIDを保存
    }
    
    # ★ 先に顧客データを名簿に登録する ★
    cust_check = supabase.table("customers").select("line_user_id").eq("line_user_id", body.user_id).execute()
    if not cust_check.data:
        supabase.table("customers").insert({"line_user_id": body.user_id, "name": body.customer_name}).execute()

    # ★ そのあとに予約データを保存する ★
    ins = supabase.table("reservations").insert(new_res).execute()

    data = ins.data[0]
    rid_short = short_res_id(data['id'])
    
    await line_push(body.user_id, f"予約確定しました✨\n日時: {s_utc.astimezone(JST).strftime('%m/%d %H:%M')}〜\n予約ID: {rid_short}")
    for uid in [os.environ.get("OWNER_USER_ID")]:
        if uid: await line_push(uid, f"【新規予約】\n{body.customer_name}様\n{s_utc.astimezone(JST).strftime('%m/%d %H:%M')}〜\n{menu_data['name']}")

    return {"ok": True, "reservation_id": rid_short}

@app.get("/api/my-reservations")
def api_my_reservations(user_id: str):
    supabase = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()
    res = supabase.table("reservations").select("*").eq("customer_user_id", user_id).neq("status", "cancelled").gte("start_at", now_iso).order("start_at").execute()
    formatted = []
    for r in res.data:
        s_dt = datetime.fromisoformat(r['start_at'].replace("Z", "+00:00")).astimezone(JST)
        formatted.append({"id": r["id"], "start_jst": s_dt.strftime("%Y-%m-%d %H:%M"), "menu_code": r["menu_code"], "booth": r["booth"]})
    return {"reservations": formatted}

@app.post("/api/cancel")
async def api_cancel(body: dict):
    supabase = get_supabase()
    target_res = supabase.table("reservations").select("*").eq("id", body['reservation_id']).execute()
    supabase.table("reservations").update({"status": "cancelled"}).eq("id", body['reservation_id']).eq("customer_user_id", body['user_id']).execute()
    
    if target_res.data:
        r = target_res.data[0]
        s_dt = datetime.fromisoformat(r['start_at'].replace("Z", "+00:00")).astimezone(JST)
        s_str = s_dt.strftime('%m/%d %H:%M')
        
        # ★ Google Calendar から予定を削除 ★
        gcal_event_id = r.get('gcal_event_id')
        service = get_gcal_service()
        calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
        if service and calendar_id and gcal_event_id:
            try:
                service.events().delete(calendarId=calendar_id, eventId=gcal_event_id).execute()
            except Exception as e:
                print("Google Calendar Delete Error:", e)

        await line_push(body['user_id'], f"予約をキャンセルしました。\n日時: {s_str}")
        for uid in [os.environ.get("OWNER_USER_ID")]:
            if uid: await line_push(uid, f"【キャンセル】\n{r.get('customer_name')}様\n日時: {s_str}")

    return {"ok": True}

# =========================================================
#  管理者用API (予約一覧・ブロック・設定)
# =========================================================
@app.get("/api/admin/reservations")
def api_admin_reservations(request: Request, date_from: str = None, date_to: str = None):
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN: raise HTTPException(status_code=401)
    supabase = get_supabase()
    query = supabase.table("reservations").select("*")
    if date_from: query = query.gte("start_at", f"{date_from}T00:00:00+09:00")
    if date_to: query = query.lte("start_at", f"{date_to}T23:59:59+09:00")
    res = query.order("start_at", desc=True).limit(200).execute() 
    
    formatted = []
    for r in res.data:
        s_dt = datetime.fromisoformat(r['start_at'].replace("Z", "+00:00")).astimezone(JST)
        formatted.append({
            "id": r["id"], "start_jst": s_dt.strftime("%Y-%m-%d %H:%M"),
            "menu_code": r["menu_code"], "customer_name": r["customer_name"], 
            "status": r["status"]
        })
    return {"reservations": formatted}

@app.post("/api/admin/blocks")
async def api_admin_add_block(body: dict, request: Request):
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN: raise HTTPException(status_code=401)
    supabase = get_supabase()
    res = supabase.table("blocked_slots").insert({ "block_date": body.get("date"), "slot_start": body.get("slot_start"), "booth": "1" }).execute()
    return {"ok": True}

@app.get("/api/admin/blocks")
def api_admin_get_blocks(request: Request):
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN: raise HTTPException(status_code=401)
    supabase = get_supabase()
    today = datetime.now(JST).strftime("%Y-%m-%d")
    res = supabase.table("blocked_slots").select("*").gte("block_date", today).order("block_date").execute()
    return {"ok": True, "blocks": res.data}

@app.delete("/api/admin/blocks/{block_id}")
async def api_admin_delete_block(block_id: str, request: Request):
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN: raise HTTPException(status_code=401)
    supabase = get_supabase()
    supabase.table("blocked_slots").delete().eq("id", block_id).execute()
    return {"ok": True}

@app.get("/api/admin/config/limit")
def api_admin_get_limit(request: Request):
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN: raise HTTPException(status_code=401)
    supabase = get_supabase()
    res = supabase.table("system_config").select("value").eq("key", "booking_limit").execute()
    return {"limit": res.data[0]["value"] if res.data else ""}

@app.post("/api/admin/config/limit")
async def api_admin_set_limit(body: dict, request: Request):
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN: raise HTTPException(status_code=401)
    supabase = get_supabase()
    supabase.table("system_config").upsert({"key": "booking_limit", "value": body["limit"]}).execute()
    return {"ok": True}

async def line_push(to_user_id, text):
    token = os.environ.get("LINE_ACCESS_TOKEN")
    if not to_user_id or not token: return
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        await client.post("https://api.line.me/v2/bot/message/push", headers=headers, json={"to": to_user_id, "messages": [{"type": "text", "text": text}]})

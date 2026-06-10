"""
部门日程秘书 - Web 版
FastAPI 后端 + 内嵌前端页面
"""
import json
import os
import re
import logging
import sqlite3
from datetime import datetime as dt, timedelta as td
from urllib.request import Request, urlopen
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

# ============ 配置 ============
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = "https://api.deepseek.com/v1"
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DB_PATH = os.environ.get("DB_PATH", "schedules.db")
logger.info(f"DEEPSEEK_API_KEY loaded: {'YES' if DEEPSEEK_API_KEY else 'NO'}")
logger.info(f"ENV keys: {[k for k in os.environ if 'DEEP' in k.upper()]}")
PORT = int(os.environ.get("PORT", "8000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="部门日程秘书")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ============ 数据库 ============
def get_conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            department TEXT,
            title TEXT NOT NULL,
            date TEXT NOT NULL,
            time_start TEXT,
            time_end TEXT,
            location TEXT,
            remark TEXT,
            status TEXT DEFAULT '正常',
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_name ON schedules(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON schedules(date)")
    conn.commit()
    conn.close()

def row_to_dict(row):
    cols = ['id','name','department','title','date','time_start','time_end',
            'location','remark','status','created_at','updated_at']
    return dict(zip(cols, row))

# ============ CRUD ============
def db_create(data: dict):
    now = dt.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO schedules (name, department, title, date, time_start, time_end,
           location, remark, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (data['name'], data.get('department'), data['title'], data['date'],
         data.get('time_start'), data.get('time_end'), data.get('location'),
         data.get('remark'), '正常', now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM schedules WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return row_to_dict(row)

def db_query(name=None, date_from=None, date_to=None, date=None, keyword=None, department=None):
    conds, params = ["status != '已取消'"], []
    if name:
        conds.append("name LIKE ?"); params.append(f"%{name}%")
    if department:
        conds.append("department LIKE ?"); params.append(f"%{department}%")
    if date:
        conds.append("date = ?"); params.append(date)
    if date_from:
        conds.append("date >= ?"); params.append(date_from)
    if date_to:
        conds.append("date <= ?"); params.append(date_to)
    if keyword:
        like = f"%{keyword}%"
        conds.append("(title LIKE ? OR location LIKE ? OR remark LIKE ?)")
        params.extend([like, like, like])
    sql = f"SELECT * FROM schedules WHERE {' AND '.join(conds)} ORDER BY date, time_start"
    conn = get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

def db_get(sid):
    conn = get_conn()
    r = conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    conn.close()
    return row_to_dict(r) if r else None

def db_update(sid, updates: dict):
    fields, vals = [], []
    for k in ['title','date','time_start','time_end','location','remark','status','department','name']:
        if k in updates and updates[k] is not None:
            fields.append(f"{k}=?"); vals.append(updates[k])
    if not fields:
        return db_get(sid)
    fields.append("updated_at=?"); vals.append(dt.now().strftime("%Y-%m-%d %H:%M:%S"))
    vals.append(sid)
    conn = get_conn()
    conn.execute(f"UPDATE schedules SET {', '.join(fields)} WHERE id=?", vals)
    conn.commit()
    r = conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    conn.close()
    return row_to_dict(r) if r else None

def db_delete(sid):
    conn = get_conn()
    conn.execute("UPDATE schedules SET status='已取消',updated_at=? WHERE id=?",
                 (dt.now().strftime("%Y-%m-%d %H:%M:%S"), sid))
    conn.commit()
    conn.close()
    return True

# ============ AI 智能体 ============
TOOLS = [
    {"type":"function","function":{"name":"create_schedule","description":"创建新日程",
     "parameters":{"type":"object","properties":{
         "name":{"type":"string","description":"人员姓名"},
         "title":{"type":"string","description":"日程标题"},
         "date":{"type":"string","description":"日期YYYY-MM-DD"},
         "department":{"type":"string","description":"部门"},
         "time_start":{"type":"string","description":"开始时间HH:MM"},
         "time_end":{"type":"string","description":"结束时间HH:MM"},
         "location":{"type":"string","description":"地点"},
         "remark":{"type":"string","description":"备注"}},
     "required":["name","title","date"]}}},
    {"type":"function","function":{"name":"query_schedules","description":"查询日程",
     "parameters":{"type":"object","properties":{
         "name":{"type":"string"},"department":{"type":"string"},"date":{"type":"string"},
         "date_from":{"type":"string"},"date_to":{"type":"string"},"keyword":{"type":"string"}}}}},
    {"type":"function","function":{"name":"update_schedule","description":"修改日程",
     "parameters":{"type":"object","properties":{
         "id":{"type":"integer","description":"日程ID"},
         "title":{"type":"string"},"date":{"type":"string"},
         "time_start":{"type":"string"},"time_end":{"type":"string"},
         "location":{"type":"string"},"remark":{"type":"string"},"status":{"type":"string"}},
     "required":["id"]}}},
    {"type":"function","function":{"name":"delete_schedule","description":"取消日程",
     "parameters":{"type":"object","properties":{
         "id":{"type":"integer","description":"日程ID"}}, "required":["id"]}}},
]

def _system_prompt():
    t = dt.now()
    mon = t - td(days=t.weekday())
    sun = mon + td(days=6)
    nm = mon + td(days=7)
    ns = sun + td(days=7)
    return (
        "你是部门日程秘书，帮助用户管理领导及员工的每周安排。"
        f"今天{t.strftime('%Y-%m-%d')}。本周{mon.strftime('%Y-%m-%d')}至{sun.strftime('%Y-%m-%d')}，"
        f"下周{nm.strftime('%Y-%m-%d')}至{ns.strftime('%Y-%m-%d')}。"
        "通过工具增删改查日程，回复简洁口语化，查询结果用表格。"
    )

def _parse_tool_tags(text):
    results = []
    blocks = re.findall(r'<invoke\s+name="([^"]+)">(.*?)</invoke>', text, re.DOTALL)
    for name, body in blocks:
        args = {}
        params = re.findall(r'<parameter\s+name="([^"]+)"(?:\s+[^>]*)?>(.*?)</parameter>', body, re.DOTALL)
        for k, v in params:
            v = v.strip()
            if v.isdigit(): v = int(v)
            elif v.lower() in ('true','false'): v = v.lower() == 'true'
            args[k] = v
        results.append((name, args))
    return results

def _chat(messages, tools=None):
    payload = {"model": MODEL, "messages": messages, "max_tokens": 1024}
    if tools:
        payload["tools"] = tools; 
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = Request(f"{BASE_URL}/chat/completions", data=data,
                  headers={"Content-Type":"application/json","Authorization":f"Bearer {DEEPSEEK_API_KEY}"},
                  method="POST")
    with urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())

def _call_tool(name, args):
    try:
        if name == "create_schedule":
            s = db_create(args); return f"已创建：{s['name']} {s['date']} {s['title']}"
        elif name == "query_schedules":
            results = db_query(**{k:v for k,v in args.items()})
            if not results: return "暂无日程安排"
            lines = ["| 姓名 | 日期 | 时间 | 日程 | 地点 |", "|------|------|------|------|------|"]
            for s in results:
                t = f"{s['time_start'] or '--'}-{s['time_end'] or '--'}"
                lines.append(f"| {s['name']} | {s['date']} | {t} | {s['title']} | {s['location'] or '--'} |")
            return "\n".join(lines)
        elif name == "update_schedule":
            sid = args.pop("id"); s = db_update(sid, args)
            return f"已修改：{s['name']} {s['date']} {s['title']}" if s else f"未找到ID={sid}"
        elif name == "delete_schedule":
            sid = args["id"]; ex = db_get(sid)
            db_delete(sid); return f"已取消：{ex['name']} {ex['date']} {ex['title']}" if ex else f"未找到ID={sid}"
        return f"未知工具:{name}"
    except Exception as e:
        return f"操作失败:{e}"

# 会话存储（简单内存，生产可用 Redis）
_sessions = {}

def ai_process(session_id: str, content: str) -> str:
    msgs = _sessions.get(session_id) or [{"role":"system","content":_system_prompt()}]
    msgs.append({"role":"user","content":content})
    resp = _chat(messages=msgs, tools=TOOLS)
    msg = resp["choices"][0]["message"]
    tool_calls = msg.get("tool_calls", [])
    content_raw = msg.get("content", "")

    # 检查原始标签
    if not tool_calls and content_raw and ("<invoke " in content_raw or "<tool_calls>" in content_raw):
        msgs.append({"role":"assistant","content":content_raw})
        tag_calls = _parse_tool_tags(content_raw)
        for i, (tname, targs) in enumerate(tag_calls):
            result = _call_tool(tname, targs)
            msgs.append({"role":"tool","tool_call_id":f"call_{i}","content":result})
        resp2 = _chat(messages=msgs)
        reply = resp2["choices"][0]["message"].get("content","好的")
    elif tool_calls:
        msgs.append({"role":"assistant","content":content_raw,"tool_calls":tool_calls})
        for tc in tool_calls:
            fn = tc["function"]; targs = json.loads(fn.get("arguments","{}"))
            result = _call_tool(fn["name"], targs)
            msgs.append({"role":"tool","tool_call_id":tc["id"],"content":result})
        resp2 = _chat(messages=msgs)
        reply = resp2["choices"][0]["message"].get("content","好的")
    else:
        reply = content_raw or "好的"

    msgs.append({"role":"assistant","content":reply})
    _sessions[session_id] = msgs[-20:]
    return reply

# ============ API 路由 ============

class ScheduleCreate(BaseModel):
    name: str
    title: str
    date: str
    department: Optional[str] = None
    time_start: Optional[str] = None
    time_end: Optional[str] = None
    location: Optional[str] = None
    remark: Optional[str] = None

class ScheduleUpdate(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    date: Optional[str] = None
    department: Optional[str] = None
    time_start: Optional[str] = None
    time_end: Optional[str] = None
    location: Optional[str] = None
    remark: Optional[str] = None
    status: Optional[str] = None

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = "default"

@app.on_event("startup")
def startup():
    init_db()
    logger.info("数据库初始化完成")

@app.get("/api/schedules")
def api_query(date_from: Optional[str] = None, date_to: Optional[str] = None,
              name: Optional[str] = None, date: Optional[str] = None,
              department: Optional[str] = None, keyword: Optional[str] = None):
    results = db_query(name=name, date_from=date_from, date_to=date_to,
                       date=date, keyword=keyword, department=department)
    return {"data": results}

@app.post("/api/schedules")
def api_create(body: ScheduleCreate):
    s = db_create(body.dict())
    return {"data": s}

@app.put("/api/schedules/{sid}")
def api_update(sid: int, body: ScheduleUpdate):
    s = db_update(sid, {k:v for k,v in body.dict().items() if v is not None})
    if not s:
        raise HTTPException(404, "未找到")
    return {"data": s}

@app.delete("/api/schedules/{sid}")
def api_delete(sid: int):
    ex = db_get(sid)
    if not ex:
        raise HTTPException(404, "未找到")
    db_delete(sid)
    return {"msg": "已取消"}

@app.post("/api/chat")
def api_chat(body: ChatRequest):
    if not DEEPSEEK_API_KEY:
        raise HTTPException(500, "未配置 DEEPSEEK_API_KEY")
    reply = ai_process(body.session_id, body.message)
    return {"reply": reply}

@app.get("/api/week")
def api_week(offset: int = 0):
    """获取指定周的日期范围（offset=0本周，offset=1下周，offset=-1上周）"""
    t = dt.now()
    mon = t - td(days=t.weekday()) + td(weeks=offset)
    sun = mon + td(days=6)
    results = db_query(date_from=mon.strftime("%Y-%m-%d"), date_to=sun.strftime("%Y-%m-%d"))
    return {
        "date_from": mon.strftime("%Y-%m-%d"),
        "date_to": sun.strftime("%Y-%m-%d"),
        "data": results
    }

# ============ 前端页面 ============
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=get_html())

def get_html():
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>部门日程秘书</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;background:#f7f7f7;color:#222;font-size:14px}
  .container{max-width:700px;margin:0 auto;padding:16px}
  .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;padding:12px 16px;background:#fff;border-radius:12px;border:0.5px solid #e0e0e0}
  .header h1{font-size:16px;font-weight:500}
  .week-nav{display:flex;align-items:center;gap:8px;font-size:13px;color:#666}
  .nav-btn{background:none;border:0.5px solid #ccc;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;color:#333}
  .nav-btn:hover{background:#f0f0f0}
  .day-card{background:#fff;border:0.5px solid #e8e8e8;border-radius:12px;margin-bottom:8px;overflow:hidden}
  .day-header{display:flex;align-items:center;gap:10px;padding:9px 14px;background:#fafafa;border-bottom:0.5px solid #eee}
  .day-label{font-weight:500;min-width:50px}
  .day-date{font-size:12px;color:#999}
  .today-badge{font-size:11px;background:#e8f0fe;color:#1a56db;border-radius:4px;padding:1px 7px}
  .events{padding:0 14px}
  .event-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:0.5px solid #f0f0f0}
  .event-row:last-child{border-bottom:none}
  .event-time{font-size:12px;color:#999;min-width:80px}
  .event-dot{width:7px;height:7px;border-radius:50%;background:#4a90e2;flex-shrink:0}
  .event-name{font-size:12px;color:#888;min-width:50px}
  .event-title{flex:1;font-size:13px}
  .event-loc{font-size:12px;color:#aaa}
  .event-actions{display:flex;gap:4px}
  .btn-sm{font-size:11px;border:0.5px solid #ddd;background:none;border-radius:4px;padding:2px 7px;cursor:pointer;color:#666}
  .btn-sm:hover{background:#f5f5f5}
  .btn-del:hover{color:#e53935;border-color:#e53935}
  .no-events{padding:12px 0;color:#bbb;font-size:13px}
  .actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
  .action-card{background:#fff;border:0.5px solid #e8e8e8;border-radius:12px;padding:14px;cursor:pointer;transition:border-color .15s}
  .action-card:hover{border-color:#4a90e2}
  .action-title{font-size:13px;font-weight:500;margin-bottom:4px}
  .action-desc{font-size:12px;color:#999}
  .panel{background:#fff;border:0.5px solid #e8e8e8;border-radius:12px;padding:16px;margin-bottom:16px;display:none}
  .panel.active{display:block}
  .panel-title{font-size:14px;font-weight:500;margin-bottom:14px;display:flex;align-items:center;justify-content:space-between}
  .close-btn{font-size:18px;cursor:pointer;color:#999;border:none;background:none;line-height:1}
  .form-row{margin-bottom:12px}
  label{display:block;font-size:12px;color:#888;margin-bottom:4px}
  input,select,textarea{width:100%;border:0.5px solid #ddd;border-radius:8px;padding:8px 10px;font-size:13px;outline:none}
  input:focus,select:focus,textarea:focus{border-color:#4a90e2}
  .form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .btn-row{display:flex;gap:8px;margin-top:14px}
  .btn{border-radius:8px;padding:9px 16px;font-size:13px;cursor:pointer;border:none}
  .btn-primary{background:#1a56db;color:#fff;flex:2}
  .btn-primary:hover{background:#1648c0}
  .btn-cancel{background:#f3f3f3;color:#666;flex:1}
  .btn-cancel:hover{background:#e8e8e8}
  .chat-panel{background:#fff;border:0.5px solid #e8e8e8;border-radius:12px;padding:16px;margin-bottom:16px}
  .chat-title{font-size:14px;font-weight:500;margin-bottom:12px}
  .chat-input-row{display:flex;gap:8px}
  .chat-input{flex:1;border:0.5px solid #ddd;border-radius:8px;padding:8px 12px;font-size:13px;outline:none}
  .chat-input:focus{border-color:#4a90e2}
  .chat-send{background:#1a56db;color:#fff;border:none;border-radius:8px;padding:8px 16px;cursor:pointer;font-size:13px}
  .chat-messages{margin-bottom:12px;max-height:220px;overflow-y:auto}
  .msg{padding:8px 10px;border-radius:8px;margin-bottom:6px;font-size:13px;line-height:1.6}
  .msg-user{background:#e8f0fe;text-align:right}
  .msg-bot{background:#f5f5f5}
  .msg-bot table{border-collapse:collapse;width:100%;margin-top:4px}
  .msg-bot th,.msg-bot td{border:0.5px solid #ddd;padding:4px 8px;font-size:12px}
  .msg-bot th{background:#f0f0f0}
  .loading{color:#aaa;font-size:12px;text-align:center;padding:8px}
  .toast{position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:#333;color:#fff;padding:8px 18px;border-radius:20px;font-size:13px;z-index:999;display:none}
  .tag{font-size:11px;padding:1px 6px;border-radius:4px;background:#e8f0fe;color:#1a56db;margin-left:4px}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>部门日程秘书</h1>
    <div class="week-nav">
      <button class="nav-btn" onclick="changeWeek(-1)">&#8249; 上周</button>
      <span id="weekLabel"></span>
      <button class="nav-btn" onclick="changeWeek(1)">下周 &#8250;</button>
    </div>
  </div>

  <div id="scheduleList"></div>

  <div class="actions">
    <div class="action-card" onclick="showPanel('addPanel')">
      <div class="action-title">＋ 新增行程</div>
      <div class="action-desc">添加成员日程安排</div>
    </div>
    <div class="action-card" onclick="showPanel('chatPanel')">
      <div class="action-title">&#128172; AI 秘书</div>
      <div class="action-desc">自然语言操作日程</div>
    </div>
  </div>

  <!-- 新增面板 -->
  <div class="panel" id="addPanel">
    <div class="panel-title">
      新增行程
      <button class="close-btn" onclick="hidePanel('addPanel')">×</button>
    </div>
    <div class="form-grid">
      <div class="form-row">
        <label>姓名 *</label>
        <input id="f_name" type="text" placeholder="张三">
      </div>
      <div class="form-row">
        <label>部门</label>
        <input id="f_dept" type="text" placeholder="技术部">
      </div>
    </div>
    <div class="form-row">
      <label>日程标题 *</label>
      <input id="f_title" type="text" placeholder="例：产品评审会">
    </div>
    <div class="form-grid">
      <div class="form-row">
        <label>日期 *</label>
        <input id="f_date" type="date">
      </div>
      <div class="form-row">
        <label>地点</label>
        <input id="f_loc" type="text" placeholder="会议室A">
      </div>
    </div>
    <div class="form-grid">
      <div class="form-row">
        <label>开始时间</label>
        <input id="f_start" type="time">
      </div>
      <div class="form-row">
        <label>结束时间</label>
        <input id="f_end" type="time">
      </div>
    </div>
    <div class="form-row">
      <label>备注</label>
      <textarea id="f_remark" rows="2" placeholder="其他信息..."></textarea>
    </div>
    <div class="btn-row">
      <button class="btn btn-cancel" onclick="hidePanel('addPanel')">取消</button>
      <button class="btn btn-primary" onclick="submitAdd()">确认新增</button>
    </div>
  </div>

  <!-- 编辑面板 -->
  <div class="panel" id="editPanel">
    <div class="panel-title">
      修改行程
      <button class="close-btn" onclick="hidePanel('editPanel')">×</button>
    </div>
    <input type="hidden" id="e_id">
    <div class="form-grid">
      <div class="form-row">
        <label>姓名</label>
        <input id="e_name" type="text">
      </div>
      <div class="form-row">
        <label>部门</label>
        <input id="e_dept" type="text">
      </div>
    </div>
    <div class="form-row">
      <label>日程标题</label>
      <input id="e_title" type="text">
    </div>
    <div class="form-grid">
      <div class="form-row">
        <label>日期</label>
        <input id="e_date" type="date">
      </div>
      <div class="form-row">
        <label>地点</label>
        <input id="e_loc" type="text">
      </div>
    </div>
    <div class="form-grid">
      <div class="form-row">
        <label>开始时间</label>
        <input id="e_start" type="time">
      </div>
      <div class="form-row">
        <label>结束时间</label>
        <input id="e_end" type="time">
      </div>
    </div>
    <div class="form-row">
      <label>备注</label>
      <textarea id="e_remark" rows="2"></textarea>
    </div>
    <div class="btn-row">
      <button class="btn btn-cancel" onclick="hidePanel('editPanel')">取消</button>
      <button class="btn btn-primary" onclick="submitEdit()">保存修改</button>
    </div>
  </div>

  <!-- AI 聊天面板 -->
  <div class="panel" id="chatPanel">
    <div class="panel-title">
      AI 秘书对话
      <button class="close-btn" onclick="hidePanel('chatPanel')">×</button>
    </div>
    <div class="chat-messages" id="chatMessages">
      <div class="msg msg-bot">你好！我是部门日程秘书。你可以直接告诉我要做什么，例如：<br>
      · 查询张三本周的安排<br>
      · 帮我添加下周三下午2点的产品评审会<br>
      · 把张三周四的会议改到周五
      </div>
    </div>
    <div class="chat-input-row">
      <input class="chat-input" id="chatInput" type="text" placeholder="输入消息，按回车发送..." onkeydown="if(event.key==='Enter')sendChat()">
      <button class="chat-send" onclick="sendChat()">发送</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let weekOffset = 0;
let currentData = [];

function getWeekRange(offset) {
  const now = new Date();
  const day = now.getDay();
  const mon = new Date(now);
  mon.setDate(now.getDate() - (day === 0 ? 6 : day - 1) + offset * 7);
  const sun = new Date(mon);
  sun.setDate(mon.getDate() + 6);
  return {
    from: fmt(mon), to: fmt(sun),
    label: `${mon.getFullYear()}年${mon.getMonth()+1}月${mon.getDate()}日 — ${sun.getMonth()+1}月${sun.getDate()}日`
  };
}

function fmt(d) {
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
}

function todayStr() { return fmt(new Date()); }

const DAYS = ['周日','周一','周二','周三','周四','周五','周六'];

function changeWeek(dir) { weekOffset += dir; loadWeek(); }

async function loadWeek() {
  const range = getWeekRange(weekOffset);
  document.getElementById('weekLabel').textContent = range.label;
  const res = await fetch(`/api/schedules?date_from=${range.from}&date_to=${range.to}`);
  const json = await res.json();
  currentData = json.data || [];
  renderSchedule(range.from, range.to);
}

function renderSchedule(from, to) {
  const list = document.getElementById('scheduleList');
  list.innerHTML = '';
  const today = todayStr();
  let d = new Date(from);
  const end = new Date(to);
  while (d <= end) {
    const key = fmt(d);
    const dayEvents = currentData.filter(e => e.date === key);
    const isToday = key === today;
    const card = document.createElement('div');
    card.className = 'day-card';
    let evHtml = '';
    if (dayEvents.length === 0) {
      evHtml = '<div class="no-events">暂无安排</div>';
    } else {
      evHtml = dayEvents.map(e => `
        <div class="event-row" data-id="${e.id}">
          <span class="event-time">${e.time_start || '--'}${e.time_end ? '–'+e.time_end : ''}</span>
          <span class="event-dot"></span>
          <span class="event-name">${e.name}</span>
          <span class="event-title">${e.title}${e.location ? '<span class="tag">'+e.location+'</span>' : ''}</span>
          <div class="event-actions">
            <button class="btn-sm" onclick="editEvent(${e.id})">改</button>
            <button class="btn-sm btn-del" onclick="delEvent(${e.id},'${e.name}','${e.title}')">删</button>
          </div>
        </div>`).join('');
    }
    card.innerHTML = `
      <div class="day-header">
        <span class="day-label">${DAYS[d.getDay()]}</span>
        <span class="day-date">${d.getMonth()+1}月${d.getDate()}日</span>
        ${isToday ? '<span class="today-badge">今天</span>' : ''}
      </div>
      <div class="events">${evHtml}</div>`;
    list.appendChild(card);
    d.setDate(d.getDate() + 1);
  }
}

function showPanel(id) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  const el = document.getElementById(id);
  el.classList.add('active');
  el.scrollIntoView({behavior:'smooth', block:'nearest'});
  // 默认填今天日期
  if (id === 'addPanel') {
    const today = todayStr();
    document.getElementById('f_date').value = today;
  }
}

function hidePanel(id) {
  document.getElementById(id).classList.remove('active');
}

function showToast(msg, dur=2000) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', dur);
}

async function submitAdd() {
  const name = document.getElementById('f_name').value.trim();
  const title = document.getElementById('f_title').value.trim();
  const date = document.getElementById('f_date').value;
  if (!name || !title || !date) { showToast('姓名、标题、日期为必填'); return; }
  const body = {name, title, date,
    department: document.getElementById('f_dept').value||null,
    time_start: document.getElementById('f_start').value||null,
    time_end: document.getElementById('f_end').value||null,
    location: document.getElementById('f_loc').value||null,
    remark: document.getElementById('f_remark').value||null};
  const res = await fetch('/api/schedules', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if (res.ok) { showToast('新增成功'); hidePanel('addPanel'); loadWeek(); clearAddForm(); }
  else { showToast('新增失败，请重试'); }
}

function clearAddForm() {
  ['f_name','f_dept','f_title','f_start','f_end','f_loc','f_remark'].forEach(id => document.getElementById(id).value='');
}

function editEvent(id) {
  const ev = currentData.find(e => e.id === id);
  if (!ev) return;
  document.getElementById('e_id').value = id;
  document.getElementById('e_name').value = ev.name||'';
  document.getElementById('e_dept').value = ev.department||'';
  document.getElementById('e_title').value = ev.title||'';
  document.getElementById('e_date').value = ev.date||'';
  document.getElementById('e_start').value = ev.time_start||'';
  document.getElementById('e_end').value = ev.time_end||'';
  document.getElementById('e_loc').value = ev.location||'';
  document.getElementById('e_remark').value = ev.remark||'';
  showPanel('editPanel');
}

async function submitEdit() {
  const id = document.getElementById('e_id').value;
  const body = {
    name: document.getElementById('e_name').value||null,
    department: document.getElementById('e_dept').value||null,
    title: document.getElementById('e_title').value||null,
    date: document.getElementById('e_date').value||null,
    time_start: document.getElementById('e_start').value||null,
    time_end: document.getElementById('e_end').value||null,
    location: document.getElementById('e_loc').value||null,
    remark: document.getElementById('e_remark').value||null,
  };
  const res = await fetch(`/api/schedules/${id}`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if (res.ok) { showToast('修改成功'); hidePanel('editPanel'); loadWeek(); }
  else { showToast('修改失败'); }
}

async function delEvent(id, name, title) {
  if (!confirm(`确认删除：${name} - ${title}？`)) return;
  const res = await fetch(`/api/schedules/${id}`, {method:'DELETE'});
  if (res.ok) { showToast('已删除'); loadWeek(); }
  else { showToast('删除失败'); }
}

// AI 对话
function mdToHtml(text) {
  // 把 Markdown 表格转成 HTML
  const lines = text.split('\\n');
  let out = '', inTable = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (line.startsWith('|') && line.endsWith('|')) {
      if (!inTable) { out += '<table>'; inTable = true; }
      const cells = line.slice(1,-1).split('|');
      if (cells.every(c => /^[-: ]+$/.test(c))) continue; // 分隔行
      const tag = (i === 0 || !lines[i-1] || !lines[i-1].trim().startsWith('|')) ? 'th' : 'td';
      out += '<tr>' + cells.map(c => `<${tag}>${c.trim()}</${tag}>`).join('') + '</tr>';
    } else {
      if (inTable) { out += '</table>'; inTable = false; }
      out += (line || '<br>');
    }
  }
  if (inTable) out += '</table>';
  return out;
}

async function sendChat() {
  const input = document.getElementById('chatInput');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  const msgs = document.getElementById('chatMessages');
  msgs.innerHTML += `<div class="msg msg-user">${msg}</div>`;
  msgs.innerHTML += `<div class="msg msg-bot loading" id="loadingMsg">思考中...</div>`;
  msgs.scrollTop = msgs.scrollHeight;
  try {
    const res = await fetch('/api/chat', {method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message: msg, session_id: 'web_user'})});
    const data = await res.json();
    document.getElementById('loadingMsg').remove();
    const reply = data.reply || '好的';
    msgs.innerHTML += `<div class="msg msg-bot">${mdToHtml(reply)}</div>`;
    msgs.scrollTop = msgs.scrollHeight;
    // AI 操作完后刷新日程列表
    loadWeek();
  } catch(e) {
    document.getElementById('loadingMsg').remove();
    msgs.innerHTML += `<div class="msg msg-bot">请求失败，请重试</div>`;
  }
}

// 初始化
loadWeek();
</script>
</body>
</html>"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)

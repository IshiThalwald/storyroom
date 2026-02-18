from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from google.oauth2 import service_account
from google.auth.transport.requests import Request as AuthRequest
import httpx
import json
import os
import time
from datetime import datetime
from collections import deque
from threading import Lock

app = FastAPI()

PASSWORD = os.getenv("PASSWORD", "123456")
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")
GCP_MODEL = os.getenv("GCP_MODEL", "gemini-2.0-flash-001")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

GCP_PROJECT_ID = ""
credentials_info = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

class MemoryLogger:
    def __init__(self, max_logs=100):
        self.logs = deque(maxlen=max_logs)
        self.lock = Lock()
    
    def add(self, level, message):
        with self.lock:
            entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "msg": message[:100]
            }
            self.logs.append(entry)

logger = MemoryLogger()
access_token = None
token_expiry = 0

def init_credentials():
    global access_token, token_expiry, GCP_PROJECT_ID, credentials_info
    
    json_str = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
    if not json_str:
        logger.add("error", "未配置GOOGLE_CREDENTIALS_JSON")
        return False
    
    try:
        credentials_info = json.loads(json_str)
        GCP_PROJECT_ID = credentials_info.get("project_id", "")
        if not GCP_PROJECT_ID:
            logger.add("error", "JSON中缺少project_id")
            return False
            
        creds = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(AuthRequest())
        access_token = creds.token
        token_expiry = time.time() + 1800
        logger.add("info", f"凭证加载: {GCP_PROJECT_ID[:8]}...")
        return True
    except Exception as e:
        logger.add("error", f"凭证失败: {str(e)}")
        return False

if not init_credentials():
    logger.add("warning", "启动时未加载凭证，将在首次请求时重试")

def get_token():
    global access_token, token_expiry
    if time.time() > token_expiry - 300:
        init_credentials()
    return access_token

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Vertex控制台</title>
    <style>
        body { font-family: Arial; padding: 20px; max-width: 800px; margin: 0 auto; background: #f5f5f5; }
        .card { background: white; padding: 20px; margin: 15px 0; border-radius: 8px; }
        .log-entry { font-family: monospace; font-size: 12px; padding: 5px; border-bottom: 1px solid #eee; }
        .error { color: red; }
        .success { color: green; }
        input, textarea { width: 100%; padding: 8px; margin: 5px 0; border: 1px solid #ddd; }
        button { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 4px; }
        .stats { display: flex; gap: 20px; flex-wrap: wrap; }
        .stat-box { background: #e9ecef; padding: 15px; border-radius: 5px; flex: 1; min-width: 120px; }
    </style>
</head>
<body>
    <h2>Vertex代理控制台</h2>
    
    <div class="card">
        <h3>状态</h3>
        <div class="stats">
            <div class="stat-box"><div>项目ID</div><strong id="project">-</strong></div>
            <div class="stat-box"><div>模型</div><strong id="model">-</strong></div>
            <div class="stat-box"><div>凭证</div><strong id="cred">-</strong></div>
        </div>
    </div>

    <div class="card">
        <h3>最近日志</h3>
        <div id="logs" style="max-height: 300px; overflow-y: auto; background: #f8f9fa; padding: 10px; border-radius: 4px;">
            加载中...
        </div>
        <button onclick="loadLogs()" style="margin-top: 10px;">刷新</button>
    </div>

    <div class="card">
        <h3>更新凭证</h3>
        <input type="password" id="pwd" placeholder="Dashboard密码" style="margin-bottom: 10px;">
        <textarea id="json" rows="8" placeholder="粘贴JSON凭证（会自动提取project_id）"></textarea>
        <button onclick="updateKey()">更新</button>
        <p id="msg" style="margin-top: 10px;"></p>
    </div>

    <script>
        const PWD = new URLSearchParams(location.search).get('pwd') || '';
        
        async function loadStatus() {
            const r = await fetch('/api/status?pwd=' + PWD);
            const d = await r.json();
            document.getElementById('project').innerText = d.project || '未配置';
            document.getElementById('model').innerText = d.model;
            document.getElementById('cred').innerText = d.cred ? '✅有效' : '❌无效';
        }
        
        async function loadLogs() {
            const r = await fetch('/api/logs?pwd=' + PWD);
            const logs = await r.json();
            document.getElementById('logs').innerHTML = logs.map(l => 
                `<div class="log-entry ${l.level}">[${l.time}] ${l.level}: ${l.msg}</div>`
            ).join('') || '<div style="color: #999;">无日志</div>';
        }
        
        async function updateKey() {
            const r = await fetch('/api/update', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    pwd: document.getElementById('pwd').value,
                    json: document.getElementById('json').value
                })
            });
            const res = await r.json();
            const msg = document.getElementById('msg');
            msg.innerText = res.message;
            msg.style.color = res.success ? 'green' : 'red';
            if(res.success) {
                loadStatus();
                document.getElementById('json').value = '';
            }
        }
        
        loadStatus();
        loadLogs();
        setInterval(loadLogs, 10000);
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content=DASHBOARD_HTML)

@app.get("/api/status")
async def status(pwd: str = ""):
    if pwd != PASSWORD:
        raise HTTPException(401, "密码错误")
    return {
        "project": GCP_PROJECT_ID,
        "model": GCP_MODEL,
        "cred": access_token is not None
    }

@app.get("/api/logs")
async def get_logs(pwd: str = ""):
    if pwd != PASSWORD:
        raise HTTPException(401, "密码错误")
    return list(logger.logs)

@app.post("/api/update")
async def update(data: dict):
    if data.get("pwd") != PASSWORD:
        return {"success": False, "message": "密码错误"}
    try:
        info = json.loads(data.get("json", ""))
        if not all(k in info for k in ["type", "project_id", "private_key", "client_email"]):
            return {"success": False, "message": "JSON缺少必要字段"}
        
        os.environ["GOOGLE_CREDENTIALS_JSON"] = data.get("json")
        if init_credentials():
            return {"success": True, "message": f"更新成功！项目ID: {GCP_PROJECT_ID}"}
        else:
            return {"success": False, "message": "JSON格式正确但无法通过Google验证"}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.post("/v1/chat/completions")
async def chat(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {PASSWORD}":
        raise HTTPException(401, "Unauthorized")
    
    if not access_token:
        if not init_credentials():
            raise HTTPException(500, "凭证未配置或无效")
    
    try:
        body = await request.json()
        is_stream = body.get("stream", False)
        
        base_url = f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/publishers/google/models/{GCP_MODEL}"
        url = f"{base_url}:streamGenerateContent" if is_stream else f"{base_url}:generateContent"
        
        user_text = body.get("messages", [{}])[-1].get("content", "")
        vertex_body = {
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {
                "temperature": body.get("temperature", 0.7),
                "maxOutputTokens": body.get("max_tokens", 2048)
            }
        }
        
        logger.add("info", f"{'流式' if is_stream else '非流式'}: {user_text[:20]}...")
        
        if is_stream:
            async def stream_response():
                try:
                    async with httpx.AsyncClient() as client:
                        async with client.stream(
                            "POST", 
                            url,
                            headers={"Authorization": f"Bearer {get_token()}"},
                            json=vertex_body,
                            timeout=60.0
                        ) as response:
                            async for line in response.aiter_lines():
                                if line and line.startswith("data: "):
                                    try:
                                        data = json.loads(line[6:])
                                        candidates = data.get("candidates", [{}])
                                        if candidates:
                                            parts = candidates[0].get("content", {}).get("parts", [{}])
                                            text = parts[0].get("text", "")
                                            if text:
                                                chunk = {
                                                    "id": "chatcmpl-vertex",
                                                    "object": "chat.completion.chunk",
                                                    "created": int(time.time()),
                                                    "model": GCP_MODEL,
                                                    "choices": [{
                                                        "index": 0,
                                                        "delta": {"content": text},
                                                        "finish_reason": None
                                                    }]
                                                }
                                                yield f"data: {json.dumps(chunk)}\n\n"
                                    except:
                                        continue
                            yield "data: [DONE]\n\n"
                except Exception as e:
                    logger.add("error", f"流式错误: {str(e)}")
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
            
            return StreamingResponse(
                stream_response(),
                media_type="text/event-stream"
            )
        
        else:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {get_token()}"},
                    json=vertex_body,
                    timeout=60.0
                )
                
                if response.status_code != 200:
                    logger.add("error", f"Vertex错误: {response.status_code}")
                    raise HTTPException(response.status_code, response.text)
                
                result = response.json()
                candidates = result.get("candidates", [{}])
                text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                
                logger.add("success", f"完成: {text[:20]}...")
                
                return {
                    "id": "chatcmpl-vertex",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": GCP_MODEL,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop"
                    }]
                }
                
    except Exception as e:
        logger.add("error", f"处理失败: {str(e)}")
        raise HTTPException(500, str(e))

@app.get("/health")
async def health():
    return {"status": "ok", "project": GCP_PROJECT_ID, "cred_valid": access_token is not None}

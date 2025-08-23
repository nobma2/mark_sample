# main.py
from fastapi import FastAPI, UploadFile, File, Form, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os, json, base64, httpx, re

# ==== 環境変数（Cloud Run で設定）====
GEMINI_API_KEY = os.environ["GOOGLE_API_KEY"]          # Secret Manager を環境変数として注入
MODEL = os.getenv("MODEL_NAME", "gemini-1.5-flash")    # うまく整形されない場合は gemini-1.5-pro を推奨

# ==== プロンプト（最小・厳格）====
PROMPT_DEFAULT = (
    "建物外観を判定し、label/confidence/reason の3項目のみを返してください。"
    "説明・コードブロックは禁止。"
)

# ==== FastAPI アプリ ====
app = FastAPI(title="Gemini Image Inspector API", version="1.2.0")

# ブラウザ直叩き用（Excelからは不要）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ---------- Gemini 呼び出し（JSONスキーマ強制＋セーフティ緩和） ----------
async def call_gemini(image_bytes: bytes, mime: str, prompt: str) -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"

    body = {
        "generationConfig": {
            "temperature": 0.0,
            "topP": 0.8,
            "maxOutputTokens": 64,
            "response_mime_type": "application/json",
            "response_schema": {
                "type": "object",
                "properties": {
                    "label":      {"type": "string", "enum": ["normal", "abnormal"]},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reason":     {"type": "string", "maxLength": 40}
                },
                "required": ["label", "confidence", "reason"],
                "additionalProperties": False
            }
        },
        "safetySettings": [
            {"category":"HARM_CATEGORY_HATE_SPEECH","threshold":"BLOCK_NONE"},
            {"category":"HARM_CATEGORY_HARASSMENT","threshold":"BLOCK_NONE"},
            {"category":"HARM_CATEGORY_SEXUALLY_EXPLICIT","threshold":"BLOCK_NONE"},
            {"category":"HARM_CATEGORY_DANGEROUS_CONTENT","threshold":"BLOCK_NONE"}
        ],
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": mime, "data": b64}},
                {"text": prompt}
            ]
        }]
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=body)
        r.raise_for_status()
        return r.json()

# ---------- 応答パース（複数parts結合＋救済） ----------
def parse_response(j: dict):
    """
    candidates[0].content.parts[*].text を連結し JSON を抽出。
    スキーマ強制が効けば素直に parse、崩れたら { ... } 抽出で救済。
    """
    try:
        # parts を結合
        text_chunks = []
        cands = j.get("candidates", [])
        if cands:
            parts = cands[0].get("content", {}).get("parts", [])
            for p in parts:
                t = p.get("text")
                if t:
                    text_chunks.append(t)
        text = "".join(text_chunks).strip()

        # 1) 素直に JSON として読む
        try:
            jr = json.loads(text)
            return str(jr.get("label","")), float(jr.get("confidence",0.0)), str(jr.get("reason",""))
        except Exception:
            pass

        # 2) テキスト内の { ... } を抽出して再トライ
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            jr = json.loads(m.group(0))
            return str(jr.get("label","")), float(jr.get("confidence",0.0)), str(jr.get("reason",""))

        # 3) だめなら空
        return "", 0.0, ""
    except Exception as e:
        return "", 0.0, f"parse_error:{e}"

# ---------- ノート整形 ----------
def build_note(label: str, conf: float, reason: str, hi=0.75, lo=0.55) -> str:
    if label ==

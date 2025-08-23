from fastapi import FastAPI, UploadFile, File, Form, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os, json, base64, httpx, re

# ==== 環境変数（Cloud Run で設定）====
GEMINI_API_KEY = os.environ["GOOGLE_API_KEY"]
MODEL = os.getenv("MODEL_NAME", "gemini-1.5-flash")   # 安定させたいなら gemini-1.5-pro を推奨

# ==== プロンプト ====
PROMPT_DEFAULT = (
    "建物外観を判定し、label/confidence/reason の3項目のみを返してください。"
    "説明・コードブロックは禁止。"
)

# ==== FastAPI アプリ ====
app = FastAPI(title="Gemini Image Inspector API", version="1.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ---------- Gemini 呼び出し ----------
async def call_gemini(image_bytes: bytes, mime: str, prompt: str) -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"

    body = {
        "generationConfig": {
            "temperature": 0.0,
            "topP": 0.8,
            "maxOutputTokens": 64,
            "response_mime_type": "application/json",
            # ★ 最小限のスキーマ（追加プロパティは指定しない）
            "response_schema": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "enum": ["normal", "abnormal"]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"}
                },
                "required": ["label", "confidence", "reason"]
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

# ---------- 応答パース ----------
def parse_response(j: dict):
    try:
        # parts を全部結合
        text_chunks = []
        cands = j.get("candidates", [])
        if cands:
            parts = cands[0].get("content", {}).get("parts", [])
            for p in parts:
                t = p.get("text")
                if t:
                    text_chunks.append(t)
        text = "".join(text_chunks).strip()

        # 1) JSON として読む
        try:
            jr = json.loads(text)
            return str(jr.get("label","")), float(jr.get("confidence",0.0)), str(jr.get("reason",""))
        except Exception:
            pass

        # 2) { ... } 抽出で再パース
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            jr = json.loads(m.group(0))
            return str(jr.get("label","")), float(jr.get("confidence",0.0)), str(jr.get("reason",""))

        # 3) ダメなら空
        return "", 0.0, ""
    except Exception as e:
        return "", 0.0, f"parse_error:{e}"

# ---------- ノート整形 ----------
def build_note(label: str, conf: float, reason: str, hi=0.75, lo=0.55) -> str:
    if label == "abnormal":
        if conf >= hi:
            phrase = "異常が確認されました。至急対応してください。"
        elif conf >= lo:
            phrase = "異常の可能性があります。目視確認をお願いします。"
        else:
            phrase = "異常の可能性があります。"
    elif label == "normal":
        phrase = "異常は確認されませんでした。"
    else:
        phrase = "判定に失敗しました。"
    return f"{phrase}（根拠:{reason}）" if reason else phrase

# ---------- エンドポイント ----------
@app.get("/ping")
def ping():
    return {
        "ok": True,
        "model": MODEL,
        "has_api_key": bool(GEMINI_API_KEY),
        "version": "1.3.0"
    }

@app.post("/infer")
async def infer(
    file: UploadFile = File(...),
    mime: str = Form("image/png"),
    prompt: str = Form(PROMPT_DEFAULT),
    debug: int = Query(0, ge=0, le=1)
):
    try:
        img = await file.read()
        if not img:
            return JSONResponse({"error": "empty file"}, status_code=400)

        raw = await call_gemini(img, mime, prompt)
        label, conf, reason = parse_response(raw)
        note = build_note(label, conf, reason)

        return JSONResponse({
            "label": label,
            "confidence": conf,
            "reason": reason,
            "note": note,
            "raw": (raw if debug == 1 else None)
        })
    except httpx.HTTPStatusError as e:
        return JSONResponse({"error": f"gemini_api:{e.response.text}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

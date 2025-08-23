from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os, json, base64, httpx

# --- 環境変数（Cloud Run 側で Secret を環境変数としてマウント） ---
GEMINI_API_KEY = os.environ["GOOGLE_API_KEY"]  # Secret Manager から注入
MODEL = os.getenv("MODEL_NAME", "gemini-1.5-flash")

# --- プロンプトのデフォルト（必要に応じて短く変更可） ---
PROMPT_DEFAULT = (
    "あなたは建物外観の安全点検AIです。\n"
    "入力画像を確認して、以下のJSON「のみ」を返してください。文章や説明は不要です。\n"
    "{\n"
    "  \"label\": \"normal\" または \"abnormal\",\n"
    "  \"confidence\": 数値 (0.0〜1.0),\n"
    "  \"reason\": \"40文字以内の日本語の根拠\"\n"
    "}"
)


# --- FastAPI アプリ本体 ---
app = FastAPI(title="Gemini Image Inspector API", version="1.0.0")

# 必要ならCORS解放（Excelデスクトップからは不要、ブラウザ直叩き時のみ）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 必要に応じて絞ってください
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== ヘルパ ==========

async def call_gemini(image_bytes: bytes, mime: str, prompt: str) -> dict:
    """Generative Language API を叩いて JSON を返す（HTTP生返却）"""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"
    body = {
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

def parse_response(j: dict):
    """
    Gemini の応答から text を取り出し、JSONを想定してパース。
    想定外の場合は text を reason として返す。
    """
    text = ""
    try:
        cands = j.get("candidates", [])
        if cands:
            parts = cands[0].get("content", {}).get("parts", [])
            if parts and "text" in parts[0]:
                text = parts[0]["text"]
        jr = json.loads(text)  # 期待形式のJSONを抽出
        label = str(jr.get("label", ""))
        conf = float(jr.get("confidence", 0.0))
        reason = str(jr.get("reason", ""))
        return label, conf, reason
    except Exception:
        # 想定外応答は text をそのまま短く reason に入れる
        return "", 0.0, (text[:100] if text else "")

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

# ========== エンドポイント ==========

@app.get("/ping")
async def ping():
    # シークレットが注入されているか最低限の確認（値は出さない）
    has_key = bool(GEMINI_API_KEY)
    return {"ok": True, "model": MODEL, "has_api_key": has_key}

@app.post("/infer")
async def infer(
    file: UploadFile = File(...),                   # 画像ファイル（multipart）
    mime: str = Form("image/png"),                  # MIMEタイプ（既定: PNG）
    prompt: str = Form(PROMPT_DEFAULT)              # プロンプト（任意差し替え可）
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
            "raw": None  # デバッグ時は raw を返すならここを raw に
        })
    except httpx.HTTPStatusError as e:
        return JSONResponse({"error": f"gemini_api:{e.response.text}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

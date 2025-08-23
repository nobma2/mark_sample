# main.py
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os, json, base64, httpx, re

# ==== 環境変数（Cloud Run で設定）====
GEMINI_API_KEY = os.environ["GOOGLE_API_KEY"]          # Secret Manager を環境変数として注入
MODEL = os.getenv("MODEL_NAME", "gemini-1.5-flash")

# ==== プロンプト（厳格JSON指定）====
PROMPT_DEFAULT = (
    "あなたは建物外観の安全点検AIです。\n"
    "以下のJSON『のみ』を返してください。コードブロックや説明は一切禁止です。\n"
    "{"
    "\"label\":\"normal\" または \"abnormal\","
    "\"confidence\": 数値(0.0〜1.0),"
    "\"reason\":\"40文字以内の日本語の根拠\""
    "}"
)

# ==== FastAPI アプリ ====
app = FastAPI(title="Gemini Image Inspector API", version="1.1.0")

# ブラウザ直叩きしたい場合だけCORS解放（Excelデスクトップからは不要）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ---------- Gemini 呼び出し ----------
async def call_gemini(image_bytes: bytes, mime: str, prompt: str) -> dict:
    """Generative Language API を叩いて応答JSONを返す"""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"
    body = {
        # JSONだけ返させる／安定化
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.8,
            "response_mime_type": "application/json"
        },
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

# ---------- 応答パース（頑健化） ----------
def parse_response(j: dict):
    """
    candidates[0].content.parts[0].text に入る想定の JSON を取り出す。
    JSON以外が混じる場合は { ... } 部分を抽出して再パース。
    それでもダメならテキスト先頭を reason として返す。
    """
    text = ""
    try:
        cands = j.get("candidates", [])
        if cands:
            parts = cands[0].get("content", {}).get("parts", [])
            if parts and "text" in parts[0]:
                text = parts[0]["text"]

        # 1st: そのままJSONとして
        try:
            jr = json.loads(text)
            return str(jr.get("label","")), float(jr.get("confidence",0.0)), str(jr.get("reason",""))
        except Exception:
            pass

        # 2nd: テキスト内の { ... } を抽出して再パース
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            jr = json.loads(m.group(0))
            return str(jr.get("label","")), float(jr.get("confidence",0.0)), str(jr.get("reason",""))

        # 3rd: ダメならそのまま短く
        return "", 0.0, (text[:100] if text else "")
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
    # 値は出さないが、キーが注入されているかだけ確認用
    return {"ok": True, "model": MODEL, "has_api_key": bool(GEMINI_API_KEY)}

@app.post("/infer")
async def infer(
    file: UploadFile = File(...),            # 画像（multipart）
    mime: str = Form("image/png"),           # MIME
    prompt: str = Form(PROMPT_DEFAULT)       # プロンプト（上書き可）
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
            "raw": None  # デバッグ時は raw を返したいならここを raw に
        })
    except httpx.HTTPStatusError as e:
        return JSONResponse({"error": f"gemini_api:{e.response.text}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

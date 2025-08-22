from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
import base64, os, json, httpx

app = FastAPI()

GEMINI_API_KEY = os.environ["GOOGLE_API_KEY"]
MODEL = os.getenv("MODEL_NAME", "gemini-1.5-flash")

PROMPT_DEFAULT = (
    "あなたは建物外観の安全点検AIです。次のJSONのみ返してください："
    "{\"label\":\"normal|abnormal\",\"confidence\":0.0-1.0,\"reason\":\"日本語40文字以内\"}"
)

async def call_gemini(image_bytes: bytes, mime: str, prompt: str):
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
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=body)
        r.raise_for_status()
        return r.json()

def parse_response(j: dict):
    txt = ""
    try:
        cand = j.get("candidates", [])[0]
        parts = cand.get("content", {}).get("parts", [])
        if parts and "text" in parts[0]:
            txt = parts[0]["text"]
        jr = json.loads(txt)
        label = str(jr.get("label",""))
        conf  = float(jr.get("confidence",0))
        reason= str(jr.get("reason",""))
        return label, conf, reason
    except Exception:
        return "", 0.0, txt[:80]

def build_note(label, conf, reason, hi=0.75, lo=0.55):
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

@app.post("/infer")
async def infer(
    file: UploadFile = File(...),
    mime: str = Form("image/png"),
    prompt: str = Form(PROMPT_DEFAULT)
):
    try:
        img = await file.read()
        gem = await call_gemini(img, mime, prompt)
        label, conf, reason = parse_response(gem)
        note = build_note(label, conf, reason)
        return JSONResponse({"label": label, "confidence": conf, "reason": reason, "note": note})
    except httpx.HTTPStatusError as e:
        return JSONResponse({"error": f"gemini_api:{e.response.text}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

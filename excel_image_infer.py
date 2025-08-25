# excel_image_infer.py
import os, tempfile, json, math, requests
import xlwings as xw

# ==== 設定 ====
SERVICE_URL = "https://image-ai-170895213168.asia-northeast1.run.app/infer"
SHEET_NAME = "写真帳"

# 画像ブロック：5行目開始、21行ピッチ、画像高さは14行（5–18, 26–39, ...）
DATA_START_ROW = 5
BLOCK_PITCH = 21
IMAGE_ROWS = 14  # 5 + 14 = 19 でコメント開始

# コメント領域：H:K の4列 × 4行
COMMENT_COL_START = "H"
COMMENT_COL_END   = "K"
COMMENT_ROWS = 4

# Cloud Run 側のプロンプト（日本語で理由40文字）
DEFAULT_PROMPT = (
    "建物外観を判定し、label/confidence/reason の3項目のみを返してください。"
    "説明・コードブロックは禁止。理由は40文字以内の日本語で。"
)

# ==== 行ユーティリティ ====
def snap_comment_start_row(img_top_row: int) -> int:
    """画像Top行から、そのブロックのコメント開始行を返す"""
    if img_top_row < DATA_START_ROW:
        idx = 0
    else:
        idx = (img_top_row - DATA_START_ROW) // BLOCK_PITCH
    return DATA_START_ROW + IMAGE_ROWS + idx * BLOCK_PITCH  # 5+14=19, 次は 40, ...

def estimate_row_from_top(sht: xw.main.Sheet, top_pts: float) -> int:
    """Top座標(pts)から行番号を概算（TopLeftCellが取れない場合の保険）"""
    y = 0.0
    r = 1
    # 安全上限
    max_rows = sht.cells.last_cell.row
    while r <= max_rows:
        h = sht.range((r, 1)).row_height
        if h is None:
            h = 15.0
        if y + h > top_pts:
            return r
        y += h
        r += 1
    return max_rows

# ==== 画像エクスポート ====
def export_shape_to_png(sht: xw.main.Sheet, shp: xw.main.Shape, out_path: str):
    """図形をPNG保存。失敗時はチャート経由でエクスポート"""
    # 1) ネイティブExport（Windowsでは効くことが多い）
    try:
        shp.api.Export(Filename=out_path, FilterName="PNG")  # Macでは失敗することあり
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return
    except Exception:
        pass
    # 2) チャート経由（Mac/Win両対応）
    try:
        left, top = shp.left, shp.top
        width  = max(shp.width, 100)
        height = max(shp.height, 100)
        co = sht.api.ChartObjects().Add(left, top, width, height)
        co.Activate()
        shp.api.Copy()
        co.Chart.Paste()
        co.Chart.Export(Filename=out_path, FilterName="PNG")
        co.Delete()
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("Chart.Export succeeded but file empty.")
    except Exception as e:
        raise RuntimeError(f"Export failed: {e}")

# ==== API 呼び出し ====
def call_infer_api(png_path: str, prompt: str) -> dict:
    with open(png_path, "rb") as f:
        files = {"file": (os.path.basename(png_path), f, "image/png")}
        data = {"mime": "image/png", "prompt": prompt}
        r = requests.post(SERVICE_URL, files=files, data=data, timeout=60)
    r.raise_for_status()
    return r.json()

# ==== コメント書き込み ====
def write_comment_block(sht: xw.main.Sheet, start_row: int, text: str):
    rng = sht.range(f"{COMMENT_COL_START}{start_row}:{COMMENT_COL_END}{start_row + COMMENT_ROWS - 1}")
    # 既存の結合を解除 → 結合 → 値設定
    try:
        rng.api.UnMerge()
    except Exception:
        pass
    rng.api.Merge()
    rng.value = text
    # 折り返し＆上詰め
    try:
        rng.api.WrapText = True
        rng.api.VerticalAlignment = -4160  # xlTop
    except Exception:
        pass

# ==== メイン ====
def inspect_active_book():
    """開いているブックの「写真帳」シートで実行（xlwings アドイン推奨）"""
    wb = xw.Book.caller()  # Excelから呼ぶ場合
    _inspect_core(wb)

def inspect_file(path: str):
    """ブックパスを渡して実行（外部からの単体実行用）"""
    app = xw.App(visible=False)
    try:
        wb = xw.Book(path)
        _inspect_core(wb)
        wb.save()
    finally:
        wb.close()
        app.quit()

def _inspect_core(wb: xw.main.Book):
    sht = wb.sheets[SHEET_NAME]
    shapes = list(sht.shapes)

    if not shapes:
        xw.msgbox("写真帳に画像（図形）が見つかりませんでした。", "excel_image_infer")
        return

    for shp in shapes:
        # 画像のみに限定
        try:
            if shp.type.lower() != "picture":
                continue
        except Exception:
            # 取得できない場合は名前でゆるく判定
            if "picture" not in shp.name.lower() and "画像" not in shp.name:
                continue

        # 画像の行位置
        try:
            top_row = shp.top_left_cell.row
        except Exception:
            top_row = estimate_row_from_top(sht, shp.top)

        comment_start = snap_comment_start_row(top_row)

        # 画像を書き出してAPIに投げる
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        export_shape_to_png(sht, shp, tmp_path)

        try:
            result = call_infer_api(tmp_path, DEFAULT_PROMPT)
        except Exception as e:
            note = f"判定エラー: {e}"
        else:
            note = result.get("note") or "判定に失敗しました。"

        try:
            os.remove(tmp_path)
        except Exception:
            pass

        # コメント書き込み
        write_comment_block(sht, comment_start, note)

    xw.msgbox("画像判定が完了しました。", "excel_image_infer")

# 直接実行用（Excel外から）
if __name__ == "__main__":
    # 例: python3 excel_image_infer.py "/Users/xxx/写真帳.xlsm"
    import sys
    if len(sys.argv) >= 2:
        inspect_file(sys.argv[1])
    else:
        print("Usage: python3 excel_image_infer.py <path_to_excel>")

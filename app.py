# Dependencies:
# pip install fastapi uvicorn pillow cos-python-sdk-v5 requests python-multipart
import base64
import io
import logging
import os
import uuid
from typing import Tuple

import requests
import xml.etree.ElementTree as ET
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from PIL import Image, UnidentifiedImageError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("puzzle-draw")

COS_IMPORT_ERROR = None
try:
    from qcloud_cos import CosConfig, CosS3Client
except Exception as exc:  # pragma: no cover - only raised when optional runtime dependency is missing
    COS_IMPORT_ERROR = exc
    CosConfig = None
    CosS3Client = None


BUCKET = "my-image-bucket-1446249102"
REGION = "ap-guangzhou"

app = FastAPI(title="拼豆图纸生成器")


class ImageProcessError(Exception):
    pass


def image_to_png_bytes(raw: bytes) -> Tuple[bytes, Image.Image]:
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except UnidentifiedImageError as exc:
        raise ImageProcessError("无法识别图片文件，请上传常见格式图片") from exc

    if image.mode not in ("RGBA", "RGB"):
        image = image.convert("RGBA")

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), image


def get_cos_client() -> CosS3Client:
    if CosConfig is None or CosS3Client is None:
        if COS_IMPORT_ERROR is not None:
            logger.error(
                "COS SDK import failed",
                exc_info=(type(COS_IMPORT_ERROR), COS_IMPORT_ERROR, COS_IMPORT_ERROR.__traceback__),
            )
        raise ImageProcessError("缺少或无法加载 COS 依赖，请先安装 cos-python-sdk-v5")

    secret_id = os.getenv("TENCENT_SECRET_ID")
    secret_key = os.getenv("TENCENT_SECRET_KEY")
    if not secret_id or not secret_key:
        raise ImageProcessError("缺少腾讯云密钥，请先设置 TENCENT_SECRET_ID 和 TENCENT_SECRET_KEY")

    try:
        config = CosConfig(Region=REGION, SecretId=secret_id, SecretKey=secret_key)
        return CosS3Client(config)
    except Exception as exc:
        logger.exception("COS client initialization failed")
        raise ImageProcessError("抠图服务初始化失败，请检查密钥和地域配置") from exc


def remove_background_with_cos(png_bytes: bytes) -> bytes:
    key = f"uploads/{uuid.uuid4().hex}.png"
    params = {"ci-process": "face-effect", "type": "face-segmentation"}
    client = get_cos_client()

    try:
        client.put_object(
            Bucket=BUCKET,
            Body=png_bytes,
            Key=key,
            ContentType="image/png",
        )
    except Exception as exc:
        logger.exception("COS put_object failed: bucket=%s key=%s", BUCKET, key)
        raise ImageProcessError("图片上传到抠图服务失败，请检查 COS 存储桶、地域、密钥权限后重试") from exc

    try:
        signed_url = client.get_presigned_url(
            Method="GET",
            Bucket=BUCKET,
            Key=key,
            Expired=300,
            Params=params,
        )
    except Exception as exc:
        logger.exception("COS presigned processing URL generation failed: bucket=%s key=%s", BUCKET, key)
        raise ImageProcessError("抠图链接生成失败，请检查 COS 数据万象配置后重试") from exc

    try:
        result = requests.get(signed_url, timeout=60)
        result.raise_for_status()
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        if response is not None:
            logger.error(
                "Image processing request failed: status=%s content_type=%s body_prefix=%r",
                response.status_code,
                response.headers.get("content-type"),
                response.content[:500],
            )
        logger.exception("Image processing request failed: bucket=%s key=%s", BUCKET, key)
        raise ImageProcessError("抠图服务暂时不可用，请稍后重试") from exc

    try:
        content_type = result.headers.get("content-type", "")
        if content_type.startswith("application/xml"):
            root = ET.fromstring(result.content)
            for elem in root.iter():
                if elem.tag.endswith("Error") or elem.tag == "Error":
                    code = ""
                    message = ""
                    for child in elem:
                        if child.tag.endswith("Code") or child.tag == "Code":
                            code = child.text or ""
                        if child.tag.endswith("Message") or child.tag == "Message":
                            message = child.text or ""
                    logger.error("CI error response: code=%s message=%s", code, message)
                    raise ImageProcessError(f"抠图服务返回错误：{code} - {message}" if code or message else "抠图服务返回错误，请检查配置")

            image_data = None
            for elem in root.iter():
                if elem.tag.endswith("ResultImage") or elem.tag == "ResultImage":
                    image_data = elem.text
                    break
            if image_data is None:
                raise ImageProcessError("抠图结果中未找到图片数据")
            image_bytes = base64.b64decode(image_data)
        else:
            image_bytes = result.content

        processed = Image.open(io.BytesIO(image_bytes))
        processed.load()
        processed = processed.convert("RGBA")
        output = io.BytesIO()
        processed.save(output, format="PNG")
        return output.getvalue()
    except ET.ParseError as exc:
        logger.error("CI XML parse failed: body_prefix=%r", result.content[:300])
        raise ImageProcessError("抠图结果XML解析失败，请稍后重试") from exc
    except ValueError as exc:
        logger.error("CI base64 decode failed")
        raise ImageProcessError("抠图结果图片数据解码失败，请稍后重试") from exc
    except Exception as exc:
        logger.exception("CI image response parse failed: status=%s content_type=%s body_prefix=%r", result.status_code, content_type, result.content[:300])
        raise ImageProcessError("抠图结果解析失败，请稍后重试") from exc


async def process_uploaded_image(file: UploadFile, remove_bg: bool) -> bytes:
    if not file.content_type or not file.content_type.startswith("image/"):
        raise ImageProcessError("请上传图片文件")

    raw = await file.read()
    if not raw:
        raise ImageProcessError("上传的图片为空，请重新选择")

    png_bytes, _ = image_to_png_bytes(raw)
    if not remove_bg:
        return png_bytes

    return remove_background_with_cos(png_bytes)


async def image_endpoint(file: UploadFile = File(...), remove_bg: bool = Form(True)):
    try:
        png_bytes = await process_uploaded_image(file, remove_bg)
        return Response(content=png_bytes, media_type="image/png")
    except ImageProcessError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception:
        return JSONResponse(status_code=500, content={"error": "图片处理失败，请稍后重试"})


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(HTML_PAGE)


@app.post("/api/preview")
async def preview(file: UploadFile = File(...), remove_bg: bool = Form(True)):
    return await image_endpoint(file, remove_bg)


@app.post("/api/process")
async def process(file: UploadFile = File(...), remove_bg: bool = Form(True)):
    return await image_endpoint(file, remove_bg)


HTML_PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>拼豆图纸生成器</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #1f2937;
      --muted: #64748b;
      --line: #d7dde8;
      --paper: #ffffff;
      --soft: #f5f7fb;
      --brand: #166f8f;
      --brand-dark: #0d526d;
      --accent: #e24a4a;
      --green: #237a57;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      color: var(--ink);
      background: linear-gradient(180deg, #f8fafc 0%, #eef3f7 100%);
    }
    header {
      padding: 22px clamp(16px, 4vw, 40px) 10px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,.86);
      backdrop-filter: blur(10px);
      position: sticky;
      top: 0;
      z-index: 5;
    }
    h1 { margin: 0; font-size: 24px; letter-spacing: 0; }
    main {
      width: min(1280px, 100%);
      margin: 0 auto;
      padding: 20px clamp(14px, 3vw, 28px) 36px;
      display: grid;
      grid-template-columns: 370px minmax(0, 1fr);
      gap: 18px;
    }
    aside, section {
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 10px 28px rgba(15, 23, 42, .06);
    }
    aside { padding: 16px; align-self: start; position: sticky; top: 88px; }
    section { padding: 16px; min-width: 0; }
    .field { margin-bottom: 16px; }
    .field > label, .label {
      display: block;
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 8px;
    }
    .drop {
      border: 1px dashed #93a7bd;
      background: #f8fbfd;
      min-height: 170px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      text-align: center;
      padding: 14px;
      cursor: pointer;
      transition: border-color .16s ease, background .16s ease;
    }
    .drop:hover, .drop.dragging { border-color: var(--brand); background: #eef8fb; }
    .drop strong { display: block; font-size: 16px; margin-bottom: 6px; }
    .drop span { color: var(--muted); font-size: 13px; }
    input[type="file"] { display: none; }
    .thumb {
      max-width: 100%;
      max-height: 210px;
      border-radius: 6px;
      display: none;
      margin-top: 10px;
      border: 1px solid var(--line);
      background: #fff;
    }
    .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    select, input[type="number"] {
      width: 100%;
      min-height: 42px;
      border: 1px solid #b7c3cf;
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      background: #fff;
    }
    .check {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 34px;
      font-size: 14px;
    }
    .dims { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }
    .hint {
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }
    button {
      border: 0;
      border-radius: 6px;
      min-height: 42px;
      padding: 0 14px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      background: var(--brand);
      color: #fff;
      transition: transform .12s ease, background .12s ease, opacity .12s ease;
    }
    button:hover { background: var(--brand-dark); }
    button:active { transform: translateY(1px); }
    button.secondary { background: #42526b; }
    button.secondary:hover { background: #2e3b4f; }
    button.success { background: var(--green); }
    button.success:hover { background: #1b6045; }
    button:disabled { opacity: .55; cursor: not-allowed; transform: none; }
    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .download-row { margin-top: 12px; display: flex; justify-content: flex-end; }
    .preview-grid {
      display: grid;
      grid-template-columns: minmax(220px, 340px) minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }
    .panel-title { margin: 0 0 10px; font-size: 16px; }
    .image-box {
      min-height: 260px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background:
        linear-gradient(45deg, #f0f3f6 25%, transparent 25%),
        linear-gradient(-45deg, #f0f3f6 25%, transparent 25%),
        linear-gradient(45deg, transparent 75%, #f0f3f6 75%),
        linear-gradient(-45deg, transparent 75%, #f0f3f6 75%);
      background-size: 22px 22px;
      background-position: 0 0, 0 11px, 11px -11px, -11px 0;
      display: grid;
      place-items: center;
      overflow: hidden;
    }
    #processedPreview {
      max-width: 100%;
      max-height: 360px;
      display: none;
    }
    .canvas-wrap {
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: auto;
      background: #f8fafc;
      min-height: 360px;
      display: grid;
      place-items: start center;
      padding: 12px;
    }
    canvas {
      max-width: 100%;
      height: auto;
      background: #fff;
      box-shadow: 0 8px 24px rgba(15,23,42,.08);
    }
    .status {
      color: var(--muted);
      font-size: 13px;
      min-height: 20px;
      margin-top: 10px;
    }
    @media (max-width: 920px) {
      main { grid-template-columns: 1fr; }
      aside { position: static; }
      .preview-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 520px) {
      .actions, .dims { grid-template-columns: 1fr; }
      header { position: static; }
      h1 { font-size: 21px; }
    }
  </style>
</head>
<body>
  <header><h1>拼豆图纸生成器</h1></header>
  <main>
    <aside>
      <div class="field">
        <span class="label">上传图片</span>
        <label class="drop" id="dropZone" for="fileInput">
          <span>
            <strong>点击或拖拽图片到这里</strong>
            <span>支持 PNG、JPG、WebP 等常见格式</span>
          </span>
        </label>
        <input id="fileInput" type="file" accept="image/*" />
        <img id="thumb" class="thumb" alt="原图缩略图" />
      </div>

      <div class="field">
        <label class="check"><input id="removeBg" type="checkbox"/> 去除背景（抠图）</label>
      </div>

      <div class="field">
        <label for="presetSelect">图纸尺寸</label>
        <select id="presetSelect"></select>
        <div id="presetDesc" class="hint"></div>
        <label class="check" style="margin-top:10px;"><input id="customSize" type="checkbox" /> 自定义尺寸</label>
        <div id="customBox" class="dims" style="display:none;">
          <label>宽（格）<input id="customWidth" type="number" value="" /></label>
          <label>高（格）<input id="customHeight" type="number" value="" /></label>
        </div>
        <div id="cmInfo" class="hint"></div>
      </div>

      <div class="actions">
        <button id="previewBtn" type="button">预览抠图效果</button>
        <button id="generateBtn" class="success" type="button">生成图纸</button>
      </div>
      <div id="status" class="status"></div>
    </aside>

    <section>
      <div class="preview-grid">
        <div>
          <div class="panel-title" style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
            <span>预览抠图效果</span>
            <button id="downloadProcessedBtn" class="secondary" type="button" disabled style="font-size:13px;min-height:32px;padding:0 10px;">下载抠图后图片</button>
          </div>
          <div class="image-box">
            <img id="processedPreview" alt="处理后图片预览" />
            <span id="emptyPreview" class="hint">处理后的图片会显示在这里</span>
          </div>
        </div>
        <div>
          <h2 class="panel-title">拼豆图纸</h2>
          <div class="canvas-wrap"><canvas id="patternCanvas" width="900" height="520"></canvas></div>
          <div id="patternStatus" class="status" style="text-align:right;margin-top:8px;color:var(--muted);min-height:18px;"></div>
          <div class="download-row">
            <button id="downloadBtn" class="secondary" type="button" disabled>下载 PNG</button>
          </div>
        </div>
      </div>
    </section>
  </main>

  <script>
    const RAW_PALETTE = [
      {id:"A1",hex:"#FAF4C8"},{id:"A2",hex:"#FFFFD5"},{id:"A3",hex:"#FEFF8B"},{id:"A4",hex:"#FBED56"},{id:"A5",hex:"#F4D738"},{id:"A6",hex:"#FEAC4C"},{id:"A7",hex:"#FE8B4C"},{id:"A8",hex:"#FFDA45"},{id:"A9",hex:"#FF995B"},{id:"A10",hex:"#F77C31"},{id:"A11",hex:"#FFDD99"},{id:"A12",hex:"#FE9F72"},{id:"A13",hex:"#FFC365"},{id:"A14",hex:"#FD543D"},{id:"A15",hex:"#FFF365"},{id:"A16",hex:"#FFFF9F"},{id:"A17",hex:"#FFE36E"},{id:"A18",hex:"#FEBE7D"},{id:"A19",hex:"#FD7C72"},{id:"A20",hex:"#FFD568"},{id:"A21",hex:"#FFE395"},{id:"A22",hex:"#F4F57D"},{id:"A23",hex:"#E6C9B7"},{id:"A24",hex:"#F7F8A2"},{id:"A25",hex:"#FFD67D"},{id:"A26",hex:"#FFC830"},
      {id:"B1",hex:"#E6EE31"},{id:"B2",hex:"#63F347"},{id:"B3",hex:"#9EF780"},{id:"B4",hex:"#5DE035"},{id:"B5",hex:"#35E352"},{id:"B6",hex:"#65E2A6"},{id:"B7",hex:"#3DAF80"},{id:"B8",hex:"#1C9C4F"},{id:"B9",hex:"#27523A"},{id:"B10",hex:"#95D3C2"},{id:"B11",hex:"#5D722A"},{id:"B12",hex:"#166F41"},{id:"B13",hex:"#CAEB7B"},{id:"B14",hex:"#ADE946"},{id:"B15",hex:"#2E5132"},{id:"B16",hex:"#C5ED9C"},{id:"B17",hex:"#9BB13A"},{id:"B18",hex:"#E6EE49"},{id:"B19",hex:"#24B88C"},{id:"B20",hex:"#C2F0CC"},{id:"B21",hex:"#156A6B"},{id:"B22",hex:"#0B3C43"},{id:"B23",hex:"#303A21"},{id:"B24",hex:"#EEFCA5"},{id:"B25",hex:"#4E846D"},{id:"B26",hex:"#8D7A35"},{id:"B27",hex:"#CCE1AF"},{id:"B28",hex:"#9EE5B9"},{id:"B29",hex:"#C5E254"},{id:"B30",hex:"#E2FCB1"},{id:"B31",hex:"#B0E792"},{id:"B32",hex:"#9CAB5A"},
      {id:"C1",hex:"#E8FFE7"},{id:"C2",hex:"#A9F9FC"},{id:"C3",hex:"#A0E2FB"},{id:"C4",hex:"#41CCFF"},{id:"C5",hex:"#01ACEB"},{id:"C6",hex:"#50AAF0"},{id:"C7",hex:"#3677D2"},{id:"C8",hex:"#0F54C0"},{id:"C9",hex:"#324BCA"},{id:"C10",hex:"#3EBCE2"},{id:"C11",hex:"#28DDDE"},{id:"C12",hex:"#1C334D"},{id:"C13",hex:"#CDE8FF"},{id:"C14",hex:"#D5FDFF"},{id:"C15",hex:"#22C4C6"},{id:"C16",hex:"#1557A8"},{id:"C17",hex:"#04D1F6"},{id:"C18",hex:"#1D3344"},{id:"C19",hex:"#1887A2"},{id:"C20",hex:"#176DAF"},{id:"C21",hex:"#BEDDFF"},{id:"C22",hex:"#67B4BE"},{id:"C23",hex:"#C8E2FF"},{id:"C24",hex:"#7CC4FF"},{id:"C25",hex:"#A9E5E5"},{id:"C26",hex:"#3CAED8"},{id:"C27",hex:"#D3DFFA"},{id:"C28",hex:"#BBCFED"},{id:"C29",hex:"#34488E"},
      {id:"D1",hex:"#AEB4F2"},{id:"D2",hex:"#858EDD"},{id:"D3",hex:"#2F54AF"},{id:"D4",hex:"#182A84"},{id:"D5",hex:"#B843C5"},{id:"D6",hex:"#AC7BDE"},{id:"D7",hex:"#8854B3"},{id:"D8",hex:"#E2D3FF"},{id:"D9",hex:"#D5B9F8"},{id:"D10",hex:"#361851"},{id:"D11",hex:"#B9BAE1"},{id:"D12",hex:"#DE9AD4"},{id:"D13",hex:"#B90095"},{id:"D14",hex:"#8B279B"},{id:"D15",hex:"#2F1F90"},{id:"D16",hex:"#E3E1EE"},{id:"D17",hex:"#C4D4F6"},{id:"D18",hex:"#A45EC7"},{id:"D19",hex:"#D8C3D7"},{id:"D20",hex:"#9C32B2"},{id:"D21",hex:"#9A009B"},{id:"D22",hex:"#333A95"},{id:"D23",hex:"#EBDAFC"},{id:"D24",hex:"#7786E5"},{id:"D25",hex:"#494FC7"},{id:"D26",hex:"#DFC2F8"},
      {id:"E1",hex:"#FDD3CC"},{id:"E2",hex:"#FEC0DF"},{id:"E3",hex:"#FFB7E7"},{id:"E4",hex:"#E8649E"},{id:"E5",hex:"#F551A2"},{id:"E6",hex:"#F13D74"},{id:"E7",hex:"#C63478"},{id:"E8",hex:"#FFDBE9"},{id:"E9",hex:"#E970CC"},{id:"E10",hex:"#D33793"},{id:"E11",hex:"#FCDDD2"},{id:"E12",hex:"#F78FC3"},{id:"E13",hex:"#B5006D"},{id:"E14",hex:"#FFD1BA"},{id:"E15",hex:"#F8C7C9"},{id:"E16",hex:"#FFF3EB"},{id:"E17",hex:"#FFE2EA"},{id:"E18",hex:"#FFC7DB"},{id:"E19",hex:"#FEBAD5"},{id:"E20",hex:"#D8C7D1"},{id:"E21",hex:"#BD9DA1"},{id:"E22",hex:"#B785A1"},{id:"E23",hex:"#937A8D"},{id:"E24",hex:"#E1BCE8"},
      {id:"F1",hex:"#FD957B"},{id:"F2",hex:"#FC3D46"},{id:"F3",hex:"#F74941"},{id:"F4",hex:"#FC283C"},{id:"F5",hex:"#E7002F"},{id:"F6",hex:"#943630"},{id:"F7",hex:"#971937"},{id:"F8",hex:"#BC0028"},{id:"F9",hex:"#E2677A"},{id:"F10",hex:"#8A4526"},{id:"F11",hex:"#5A2121"},{id:"F12",hex:"#FD4E6A"},{id:"F13",hex:"#F35744"},{id:"F14",hex:"#FFA9AD"},{id:"F15",hex:"#D30022"},{id:"F16",hex:"#FEC2A6"},{id:"F17",hex:"#E69C79"},{id:"F18",hex:"#D37C46"},{id:"F19",hex:"#C1444A"},{id:"F20",hex:"#CD9391"},{id:"F21",hex:"#F7B4C6"},{id:"F22",hex:"#FDC0D0"},{id:"F23",hex:"#F67E66"},{id:"F24",hex:"#E698AA"},{id:"F25",hex:"#E54B4F"},
      {id:"G1",hex:"#FFE2CE"},{id:"G2",hex:"#FFC4AA"},{id:"G3",hex:"#F4C3A5"},{id:"G4",hex:"#E1B383"},{id:"G5",hex:"#EDB045"},{id:"G6",hex:"#E99C17"},{id:"G7",hex:"#9D5B3E"},{id:"G8",hex:"#753832"},{id:"G9",hex:"#E6B483"},{id:"G10",hex:"#D98C39"},{id:"G11",hex:"#E0C593"},{id:"G12",hex:"#FFC890"},{id:"G13",hex:"#B7714A"},{id:"G14",hex:"#8D614C"},{id:"G15",hex:"#FCF9E0"},{id:"G16",hex:"#F2D9BA"},{id:"G17",hex:"#78524B"},{id:"G18",hex:"#FFE4CC"},{id:"G19",hex:"#E07935"},{id:"G20",hex:"#A94023"},{id:"G21",hex:"#B88558"},
      {id:"H1",hex:"#FDFBFF"},{id:"H2",hex:"#FEFFFF"},{id:"H3",hex:"#B6B1BA"},{id:"H4",hex:"#89858C"},{id:"H5",hex:"#48464E"},{id:"H6",hex:"#2F2B2F"},{id:"H7",hex:"#000000"},{id:"H8",hex:"#E7D6DB"},{id:"H9",hex:"#EDEDED"},{id:"H10",hex:"#EEE9EA"},{id:"H11",hex:"#CECDD5"},{id:"H12",hex:"#FFF5ED"},{id:"H13",hex:"#F5ECD2"},{id:"H14",hex:"#CFD7D3"},{id:"H15",hex:"#98A6A8"},{id:"H16",hex:"#1D1414"},{id:"H17",hex:"#F1EDED"},{id:"H18",hex:"#FFFDF0"},{id:"H19",hex:"#F6EFE2"},{id:"H20",hex:"#949FA3"},{id:"H21",hex:"#FFFBE1"},{id:"H22",hex:"#CACAD4"},{id:"H23",hex:"#9A9D94"},
      {id:"M1",hex:"#BCC6B8"},{id:"M2",hex:"#8AA386"},{id:"M3",hex:"#697D80"},{id:"M4",hex:"#E3D2BC"},{id:"M5",hex:"#D0CCAA"},{id:"M6",hex:"#B0A782"},{id:"M7",hex:"#B4A497"},{id:"M8",hex:"#B38281"},{id:"M9",hex:"#A58767"},{id:"M10",hex:"#C5B2BC"},{id:"M11",hex:"#9F7594"},{id:"M12",hex:"#644749"},{id:"M13",hex:"#D19066"},{id:"M14",hex:"#C77362"},{id:"M15",hex:"#757D78"},
    ];

    const PRESETS = [
      {name:"标准小板", width:52, height:52, desc:"宽高约14cm，适合钥匙扣、冰箱贴等，新手入门首选"},
      {name:"中板", width:78, height:78, desc:"宽高约21cm，适合完整人物、杯垫、摆台等"},
      {name:"超大板", width:104, height:104, desc:"宽高约28cm，适合装饰挂画、鼠标垫等"},
      {name:"长方形长条板", width:52, height:104, desc:"宽14cm高28cm，适合文字横幅、长条立牌、场景横幅等"},
      {name:"迷你小方板", width:30, height:30, desc:"宽高约8cm，巴掌大小，迷你挂件、配件等"},
      {name:"迷你小小方板", width:20, height:20, desc:"宽高约5cm，适合耳环等"}
    ];

    const palette = RAW_PALETTE.map(item => {
      let value = String(item.hex || "").replace(/[^0-9a-fA-F]/g, "");
      if (value.length === 1) value = value.repeat(6);
      if (value.length < 6) value = value.padEnd(6, "0");
      value = value.slice(0, 6).toUpperCase();
      const r = parseInt(value.slice(0, 2), 16);
      const g = parseInt(value.slice(2, 4), 16);
      const b = parseInt(value.slice(4, 6), 16);
      return {...item, hex: `#${value}`, r, g, b};
    });

    const fileInput = document.getElementById("fileInput");
    const dropZone = document.getElementById("dropZone");
    const thumb = document.getElementById("thumb");
    const removeBg = document.getElementById("removeBg");
    const previewBtn = document.getElementById("previewBtn");
    const generateBtn = document.getElementById("generateBtn");
    const downloadBtn = document.getElementById("downloadBtn");
    const downloadProcessedBtn = document.getElementById("downloadProcessedBtn");
    const presetSelect = document.getElementById("presetSelect");
    const presetDesc = document.getElementById("presetDesc");
    const customSize = document.getElementById("customSize");
    const customBox = document.getElementById("customBox");
    const customWidth = document.getElementById("customWidth");
    const customHeight = document.getElementById("customHeight");
    const cmInfo = document.getElementById("cmInfo");
    const statusEl = document.getElementById("status");
    const processedPreview = document.getElementById("processedPreview");
    const emptyPreview = document.getElementById("emptyPreview");
    const patternStatus = document.getElementById("patternStatus");
    const canvas = document.getElementById("patternCanvas");
    const ctx = canvas.getContext("2d");

    let selectedFile = null;
    let processedBlobUrl = "";

    function init() {
      PRESETS.forEach((preset, index) => {
        const option = document.createElement("option");
        option.value = index;
        option.textContent = `${preset.name}：${preset.width}×${preset.height}格`;
        presetSelect.appendChild(option);
      });
      drawEmptyCanvas();
      updateSizeUI();
    }

    function setStatus(text) { statusEl.textContent = text || ""; }

    function getSize() {
      if (customSize.checked) {
        return {
          width: parseCustomValue(customWidth.value, 5, 200),
          height: parseCustomValue(customHeight.value, 5, 200)
        };
      }
      const preset = PRESETS[Number(presetSelect.value) || 0];
      return {width: preset.width, height: preset.height};
    }

    function parseCustomValue(value, min, max) {
      if (!value || value.trim() === "") return min;
      const num = Number.parseInt(value, 10);
      if (Number.isNaN(num)) return min;
      return Math.max(min, Math.min(max, num));
    }

    function validateCustomSize() {
      const width = parseCustomValue(customWidth.value, 5, 200);
      const height = parseCustomValue(customHeight.value, 5, 200);
      customWidth.value = customWidth.value.trim() === "" ? "" : width;
      customHeight.value = customHeight.value.trim() === "" ? "" : height;
      updateCmInfo();
    }

    function updateCmInfo() {
      const size = getSize();
      cmInfo.textContent = `一格0.26cm，当前约${(size.width * 0.26).toFixed(1)}cm×${(size.height * 0.26).toFixed(1)}cm`;
    }

    function updateSizeUI() {
      const preset = PRESETS[Number(presetSelect.value) || 0];
      presetDesc.textContent = preset.desc;
      customBox.style.display = customSize.checked ? "grid" : "none";
      if (!customSize.checked) {
        const size = getSize();
        customWidth.value = "";
        customHeight.value = "";
      }
      updateCmInfo();
    }

    function setFile(file) {
      if (!file || !file.type.startsWith("image/")) {
        alert("请上传图片文件");
        return;
      }
      selectedFile = file;
      thumb.src = URL.createObjectURL(file);
      thumb.style.display = "block";
      downloadBtn.disabled = true;
      setStatus("已选择图片");
    }

    async function callImageApi(path) {
      if (!selectedFile) {
        alert("请先上传一张图片");
        throw new Error("no file");
      }
      const form = new FormData();
      form.append("file", selectedFile);
      form.append("remove_bg", removeBg.checked ? "true" : "false");

      const response = await fetch(path, {method: "POST", body: form});
      const contentType = response.headers.get("content-type") || "";
      if (!response.ok) {
        if (contentType.includes("application/json")) {
          const data = await response.json();
          throw new Error(data.error || "图片处理失败，请稍后重试");
        }
        throw new Error("图片处理失败，请稍后重试");
      }
      return await response.blob();
    }

    async function previewImage() {
      try {
        setWorking(true, "正在生成预览...");
        const blob = await callImageApi("/api/preview");
        if (processedBlobUrl) URL.revokeObjectURL(processedBlobUrl);
        processedBlobUrl = URL.createObjectURL(blob);
        processedPreview.src = processedBlobUrl;
        processedPreview.style.display = "block";
        emptyPreview.style.display = "none";
        downloadProcessedBtn.disabled = false;
        setStatus("预览已生成");
        alert("抠图已完成，可在预览区域查看效果并下载。");
      } catch (err) {
        if (err.message !== "no file") alert(err.message || "网络请求失败，请稍后重试");
        downloadProcessedBtn.disabled = true;
        setStatus("");
      } finally {
        setWorking(false);
      }
    }

    function downloadProcessedImage() {
      if (!processedBlobUrl) return;
      const link = document.createElement("a");
      link.download = "抠图后图片.png";
      link.href = processedBlobUrl;
      link.click();
    }

    async function generatePattern() {
      try {
        setWorking(true, "正在生成图纸...");
        if (patternStatus) patternStatus.textContent = "图纸生成中...，请稍等";
        const blob = await callImageApi("/api/process");
        const bitmap = await createImageBitmap(blob);
        drawPattern(bitmap, getSize());
        downloadBtn.disabled = false;
        if (patternStatus) patternStatus.textContent = "";
        setStatus("图纸已生成，可下载 PNG");
      } catch (err) {
        if (err.message !== "no file") alert(err.message || "生成图纸失败，请稍后重试");
        if (patternStatus) patternStatus.textContent = "";
        setStatus("");
      } finally {
        setWorking(false);
      }
    }

    function setWorking(working, text) {
      previewBtn.disabled = working;
      generateBtn.disabled = working;
      if (text) setStatus(text);
    }

    function nearestPaletteColor(r, g, b) {
      let best = palette[0];
      let bestDistance = Infinity;
      for (const color of palette) {
        const dr = r - color.r;
        const dg = g - color.g;
        const db = b - color.b;
        const distance = dr * dr + dg * dg + db * db;
        if (distance < bestDistance) {
          bestDistance = distance;
          best = color;
        }
      }
      return best;
    }

    function buildPixelMatrix(image, width, height) {
      const offscreen = document.createElement("canvas");
      offscreen.width = width;
      offscreen.height = height;
      const octx = offscreen.getContext("2d", {willReadFrequently: true});
      octx.fillStyle = "#ffffff";
      octx.fillRect(0, 0, width, height);
      const scale = Math.min(width / image.width, height / image.height);
      const drawW = Math.max(1, Math.round(image.width * scale));
      const drawH = Math.max(1, Math.round(image.height * scale));
      const dx = Math.floor((width - drawW) / 2);
      const dy = Math.floor((height - drawH) / 2);
      octx.imageSmoothingEnabled = true;
      octx.drawImage(image, dx, dy, drawW, drawH);

      const data = octx.getImageData(0, 0, width, height).data;
      const matrix = [];
      const used = new Map();
      for (let y = 0; y < height; y++) {
        const row = [];
        for (let x = 0; x < width; x++) {
          const index = (y * width + x) * 4;
          const alpha = data[index + 3];
          const color = alpha < 16 ? palette.find(c => c.id === "T1") : nearestPaletteColor(data[index], data[index + 1], data[index + 2]);
          row.push(color);
          used.set(color.id, color);
        }
        matrix.push(row);
      }
      return {matrix, used: Array.from(used.values()).sort((a, b) => a.id.localeCompare(b.id, "zh-CN", {numeric: true}))};
    }

    function drawPattern(image, size) {
      const {width, height} = size;
      const {matrix, used} = buildPixelMatrix(image, width, height);
      const cell = width > 90 || height > 90 ? 20 : 24;
      const ruler = 54;
      const legendTopGap = 28;
      const legendItemW = 96;
      const legendItemH = 28;
      const legendCols = Math.max(1, Math.floor((width * cell) / legendItemW));
      const legendRows = Math.ceil(used.length / legendCols);
      const bottomPadding = 20;

      canvas.width = ruler + width * cell + 28;
      canvas.height = ruler + height * cell + legendTopGap + legendRows * legendItemH + bottomPadding;

      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.textBaseline = "top";
      ctx.lineWidth = 1;

      for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
          const color = matrix[y][x];
          const px = ruler + x * cell;
          const py = ruler + y * cell;
          ctx.fillStyle = color.hex;
          ctx.fillRect(px, py, cell, cell);
          if (cell >= 18) {
            ctx.fillStyle = contrastColor(color);
            ctx.font = `${Math.max(7, Math.floor(cell * 0.34))}px Arial`;
            ctx.fillText(color.id, px + 2, py + 2);
          }
        }
      }

      drawGrid(ruler, ruler, width, height, cell);
      drawRulers(ruler, width, height, cell);
      drawLegend(used, ruler, ruler + height * cell + legendTopGap, legendCols, legendItemW, legendItemH);
    }

    function contrastColor(color) {
      const brightness = color.r * 0.299 + color.g * 0.587 + color.b * 0.114;
      return brightness > 155 ? "#111827" : "#ffffff";
    }

    function drawGrid(startX, startY, width, height, cell) {
      for (let x = 0; x <= width; x++) {
        ctx.beginPath();
        ctx.strokeStyle = x % 5 === 0 ? "#d61f1f" : "#ffffff";
        ctx.moveTo(startX + x * cell + .5, startY);
        ctx.lineTo(startX + x * cell + .5, startY + height * cell);
        ctx.stroke();
      }
      for (let y = 0; y <= height; y++) {
        ctx.beginPath();
        ctx.strokeStyle = y % 5 === 0 ? "#d61f1f" : "#ffffff";
        ctx.moveTo(startX, startY + y * cell + .5);
        ctx.lineTo(startX + width * cell, startY + y * cell + .5);
        ctx.stroke();
      }
    }

    function drawRulers(ruler, width, height, cell) {
      ctx.fillStyle = "#f3f6fa";
      ctx.fillRect(ruler, 0, width * cell, ruler);
      ctx.fillRect(0, ruler, ruler, height * cell);
      ctx.strokeStyle = "#94a3b8";
      ctx.strokeRect(ruler, ruler, width * cell, height * cell);
      ctx.fillStyle = "#1f2937";
      ctx.textAlign = "center";
      ctx.font = "10px Arial";

      for (let x = 1; x <= width; x++) {
        const px = ruler + (x - .5) * cell;
        const long = x % 5 === 0;
        ctx.strokeStyle = long ? "#d61f1f" : "#64748b";
        ctx.beginPath();
        ctx.moveTo(px, ruler - (long ? 18 : 10));
        ctx.lineTo(px, ruler);
        ctx.stroke();
        if (long || width <= 30) {
          ctx.font = long ? "bold 11px Arial" : "9px Arial";
          ctx.fillText(String(x), px, ruler - (long ? 34 : 25));
        }
      }

      ctx.textAlign = "right";
      for (let y = 1; y <= height; y++) {
        const py = ruler + (y - .5) * cell;
        const long = y % 5 === 0;
        ctx.strokeStyle = long ? "#d61f1f" : "#64748b";
        ctx.beginPath();
        ctx.moveTo(ruler - (long ? 18 : 10), py);
        ctx.lineTo(ruler, py);
        ctx.stroke();
        if (long || height <= 30) {
          ctx.font = long ? "bold 11px Arial" : "9px Arial";
          ctx.fillText(String(y), ruler - (long ? 22 : 14), py - 6);
        }
      }
      ctx.textAlign = "left";
    }

    function drawLegend(used, x, y, cols, itemW, itemH) {
      ctx.fillStyle = "#1f2937";
      ctx.font = "bold 14px Arial";
      ctx.fillText("图例", x, y - 20);
      used.forEach((color, index) => {
        const col = index % cols;
        const row = Math.floor(index / cols);
        const px = x + col * itemW;
        const py = y + row * itemH;
        ctx.fillStyle = color.hex;
        ctx.fillRect(px, py, 20, 20);
        ctx.strokeStyle = "#cbd5e1";
        ctx.strokeRect(px, py, 20, 20);
        ctx.fillStyle = "#111827";
        ctx.font = "12px Arial";
        ctx.fillText(color.id, px + 26, py + 3);
      });
    }

    function drawEmptyCanvas() {
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#64748b";
      ctx.font = "16px Microsoft YaHei, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("上传图片后生成拼豆图纸", canvas.width / 2, canvas.height / 2);
      ctx.textAlign = "left";
    }

    dropZone.addEventListener("dragover", event => {
      event.preventDefault();
      dropZone.classList.add("dragging");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragging"));
    dropZone.addEventListener("drop", event => {
      event.preventDefault();
      dropZone.classList.remove("dragging");
      setFile(event.dataTransfer.files[0]);
    });
    fileInput.addEventListener("change", () => setFile(fileInput.files[0]));
    presetSelect.addEventListener("change", updateSizeUI);
    customSize.addEventListener("change", updateSizeUI);
    customWidth.addEventListener("input", updateCmInfo);
    customWidth.addEventListener("blur", validateCustomSize);
    customHeight.addEventListener("input", updateCmInfo);
    customHeight.addEventListener("blur", validateCustomSize);
    previewBtn.addEventListener("click", previewImage);
    generateBtn.addEventListener("click", generatePattern);
    downloadBtn.addEventListener("click", () => {
      const link = document.createElement("a");
      link.download = "拼豆图纸.png";
      link.href = canvas.toDataURL("image/png");
      link.click();
    });
    downloadProcessedBtn.addEventListener("click", downloadProcessedImage);

    init();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

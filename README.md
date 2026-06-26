# PuzzleDrawCodex 拼豆图纸生成器

一个基于 FastAPI 的拼豆图纸生成器 Web 应用。后端提供图片上传、可选腾讯云 COS 数据万象人像抠图接口，前端在浏览器中使用 Canvas 生成拼豆图纸，并支持下载 PNG。

## 功能简介

- 上传图片并显示原图缩略图
- 可选择是否启用“去除背景（抠图）”，默认开启
- 调用 `/api/preview` 预览抠图后的图片
- 调用 `/api/process` 获取处理图，并在浏览器生成拼豆图纸
- 支持 52×52、78×78、104×104、52×104、30×30、20×20 等预设尺寸
- 支持自定义宽高，并按一格 0.26cm 实时换算尺寸
- 图纸包含等比居中图像、调色板映射、网格、每 5 格红色分隔线、刻度尺、格内色号、底部图例
- 可下载包含完整图纸和图例的 PNG
- 网络和接口错误会以中文弹窗提示

## 本地运行

1. 进入项目目录：

   ```bash
   cd PuzzleDrawCodex
   ```

2. 创建并激活虚拟环境（可选）：

   Windows PowerShell:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   macOS / Linux:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. 安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

4. 设置腾讯云环境变量。如果不需要抠图，也可以在页面取消勾选“去除背景（抠图）”。

   Windows CMD:

   ```cmd
   set TENCENT_SECRET_ID=你的SecretId
   set TENCENT_SECRET_KEY=你的SecretKey
   ```

   Windows PowerShell:

   ```powershell
   $env:TENCENT_SECRET_ID="你的SecretId"
   $env:TENCENT_SECRET_KEY="你的SecretKey"
   ```

   macOS / Linux:

   ```bash
   export TENCENT_SECRET_ID="你的SecretId"
   export TENCENT_SECRET_KEY="你的SecretKey"
   ```

5. 启动应用：

   ```bash
   python app.py
   ```

6. 打开浏览器访问：

   ```text
   http://127.0.0.1:8000
   ```

## 腾讯云配置

应用使用以下 COS 数据万象配置：

- Bucket: `my-image-bucket-1446249102`
- Region: `ap-guangzhou`
- 处理参数: `ci-process=face-effect&type=face-segmentation`
- 签名有效期: 300 秒

云端部署时，请在平台的环境变量配置中添加 `TENCENT_SECRET_ID` 和 `TENCENT_SECRET_KEY`。不要把真实密钥提交到 GitHub。

## 在线演示链接

当前未配置在线演示。部署到云平台后，可将访问地址补充到这里：

```text
https://你的在线演示地址
```

## 推送到 GitHub 仓库操作指南

目标仓库：

```text
https://github.com/18708386823/PuzzleDrawCodex.git
```

如果你还没有克隆仓库，先执行：

```bash
git clone https://github.com/18708386823/PuzzleDrawCodex.git
cd PuzzleDrawCodex
```

如果你已经有本地仓库，进入仓库目录并确认远程地址：

```bash
cd PuzzleDrawCodex
git remote -v
git remote set-url origin https://github.com/18708386823/PuzzleDrawCodex.git
```

将 `app.py`、`README.md`、`requirements.txt` 复制到仓库根目录后，可选择创建虚拟环境：

```bash
python -m venv .venv
```

激活虚拟环境后安装依赖：

```bash
pip install -r requirements.txt
```

确认改动并提交：

```bash
git status
git add app.py README.md requirements.txt
git commit -m "Add puzzle pattern generator"
git push origin main
```

如果远程仓库默认分支是 `master`，将最后一条命令改为：

```bash
git push origin master
```

如果仓库非空且推送时提示冲突，先拉取远程内容并处理冲突：

```bash
git pull --rebase origin main
```

处理冲突后继续：

```bash
git add app.py README.md requirements.txt
git rebase --continue
git push origin main
```

如果你不确定默认分支名称，可以执行：

```bash
git branch
```

看到当前分支后，将推送命令里的 `main` 替换为实际分支名。

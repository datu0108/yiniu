# Image Editor API Skill（Accio Work）

PackyAPI + **gemini-2.5-flash-image**：修图、批量修图、文生图。

## 部署路径

复制整个文件夹到 Accio Agent 的 skills 目录：

```text
${agent_core}/skills/image_editor_api/
```

本仓库打包副本路径：

```text
/Users/datu/PyCharmMiscProject/yiniu/export/image_editor_api/
```

## 目录说明

| 文件 | 作用 |
|------|------|
| `SKILL.md` | Agent 指令（必读） |
| `bridge.py` | 唯一执行入口 |
| `config.json` | API 端点与模型 |
| `.env` | API 密钥（勿提交 git、勿外传） |
| `config.example.json` | 配置示例 |
| `.jobs/` | 后台任务状态（自动创建） |

## 环境

- Python 3.9+
- 可选：`pip install Pillow`（上传前压缩大图）

## 子命令速查

```bash
# 修图（单张）
python bridge.py launch --input <图> --prompt "<指令>" --output <目录>

# 批量修图（文件夹）
python bridge.py batch-launch --input-dir <文件夹> --prompt "<指令>" --output <目录>

# 文生图（无输入图）
python bridge.py generate-launch --prompt "<描述>" --output <目录> --name out.png

# 轮询
python bridge.py status --job <job_id>
python bridge.py batch-status --batch <batch_id>
```

## 配置

默认模型：`gemini-2.5-flash-image`  
默认端点：`https://www.packyapi.com/v1beta/models/gemini-2.5-flash-image:generateContent`

密钥在 `.env` 的 `LLM_API_KEY`，或通过环境变量覆盖。

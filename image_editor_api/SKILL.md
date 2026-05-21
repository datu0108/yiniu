---
name: image_editor_api
description: "通过 PackyAPI 调用图像模型（推荐 gemini-2.5-flash-image）。支持：修图（改图/换风格）、文生图（按描述生成新图）、批量修图（去除水印、文字、人物，修复背景，商品图净化）。支持单张 launch、批量 batch-launch 以及状态查询。使用时必须通过 launch/status 异步模式以避免超时。"
---

# Skill: ImageEditor

## Description

通过 PackyAPI 调用图像模型（推荐 **`gemini-2.5-flash-image`**）。支持：

* **修图**：有输入图 → 改图 / 换风格（`launch` / `batch-launch`）
* **文生图**：无输入图 → 按描述生成新图（`generate-launch`）

**Trigger**：用户提供修图说明 + 图片路径/目录；或仅提供生成描述（文生图）时使用。

**重要（Accio Work）**：单张常需数十秒～数分钟。若用同步 `run`，bash 可能在 60～120s 断开。  
**必须使用 `launch` / `batch-launch` + `status` / `batch-status` 轮询**，不要长时间阻塞一条命令。

## Prerequisites

* Python 3.9+（可选 Pillow 用于上传前压缩）。
* API 密钥：`LLM_API_KEY`（勿写入 git）。
* 脚本：`${agent_core}/skills/image_editor_api/bridge.py`
* 默认已写在 `config.json` + 项目根 `.env`（`LLM_API_KEY`，勿提交 git）。
* 模型：**`gemini-2.5-flash-image`**，端点：`/v1beta/models/gemini-2.5-flash-image:generateContent`
* Skill 目录下另有 `skills/image_editor_api/.env`（Accio 单独部署时用）。
* `api_timeout_seconds` 默认 600。

## Parameters

### 单张

| 参数 | 说明 | 必填 |
|------|------|------|
| `image_path` | 输入图片本地路径 | 是 |
| `prompt` | 修图指令 | 是 |
| `output_dir` | 输出目录（生成 `{原名}_edited.png`） | 是 |

### 文生图（生成）

| 参数 | 说明 | 必填 |
|------|------|------|
| `prompt` | 画面描述（商品、风格、背景等） | 是 |
| `output_dir` | 输出目录 | 是 |
| `name` | 输出文件名（可选，默认 `generated_<id>.png`） | 否 |

**要求**：`protocol=gemini`，`api_path` 含 `generateContent`（如 `gemini-2.5-flash-image`）。每次 `generate-launch` = **1 次** API 扣费。

### 批量修图

| 参数 | 说明 | 必填 |
|------|------|------|
| `input_dir` | 含多张图片的文件夹 | 是 |
| `prompt` | 应用于每一张的修图指令 | 是 |
| `output_dir` | 输出目录 | 是 |
| `recursive` | 是否包含子目录（默认否） | 否 |
| `delay` | 每张启动间隔秒数（默认 1） | 否 |

**批量规则**：

* 处理扩展名：`.png` `.jpg` `.jpeg` `.gif` `.webp` `.bmp`
* **跳过** 以 `_edited` 结尾的文件（避免重复处理输出图）
* **每张图 = 1 次 API 调用 = 1 次扣费**

## Workflow — 单张

1. **验证**：`image_path` 存在；`prompt` 非空。
2. **发起后台任务**：

```bash
python "${agent_core}/skills/image_editor_api/bridge.py" launch \
  --input "<image_path>" \
  --prompt "<prompt>" \
  --output "<output_dir>"
```

记录返回的 `job_id`。

3. **轮询**（每 20～30 秒，最多约 10 分钟）：

```bash
python "${agent_core}/skills/image_editor_api/bridge.py" status --job <job_id>
```

* `running` → 继续等，**不要**再次 `launch`
* `done` → 使用 `output_path`
* `error` → 展示 `error`，同一图最多再 `launch` **1 次**

## Workflow — 文生图（生成）

1. **验证**：`prompt` 非空；配置为 Gemini 文生图端点。
2. **发起任务**：

```bash
python "${agent_core}/skills/image_editor_api/bridge.py" generate-launch \
  --prompt "<画面描述>" \
  --output "<output_dir>" \
  --name "keyboard_product.png"
```

记录 `job_id`。

3. **轮询**（与修图相同）：

```bash
python "${agent_core}/skills/image_editor_api/bridge.py" status --job <job_id>
```

4. **交付**：`status` 为 `done` 时使用 `output_path`。

**禁止**：重复 `generate-launch` 同一需求（重复扣费）。

## Workflow — 批量修图（文件夹）

1. **验证**：`input_dir` 为目录且内含至少一张可处理图片；`prompt` 非空。
2. **发起批量任务**（一次命令，为每张图各起一个后台 job）：

```bash
python "${agent_core}/skills/image_editor_api/bridge.py" batch-launch \
  --input-dir "<input_dir>" \
  --prompt "<prompt>" \
  --output "<output_dir>"
```

记录 `batch_id` 与 `jobs` 列表。可选 `--recursive` 处理子目录；可选 `--delay 2` 加大启动间隔。

3. **轮询批量汇总**（每 20～30 秒）：

```bash
python "${agent_core}/skills/image_editor_api/bridge.py" batch-status --batch <batch_id>
```

* `status: "running"` → 继续等
* `status: "done"` → 全部成功，从 `jobs[].output_path` 取路径
* `status: "partial"` → 部分成功，检查 `jobs` 里各条 `status` / `error`
* `status: "error"` → 全部失败

4. **交付**：汇总所有 `output_path` 展示给用户。

**禁止**：对同一目录重复 `batch-launch`（会 N 倍扣费）。失败项可对单张用 `launch` 补跑 **1 次**。

## Constraints & Error Handling

| 情况 | 处理 |
|------|------|
| `Remote end closed connection` 但 PackyCode 有消费 | 先 `status` / `batch-status`；勿立刻整批重跑 |
| HTTP 401 | 检查 `LLM_API_KEY` |
| HTTP 403 额度不足 | 充值 |
| HTTP 503 / 无渠道 | 检查 PackyAPI 模型与分组 |
| 同一任务重复 launch / batch-launch | **禁止** |

**禁止**：Accio 中用同步 `run` 长等；禁止对同一批图片连续多次 `batch-launch`。

## 示例

### 单张

```bash
python "${agent_core}/skills/image_editor_api/bridge.py" launch \
  --input 1688_images/keyboard_v68.png \
  --prompt "改成国际站金品主图风格，白底专业布光" \
  --output 1688_images/processed

python "${agent_core}/skills/image_editor_api/bridge.py" status --job <job_id>
```

### 文生图

```bash
python "${agent_core}/skills/image_editor_api/bridge.py" generate-launch \
  --prompt "FURYCUBE V68 紧凑机械键盘商品主图，粉白键帽，浅灰专业摄影棚背景，B2B电商风格，无文字水印" \
  --output 1688_images/generated \
  --name keyboard_v68_gen.png

python "${agent_core}/skills/image_editor_api/bridge.py" status --job <job_id>
```

### 批量修图

```bash
python "${agent_core}/skills/image_editor_api/bridge.py" batch-launch \
  --input-dir 1688_images/inbox \
  --prompt "去除水印和多余文字，自然修复背景" \
  --output 1688_images/processed/batch_out

python "${agent_core}/skills/image_editor_api/bridge.py" batch-status --batch <batch_id>
```

`batch-launch` 返回示例：

```json
{
  "ok": true,
  "status": "running",
  "batch_id": "a1b2c3d4e5f6",
  "total": 5,
  "done": 0,
  "running": 5,
  "jobs": [
    {"job_id": "aaa...", "input_path": "/path/a.png", "status": "running"}
  ],
  "status_command": "python \".../bridge.py\" batch-status --batch a1b2c3d4e5f6"
}
```

`batch-status` 完成示例：

```json
{
  "ok": true,
  "status": "done",
  "batch_id": "a1b2c3d4e5f6",
  "total": 5,
  "done": 5,
  "error": 0,
  "running": 0,
  "jobs": [
    {"job_id": "aaa...", "input_path": "...", "status": "done", "output_path": ".../a_edited.png"}
  ]
}
```

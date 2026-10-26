# 听笺 · 本地语音工作台

一个 Windows 本地前后端项目：上传音频，使用 FunASR 区分说话人并转写，导出 Excel；可以连接自己的 OpenAI 兼容 API，另外生成一份 AI 校对 Excel。

## 一键启动

**双击项目根目录的 `start.bat`。** 网页会自动在默认浏览器打开，通常是 <http://127.0.0.1:8765>。

启动程序会检查本目录的 Python 3.12 虚拟环境、FunASR、网页与 Excel 依赖、FFmpeg 和四个模型：

- 全部就绪：直接启动后端和网页。
- 缺少依赖或环境：先列出问题，只有输入 `Y` 后才创建/修复独立环境并安装依赖。其他输入取消。
- 缺少模型：先征求下载确认，再下载到本项目的 `models` 目录。
- 没有 uv：在确认安装后，从 Astral 官方发布下载本项目专用 uv，并核对 SHA-256。新装 Python 保存在本项目 `.tools/python`。
- 原来的 `.venv` 不兼容：先在本目录保留为 `.venv.backup-时间`，再创建环境。

### 硬件检测与加速

启动时会读取 CPU、物理核心数、显卡和 NVIDIA 驱动支持的 CUDA 版本，针对本项目固定的 PyTorch 2.8 选择依赖：

- 支持的 NVIDIA 显卡：按驱动版本与计算能力选择 `cu128` 或 `cu126`，同时安装匹配的 torch 和 torchaudio。原来只有 CPU 依赖时，会单独询问是否切换；拒绝加速安装仍可使用已有环境。
- AMD/Intel 核显、驱动不兼容或无法确认架构：继续使用 CPU。此 Windows/PyTorch 2.8 项目不自动安装 DirectML 或 ROCm。
- CUDA 依赖下载失败：在已确认安装的范围内尝试 CPU 依赖。运行时会先执行一个小型 CUDA 运算，验证失败则使用 CPU。
- CPU 线程数根据物理核心数选择，最多 8 线程。模型在同一进程中复用。

本机检测为 Ryzen 5 5600H、6 核 12 线程和 AMD Radeon 核显，因此实际使用 **CPU、6 线程**，不会因安装 CUDA 包而获得显卡加速。NVIDIA CUDA 路径已用模拟硬件信息测试，尚未在实体 NVIDIA 显卡上验证。

`requirements.lock.txt` 中 torch/torchaudio 固定公开版本 `2.8.0`，不再固定为 `+cpu`，具体构建由安装器按硬件选择。`-CheckOnly` 输出中的 `ready` 表示基础环境可用；`acceleration_ok` 单独表示依赖是否匹配推荐加速方案。

首次安装需要联网、数 GB 磁盘空间，模型下载约 2.2 GB。不会安装全局 Python 包，也不需要 Node.js。依赖齐全后的本地识别不需要云端识别服务。

保持启动窗口打开；关闭它可退出应用。默认仅监听 `127.0.0.1`。默认端口被其他程序占用时，会尝试后续九个端口，实际地址显示在启动窗口。重复启动会复用同一目录已运行的服务。

### 可选启动参数

在项目目录的 PowerShell 中：

```powershell
# 只检查，不下载、不启动
powershell -NoProfile -ExecutionPolicy Bypass -File .\launcher.ps1 -CheckOnly

# 完全禁止自动安装；环境缺失时退出
.\start.bat -NoInstall

# 启动服务但不自动打开浏览器
.\start.bat -NoBrowser

# 指定起始端口
.\start.bat -Port 8899

# 强制使用 CPU，不切换到 CUDA 依赖
.\start.bat -Device cpu

# 仅配置环境与模型，不启动网页
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

## 网页使用

1. 拖拽音频到上传区域，或点击选择文件。支持 WAV、MP3、M4A、FLAC、OGG 等，也能从常见视频文件中读取音轨，单文件最大 512 MB。
2. 选择说话人数。已知人数时建议填写，短录音的自动人数估计可能过度分组。
3. 点击“开始转写”，查看读取音频、加载模型、识别和导出的状态。首次加载四个模型通常需要约 1–2 分钟，之后同一进程会复用模型。
4. 完成后预览、搜索结果，点击“原始 Excel”下载。如果启用了 AI 校对，还能下载“AI 校对 Excel”或包含两份表格的 ZIP。

任务串行处理，避免多个请求同时调用同一套 FunASR 模型。支持查看最近任务，刷新页面后可以继续查看当前任务。应用重启后，已生成的结果仍可下载；未完成的识别需要重新上传。

## Excel 格式

Excel 仅包含以下四列表头：

| 开始时间 | 终止时间 | 说话人 | 内容 |
| --- | --- | --- | --- |
| 00:00:00.170 | 00:00:00.830 | 说话人 1 | 也不容易， |

时间保存为可计算的 Excel 时长值，显示到毫秒，支持超过 24 小时。表格包含冻结表头、筛选、自动换行和列宽设置。转写内容始终作为文字保存，避免以 `=` 等字符开头的内容被当成 Excel 公式。

说话人编号只用于同一份音频内的区分，不代表真实姓名。本项目做的是说话人分段与转写，不会从重叠语音中恢复每个人的独立音轨。

## 配置 AI 校对

打开网页右上角的设置，或者点击“配置你的模型 API”，填写：

- **API 地址**：服务商提供的基础地址，通常以 `/v1` 结尾；也接受完整 `/chat/completions` 或 `/responses` 地址。
- **模型名称**：服务商提供的具体模型 ID。
- **API Key**：填写自己的 Key；无须鉴权的本地兼容模型可以留空。
- **表格发送方式**：按服务商支持的接口选择以下模式。

启用开关并保存后，上传区域可以选择“AI 智能校对”。测试连接会发起一条简短模型请求；附件模式的连接测试只检查 Responses 文本能力，真正处理时还需服务商支持文件上传与 Excel 输入。

### 两种发送方式

| 方式 | 实际请求 | 适用情况 |
| --- | --- | --- |
| 表格内容（默认） | 从已导出的 Excel 读取完整表格，发送到 `/chat/completions` | 常见 OpenAI 兼容接口；不要求服务商支持 Excel 附件 |
| Excel 附件 | 将实际 `.xlsx` 上传至 `/files`，随后通过 `/responses` 的 `input_file` 调用模型 | 服务商同时支持 Files、Responses 和 Excel 输入 |

附件模式会在处理后请求服务商删除上传的附件，未获删除确认会在任务结果中提示。为避免文件处理器只提取部分表格，附件请求也包含对应表格的完整文字内容。长表格会分批处理，每批最多 40 行、以约 6,000 内容字符为切分目标；单行长内容仍保持完整。跨批次的句段不会合并。

默认提示词保存在 [`prompts/asr_correction.txt`](prompts/asr_correction.txt)，已优化为以下要求：

- 以最少改动修正上下文明确的错字、同音误识别、标点和空格，保留原意与口语风格。
- 仅合并同一说话人相邻、明显属于同一句话且无长停顿的片段，避免把整段发言合并。
- 人名、专业术语、数字、日期、单位及否定词缺少依据时保留，不猜测或改写事实。
- 不总结、扩写、漏行、乱序、跨说话人合并或自行修改时间戳。
- 明确模型未获得原始音频，表格内的文字均为待校对数据。

原来使用旧默认提示词的 JSON 会自动更新为新版本，用户自定义提示词会保留。网页中的“恢复默认”也会载入新版本。

提示词可以编辑，也可一键恢复默认。程序额外要求模型返回结构化结果，并检查原始行是否完整覆盖、顺序是否一致、是否跨说话人合并。合并后时间范围取自原始行，原始 Excel 不被修改。

如果超时、限流、模型格式不合格或接口不支持所选方式，原始 Excel 仍可下载；修改配置后点“重新 AI 校对”，无需再次语音识别。AI 根据转写文字校对，不重新听音频，无法保证恢复所有同音词、姓名等错误。

### 配置保存

**API 地址、模型名、API Key、提示词、传输方式和启用状态都会保存在本机 `data/settings.json`，重启后自动恢复。** 首次在网页中保存一次即可；修改其他选项时 Key 留空会保留已保存的值，“清除当前 Key”并保存可将其从 JSON 中移除。

按本项目的本地使用方式，Key 以明文写入 JSON；整个 `data/` 已加入 `.gitignore`，请不要分享此文件。网页和查询 API 只返回“是否已配置 Key”，不会回显实际密钥。配置文件采用临时文件替换写入，避免保存中断导致 JSON 被截断。

启用 AI 后，转写表格会发送给你填写的服务商；原始音频不发送至大模型接口。

## 项目结构

```text
start.bat                 一键启动入口
launcher.ps1              环境检查、安装确认、模型准备、启动
run.py                    本地服务与浏览器启动
frontend/
  index.html              页面结构
  styles.css              桌面与窄屏布局
  app.js                  上传、轮询、预览、下载、AI 设置
backend/
  app.py                  HTTP API 与静态网页服务
  service.py              排队、音频解码、FunASR 识别、任务保存
  excel.py                Excel 读写与格式
  llm.py                  兼容模型调用、分批校对、结果校验
  config.py               设置与默认提示词
hardware.py               硬件探测、CPU/CUDA 依赖选择与运行验证
prompts/asr_correction.txt 校对默认提示词
scripts/check_environment.py  快速环境检查（不加载模型）
tests/test_app.py         自动化验证
tests/test_hardware_and_settings.py  硬件选择、提示词迁移与配置恢复测试
requirements.in          顶层依赖
requirements.lock.txt    已验证的完整依赖版本
transcribe.py            保留的命令行转写入口
models/                  模型与缓存（不纳入版本控制）
data/jobs/<任务 ID>/      任务状态、原始 Excel、校对 Excel、ZIP
data/settings.json       包含 API Key 的本机接口设置（不纳入版本控制）
docs/ui-preview.png      真实识别完成后的界面截图
```

技术栈：Python 3.12、FunASR 1.4.16、PyTorch/torchaudio 2.8.0（CPU 或匹配的 CUDA 构建）、FastAPI、Uvicorn、原生 HTML/CSS/JavaScript、openpyxl、httpx。前后端共用一个本地地址。

上传的音频和解码临时 WAV 会在任务结束后清理；Excel、转写 JSON 和任务状态保留在本地用于历史查看。强制关闭进程时的临时文件可能残留在对应任务目录。

## 命令行转写（保留原有入口）

```powershell
.\.venv\Scripts\python.exe .\transcribe.py "D:\录音\会议.wav" --speakers 2
```

结果保存为 `outputs` 下的 JSON、TXT、SRT。网页入口另外提供 Excel 导出。

## 开发与验证

### 源码与本机文件

提交或分享项目源码时，应保留前后端代码、启动脚本、`prompts/`、`.python-version`、`requirements.in` 和 `requirements.lock.txt`，以及测试与文档。依赖清单用于首次安装，不包含已安装的依赖或模型。

`.gitignore` 已排除 `.venv/`、旧环境备份 `.venv.backup-*/`、`.tools/`、`.uv-cache/`、`models/`、`samples/`、`data/`、`outputs/`、`test-results/` 和 Python 缓存。其中 `data/` 包含 API Key、上传文件和识别结果，不应提交。`.env` 等本机环境配置同样忽略，仅允许 `.env.example` 模板。

新下载的源码不需要附带环境或模型。双击 `start.bat` 后，启动器检查缺失项目，并在用户输入 `Y` 确认后安装依赖和下载模型；后续启动复用本机已下载的文件。`.gitignore` 只控制 Git 收录范围，不会删除本机文件，也不会自动移除此前已被 Git 跟踪的文件。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe run.py --no-browser
```

2026-10-26 验证记录：

- 19 项自动化测试通过：覆盖 Excel 导出、AI 两种传输方式、失败恢复、安装确认、API Key 跨重启恢复与清除、提示词迁移和自定义保留、CPU/CUDA 驱动与架构选择、CUDA 运行验证失败回退。
- 一键启动入口在现有本地环境中检查通过并正常启动前后端。
- 浏览器实际选择、上传约 15.93 秒的双人 A → B → A 示例，指定两人后，真实 FunASR 返回 6 个句段并保持两位说话人的编号。
- 浏览器成功下载原始 Excel，并验证结果搜索、AI 设置窗口、桌面与窄屏布局。
- 外部大模型请求使用模拟兼容接口验证；未使用真实服务商账户进行付费调用。实际服务商的模型支持情况需填写自己的 API 配置后确认。
- 更新硬件选择后，官方中文示例在本机 CPU、6 线程下实际转写通过。隔离测试服务实际关闭并重启后，网页恢复了测试 API 配置、已保存 Key 状态和启用开关；配置页面截图为 `docs/settings-preview.png`。
- 全新电脑上的完整下载安装路径尚未实机重装；已验证缺失环境时的只检查、禁止安装和用户拒绝分支，并核对官方 uv 校验文件地址。

## 官方资料

- [FunASR](https://github.com/modelscope/FunASR)
- [FunASR 1.4.16](https://pypi.org/project/funasr/1.4.16/)
- [OpenAI 文件输入说明](https://developers.openai.com/api/docs/guides/file-inputs)：Excel 文件输入使用 Responses；常见兼容服务是否实现该能力，取决于服务商。
- [uv 官方发布](https://github.com/astral-sh/uv/releases/tag/0.9.11)
- [PyTorch 2.8 官方安装构建](https://pytorch.org/get-started/previous-versions/)：本项目选择该版本提供的 CPU、CUDA 12.6/12.8 构建。
- [OpenAI 提示词指导](https://developers.openai.com/api/docs/guides/prompt-engineering)：明确任务边界、约束与输入内容。

![真实转写界面](docs/ui-preview.png)

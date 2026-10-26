"""Portable Agent review packages contain task files, never API configuration."""
import zipfile
from pathlib import Path

from .config import ROOT

SKILL = ROOT / ".agents" / "skills" / "asr-paragraph-review"


def write_skill_archive(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".pending.zip")
    with zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED) as archive:
        for source in SKILL.rglob("*"):
            if source.is_file() and "__pycache__" not in source.parts:
                archive.write(source, str(Path(SKILL.name) / source.relative_to(SKILL)))
    pending.replace(path)
    return path


def build_handoff(settings, directory, job):
    directory = Path(directory)
    references = job.get("reference_rows", [])
    prompt = ("请使用 $asr-paragraph-review（或直接按本任务说明）整理随附校对包。\n"
              "读取“主稿.xlsx”；有“Nano参考.xlsx”或“Qwen参考.xlsx”时仅在疑难处对照参考。\n"
              "同一人围绕一件事的连续发言合并成大句或段落，只合并、不拆分原句段。\n"
              "请直接生成并交还“AI整理.xlsx”，表头严格为：开始时间、终止时间、说话人、内容。"
              "合并行时间取主稿第一行开始与最后覆盖行的终止时间，不估算新时间，不采用参考稿的说话人或时间。"
              "覆盖全部主稿内容，保留原始主稿文件，完成后提供原始 Excel 和整理 Excel 两份下载。"
              "不需要输出或回传 JSON，也不需要回到网页再次生成 Excel。\n\n" + settings.prompt
              + "\n\n本次交接的最终交付格式以 Excel 为准。若自定义提示词中提到 JSON，只将其视为旧 API 设置；请直接交还原始 Excel 与 AI整理.xlsx。")
    (directory / "PROMPT.md").write_text(prompt, encoding="utf-8")
    notes = (f"# 任务说明\n\n录音文件：{job['filename']}\n\n"
             f"主稿模型：{job.get('asr_model', 'ASR')}\n\n主稿句段：{len(job['rows'])}\n\n"
             f"LLM 语音参考模型：{job.get('reference_model', 'Fun-ASR-Nano')}；{'可用' if references else '不可用或未启用，按主稿整理'}\n\n"
             + "\n".join(job.get("warnings", [])) + "\n\n本机发言合并.xlsx 是按连续说话人合并的预览，不是 AI 校对结果。\n")
    (directory / "agent-notes.md").write_text(notes, encoding="utf-8")
    files = (("original.xlsx", "主稿.xlsx"), ("paragraphs.xlsx", "本机发言合并.xlsx"),
             ("nano-reference.xlsx", "Nano参考.xlsx"), ("llm-reference.xlsx", "Qwen参考.xlsx"), ("PROMPT.md", "校对任务.md"), ("agent-notes.md", "任务说明.md"))
    pending = directory / "agent-review.pending.zip"
    with zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, label in files:
            path = directory / name
            if path.is_file():
                archive.write(path, label)
        for source in SKILL.rglob("*"):
            if source.is_file() and "__pycache__" not in source.parts:
                archive.write(source, str(Path("skills") / SKILL.name / source.relative_to(SKILL)))
    pending.replace(directory / "agent-review.zip")
    return prompt

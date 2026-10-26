"""Coverage diagnostics distinguish silence from suspected missing recognition."""


def coverage(rows, duration_ms):
    ordered = sorted(rows, key=lambda row: row["start_ms"])
    cursor, gaps = 0, []
    for row in ordered:
        if row["start_ms"] - cursor >= 30000:
            gaps.append({"start_ms": cursor, "end_ms": row["start_ms"]})
        cursor = max(cursor, row["end_ms"])
    tail = max(0, duration_ms - cursor)
    if tail >= 30000:
        gaps.append({"start_ms": cursor, "end_ms": duration_ms})
    warnings = []
    if gaps:
        warnings.append(f"有 {len(gaps)} 段超过 30 秒没有转写文字，可能是静音，也可能漏识别，请对照音频检查。")
    # Report suspicious collapse into tiny fragments, without declaring it wrong.
    if duration_ms >= 300000:
        recent = [row for row in ordered if row["end_ms"] >= duration_ms - 300000]
        if len(recent) >= 30 and sum(len(row["content"].strip()) <= 4 for row in recent) / len(recent) >= .8:
            warnings.append("最后 5 分钟大多是极短句段；若原音频有连续讲话，请换用精度优先模型重新识别。时间戳覆盖到末尾不代表文字完整。")
    return {"last_end_ms": cursor, "tail_gap_ms": tail, "long_gaps": gaps}, warnings

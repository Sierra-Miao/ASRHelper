"""Group consecutive same-speaker fragments, preserving source time bounds."""


def local_paragraphs(rows, max_gap_ms=10000, max_duration_ms=300000, max_chars=3000):
    output = []
    for row in rows:
        if (output and output[-1]["speaker"] == row["speaker"]
                and 0 <= row["start_ms"] - output[-1]["end_ms"] <= max_gap_ms
                and row["end_ms"] - output[-1]["start_ms"] <= max_duration_ms
                and len(output[-1]["content"]) + len(row["content"]) <= max_chars):
            previous = output[-1]
            separator = " " if previous["content"][-1:].isascii() and previous["content"][-1:].isalnum() and row["content"][:1].isascii() and row["content"][:1].isalnum() else ""
            previous["content"] += separator + row["content"]
            previous["end_ms"] = max(previous["end_ms"], row["end_ms"])
        else:
            output.append({key: row[key] for key in ("start_ms", "end_ms", "speaker", "content")})
    return output

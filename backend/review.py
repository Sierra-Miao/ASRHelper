"""Validate source coverage for API and imported Agent review results."""
import json
import re


class CorrectionError(Exception):
    pass


def validate_groups(text, source_table, source_rows):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        groups = json.loads(cleaned)["groups"]
        if not isinstance(groups, list) or not groups:
            raise ValueError()
        expected, seen, output = [row["id"] for row in source_table], [], []
        for group in groups:
            ids = group["source_ids"]
            if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids):
                raise ValueError()
            if ids != expected[len(seen):len(seen) + len(ids)]:
                raise ValueError()
            originals = [source_rows[i - 1] for i in ids]
            if len({row["speaker"] for row in originals}) != 1:
                raise ValueError()
            if "sentences" in group:
                raise ValueError()
            content = group["content"]
            if not isinstance(content, str) or not content.strip() or len(content) > 32767:
                raise ValueError()
            output.append({"start_ms": originals[0]["start_ms"], "end_ms": max(row["end_ms"] for row in originals),
                           "speaker": originals[0]["speaker"], "content": content.strip()})
            seen.extend(ids)
        if seen != expected:
            raise ValueError()
        return output
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise CorrectionError("校对结果不合格：存在漏行、乱序、跨说话人合并、拆分或无效内容。原始结果已保留。") from exc

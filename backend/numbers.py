"""Conservative Chinese quantity formatting; does not guess spoken values."""
import re

DIGITS = dict(zip("零〇一二三四五六七八九", "00123456789"))
DIGITS["两"] = "2"
SMALL = {"十": 10, "百": 100, "千": 1000}
NUMBER = r"[零〇一二两三四五六七八九十百千万亿]+(?:点[零〇一二两三四五六七八九]+(?![零〇一二两三四五六七八九十百千万亿]))?"
UNITS = r"(?:年|月|日|号|岁|个|位|次|件|人|元|块|万|亿|秒|分|分钟|小时|点|米|公里|公斤|千克|克|斤|吨|度|页|章|节|毫升|升|倍|天|周|星期|个月|万元|亿元)"
IDIOMS = re.compile("万一|千万不要|千万别|千万不能|千方百计|一五一十|一心一意|三心二意|五花八门|七上八下|乱七八糟|九牛一毛|百发百中|一石二鸟|不三不四|说三道四|十全十美|四面八方")


def integer(text):
    if re.search(r"[百千万亿][一二两三四五六七八九]$", text):
        raise ValueError("Ambiguous colloquial quantity")
    if all(char in DIGITS for char in text):
        return "".join(DIGITS[char] for char in text)
    for unit, scale in (("亿", 100000000), ("万", 10000)):
        if unit in text:
            left, right = text.split(unit, 1)
            return str(int(integer(left or "一")) * scale + int(integer(right or "零")))
    total, current = 0, 0
    for char in text:
        if char in DIGITS:
            current = int(DIGITS[char])
        else:
            total += (current or 1) * SMALL[char]
            current = 0
    return str(total + current)


def number(text):
    whole, dot, decimal = text.partition("点")
    return integer(whole) + ("." + "".join(DIGITS[c] for c in decimal) if dot else "")


def normalize_numbers(text):
    protected = [(match.start(), match.end()) for match in IDIOMS.finditer(text)]
    pattern = re.compile(rf"百分之(?P<percent>{NUMBER})|(?P<number>{NUMBER})")

    def replace(match):
        if any(match.start() < end and match.end() > start for start, end in protected):
            return match.group()
        if match.group("percent"):
            try:
                return number(match.group("percent")) + "%"
            except (ValueError, KeyError):
                return match.group()
        value = match.group("number")
        before, after = text[:match.start()], text[match.end():]
        # Only quantities with explicit units, ordinal markers or numeric serials.
        # Leave unqualified short words alone: names and idioms can contain digits.
        if (re.match(UNITS, after) or before.endswith("第") or "点" in value
                or (len(value) >= 3 and all(c in DIGITS for c in value))
                or (any(c in value for c in "十百千") and (not after or after[0] in "，。！？、；：,.!?;: "))):
            try:
                return number(value)
            except (ValueError, KeyError):
                pass
        return match.group()

    return pattern.sub(replace, text)

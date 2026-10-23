# Dumps the zh-CN augment table to a UTF-8 file (console encoding mangles CJK output).

import io
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg")


def flight(text):
    parts = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,\s*(".*?")\s*\]\)', text, re.S):
        try:
            parts.append(json.loads(m.group(1)))
        except Exception:
            pass
    return "".join(parts)


def rows(payload):
    out = []
    for m in re.finditer(r'"data":\[', payload):
        s = payload.index("[", m.start())
        try:
            arr, _ = json.JSONDecoder().raw_decode(payload[s:])
        except Exception:
            continue
        for o in arr:
            if isinstance(o, dict) and "name" in o and "rarity" in o:
                out.append(o)
    return out


lines = []
for tag in ["zh_CN", "en"]:
    path = os.path.join(OUT, "augments_yasuo_%s.html" % tag)
    if not os.path.exists(path):
        continue
    with io.open(path, encoding="utf-8") as fh:
        data = rows(flight(fh.read()))
    lines.append("=== yasuo (%s) : %d augments ===" % (tag, len(data)))
    for o in data[:10]:
        lines.append("  %-22s rarity=%s tier=%s popular=%-6s performance=%s"
                     % (o["name"], o.get("rarity"), o.get("tier"),
                        o.get("popular"), o.get("performance")))
    lines.append("  中文关键词检查: %s"
                 % [o["name"] for o in data if any("\u4e00" <= ch <= "\u9fff" for ch in o["name"])][:6])
    lines.append("")

with io.open(os.path.join(OUT, "_zh_names.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("written: opgg/_zh_names.txt")

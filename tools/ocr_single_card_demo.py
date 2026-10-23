"""单卡端到端演示：截图 -> OCR -> matcher -> augmentId。

    python tools/ocr_single_card_demo.py <图片路径> [更多图片...]

没有传参时使用 data/_ocr_smoke.png（合成图，仅用于链路自检）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.augment_repository import AugmentRepository  # noqa: E402
from src.ocr.engine import OcrEngine, OcrUnavailableError  # noqa: E402
from src.ocr.matcher import AugmentMatcher, normalize_text  # noqa: E402


def main(argv: list[str]) -> int:
    paths = argv or ["data/_ocr_smoke.png"]
    try:
        engine = OcrEngine()
    except OcrUnavailableError as exc:
        print("RapidOCR 不可用: %s" % exc)
        return 1

    repository = AugmentRepository().load()
    matcher = AugmentMatcher(repository)
    print("字典: Mayhem %d 条" % len(repository.items))

    matched = 0
    for path in paths:
        print("")
        print("=" * 70)
        print("image: %s" % path)
        results = engine.recognize(path)
        if not results:
            print("  OCR: 无结果（文件不存在或识别失败）")
            continue
        for r in results:
            print("  OCR  text=%r conf=%.4f bbox=%s" % (r.text, r.confidence, r.bbox))
        joined = "".join(r.text for r in results)
        print("  拼接文本: %r" % joined)
        print("  标准化  : %r" % normalize_text(joined))

        match = matcher.match(joined)
        if match.matched:
            item = repository.get(match.augment_id) if match.augment_id else None
            matched += 1
            print("  匹配    : augmentId=%s name_cn=%s level=%s method=%s conf=%.4f"
                  % (match.augment_id, match.name_cn,
                     (item.level if item else "-"), match.method, match.confidence))
        else:
            print("  匹配    : 无 (method=none, best_ratio=%.4f)" % match.confidence)

    print("")
    print("Total: %d   Matched: %d   Failed: %d"
          % (len(paths), matched, len(paths) - matched))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

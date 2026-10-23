"""透明置顶 Overlay（PySide6 延迟导入）。"""


def pyside6_available() -> bool:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return False
    return True


__all__ = ["pyside6_available"]

"""保证无论从哪个目录/用何种方式调用 pytest，backend 根目录都在 sys.path 上，
这样测试里的 `from app....` 导入始终可用。"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

"""src レイアウトのパッケージを pytest から解決するための設定。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

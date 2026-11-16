"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS

# 这些表由异常检测的内部数据流使用，不算运营概览里的独立业务模块。
AUXILIARY_MODULES = {
    "anomalydetect_snapshot",
    "anomalydetect_dataset",
    "anomalydetect_event",
}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()

    def module_names(self) -> list[str]:
        """运营概览只暴露业务模块，内部数据表不单独计模块。"""
        return sorted(name for name in self._tables if name not in AUXILIARY_MODULES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        """分配模块内自增编号；并发写入时也不会拿到重复 id。"""
        with self._lock:
            return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def append(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            row.setdefault("id", self.next_id(module))
            self.rows(module).append(row)
            return row

    def remove(self, module: str, row: dict[str, Any]) -> None:
        with self._lock:
            rows = self.rows(module)
            if row in rows:
                rows.remove(row)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()

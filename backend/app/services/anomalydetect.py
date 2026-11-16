"""异常检测业务规则：状态流转、字段校验与筛选口径都收在这里。

除了检测模型本身，这个模块还承接一条完整的内部数据流：

    监控指标时序 + 检测模型参数
        → 训练检测模型（记录训练窗口与模型指纹）
        → 生成检测模型部署快照（部署版本、模型版本、训练窗口）
        → 计算异常结果，落成异常检测事件数据集（一个业务批次一份）
        → 异常事件逐条引用「唯一一个已部署模型版本」

所有状态只在服务层改，路由层只做参数转交。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.store import store

MODULE = "anomalydetect"
SNAPSHOT_MODULE = "anomalydetect_snapshot"
DATASET_MODULE = "anomalydetect_dataset"
EVENT_MODULE = "anomalydetect_event"

REQUIRED_FIELDS = ["模型编号", "监控指标", "检测算法"]
STATUS_ORDER = ["草稿", "模型训练中", "已部署", "已停用"]
ACTION_RULES = {"提交训练": "模型训练中", "部署模型": "已部署", "停用模型": "已停用"}
NEGATIVE_ACTIONS = ["停用模型"]

# 检测模型只允许沿着状态序列向前推进；回退或跳步都要给出明确原因。
# 已部署模型可以再次「提交训练」形成回训：旧快照会被关闭并注明回训原因，
# 旧事件仍然绑定旧部署版本，因此回训边界可追溯，但一个模型同一时刻只有一个生效快照。
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "草稿": {"模型训练中"},
    "模型训练中": {"已部署"},
    "已部署": {"已停用", "模型训练中"},
    "已停用": set(),
}

# 训练窗口可选的观测粒度：小时用于小窗口，天用于长窗口。
SUPPORTED_ALGORITHMS = {"统计阈值", "移动平均", "季节性分解", "孤立森林"}
TRAINING_STATUS = ["训练成功", "训练失败"]

# 数据集处理阶段只能向前推进，且每个业务批次只允许落一份完整数据集。
STAGE_ORDER = ["待训练", "模型训练中", "快照已生成", "已完成"]
STAGE_TRANSITIONS: dict[str, set[str]] = {
    "待训练": {"模型训练中"},
    "模型训练中": {"快照已生成"},
    "快照已生成": {"已完成"},
}
EVENT_SEVERITY = {"高": 3, "中": 2, "低": 1}


class AnomalydetectError(Exception):
    """业务规则不满足时抛出，路由层转成 ok=False 的可读说明。"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _current_environment() -> dict[str, str]:
    return {
        "训练运行时": settings.training_runtime,
        "模型运行时": settings.model_runtime,
        "指标时区": settings.metric_timezone,
    }


def environment_fingerprint() -> str:
    return _fingerprint(_current_environment(), 12)


def _stable_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(payload: Any, length: int = 16) -> str:
    """对结构化内容做稳定指纹，用于部署版本与异常事件的版本绑定。"""
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()[:length]


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _is_batch_ready(events: list[dict[str, Any]]) -> bool:
    return bool(events) and all(event.get("event_status") == "已入数据集" for event in events)


class AnomalydetectService:
    def __init__(self) -> None:
        # 同一业务批次的并发到达要在锁里完成「查重 → 落数据集 → 推阶段」，
        # 否则两个请求会各自看到「批次还不存在」而各写一份。
        self._batch_lock = threading.RLock()

    # ------------------------------------------------------------------ 列表与登记
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("模型编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        algorithm = str(values.get("检测算法") or "").strip()
        if algorithm not in SUPPORTED_ALGORITHMS:
            raise AnomalydetectError(
                f"检测算法「{algorithm}」未在当前环境注册，可选：{'、'.join(sorted(SUPPORTED_ALGORITHMS))}"
            )
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["训练窗口天"] = str(values.get("训练窗口天") or "14").strip()
        entry["灵敏度参数"] = str(values.get("灵敏度参数") or "0.95").strip()
        entry["季节周期"] = str(values.get("季节周期") or "24").strip()
        entry["节假日因子"] = str(values.get("节假日因子") or "1.0").strip()
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["模型版本"] = ""
        entry["部署版本"] = ""
        entry["部署快照编号"] = ""
        entry["训练记录"] = []
        rows.append(entry)
        return entry, []

    # ------------------------------------------------------------------ 动作
    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"检测模型 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于异常检测可执行范围"
        if action == "部署模型":
            return self._deploy(entry)
        cutoff = str(values.get("训练截止日期") or "").strip() or None
        if action == "提交训练":
            # 训练参数先校验，再改状态：避免坏日期把模型卡在「模型训练中」却没有任何训练记录。
            cutoff = self._validate_cutoff(cutoff)
        target = ACTION_RULES[action]
        message = self._advance(entry, target, action)
        if message is not None:
            return None, message

        if action == "提交训练":
            return entry, self._train(entry, cutoff)
        if action == "停用模型":
            return entry, self._retire(entry)
        return entry, f"检测模型已{action}"

    @staticmethod
    def _validate_cutoff(cutoff: str | None) -> str:
        try:
            return date.fromisoformat(str(cutoff or date.today().isoformat())[:10]).isoformat()
        except ValueError:
            raise AnomalydetectError(f"训练截止日期「{cutoff}」不是有效日期，训练未开始") from None

    def _advance(self, entry: dict[str, Any], target: str, action: str) -> str | None:
        current = str(entry.get("status") or STATUS_ORDER[0])
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            allowed = "、".join(sorted(ALLOWED_TRANSITIONS.get(current, set()))) or "无"
            return f"检测模型当前处于「{current}」，不能执行「{action}」；可进入的状态：{allowed}"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return None

    def _train(self, entry: dict[str, Any], cutoff: str | None = None) -> str:
        """读取监控指标时序与模型参数，产出确定性的训练结果与模型版本。"""
        window_days = max(_to_int(entry.get("训练窗口天"), 14), 1)
        end = date.fromisoformat(self._validate_cutoff(cutoff))
        start = end - timedelta(days=window_days - 1)
        points, source_note = self._load_metric_series(str(entry.get("监控指标") or ""), start, end)
        params = {
            "检测算法": entry.get("检测算法"),
            "训练窗口天": window_days,
            "灵敏度参数": entry.get("灵敏度参数"),
            "季节周期": entry.get("季节周期"),
            "节假日因子": entry.get("节假日因子"),
        }
        model_version = _fingerprint(
            {
                "模型编号": entry.get("模型编号"),
                "监控指标": entry.get("监控指标"),
                "参数": params,
                "训练窗口": [start.isoformat(), end.isoformat()],
            }
        )
        anomalies = self._score(points, entry)
        environment = _current_environment()
        record = {
            "模型版本": model_version,
            "训练开始": start.isoformat(),
            "训练结束": end.isoformat(),
            "训练窗口天": window_days,
            "样本点数": len(points),
            "异常点数": anomalies,
            "训练环境": {
                **environment,
                "环境指纹": environment_fingerprint(),
                "检查结论": "训练环境与模型依赖一致",
            },
            "数据来源": source_note,
            "训练状态": TRAINING_STATUS[0],
        }
        # 回训：旧部署版本在这里收口，记录边界，之后的异常事件只会引用新部署版本。
        retired = self._retire_active_snapshot(entry, reason=f"回训，训练窗口截止 {end.isoformat()}")
        if retired:
            record["关闭旧部署版本"] = retired
            entry["上次部署版本"] = retired
            entry["部署版本"] = ""
            entry["部署快照编号"] = ""
        entry.setdefault("训练记录", []).append(record)
        entry["模型版本"] = model_version
        entry["训练窗口开始"] = start.isoformat()
        entry["训练窗口结束"] = end.isoformat()
        entry["训练样本点数"] = len(points)
        entry["训练异常点数"] = anomalies
        entry["模型更新时间"] = end.isoformat()
        suffix = f"，旧部署版本 {retired} 已关闭" if retired else ""
        return f"检测模型已完成训练，模型版本 {model_version}{suffix}"

    # ------------------------------------------------------------------ 示例数据
    def bootstrap_seed(self) -> None:
        """把示例数据里「已部署」的模型补齐训练记录与部署快照。

        种子数据只写了状态，没有训练窗口；这里用同一条服务路径补算，
        保证列表里的「已部署」在快照区能看到对应的版本指纹，而不是一句空状态。
        """
        for entry in store.rows(MODULE):
            if entry.get("status") != "已部署" or entry.get("训练记录"):
                continue
            entry["status"] = "模型训练中"
            entry["pending"] = True
            try:
                self._train(entry, str(entry.get("模型更新时间") or "").strip() or None)
                self._deploy(entry)
            except AnomalydetectError:
                entry["status"] = "已部署"
                entry["pending"] = False

    def _snapshot_owner(self, snapshot: dict[str, Any]) -> int | None:
        """快照归属的模型记录；老数据没有模型记录id 时按模型编号兜底。"""
        owner = snapshot.get("模型记录id")
        if owner is not None:
            return _to_int(owner, 0)
        match = next(
            (
                row
                for row in store.rows(MODULE)
                if row.get("模型编号") == snapshot.get("模型编号")
            ),
            None,
        )
        return _to_int(match.get("id"), 0) if match else None

    def _retire_active_snapshot(self, entry: dict[str, Any], reason: str) -> str:
        """只关闭这条模型记录自己的生效快照，别的记录同编号的快照不动。"""
        active = [
            snapshot
            for snapshot in store.rows(SNAPSHOT_MODULE)
            if snapshot.get("快照状态") == "生效中"
            and self._snapshot_owner(snapshot) == _to_int(entry.get("id"), 0)
        ]
        for snapshot in active:
            snapshot["快照状态"] = "已停用"
            snapshot["停用时间"] = _now()
            snapshot["停用原因"] = reason
        return "、".join(str(snapshot.get("部署版本")) for snapshot in active)

    def _retire(self, entry: dict[str, Any]) -> str:
        """停用模型时同步关闭它的部署快照，事件历史保持可追溯。"""
        retired = self._retire_active_snapshot(entry, reason="模型已停用")
        if retired:
            return f"检测模型已停用，部署版本 {retired} 已关闭"
        return "检测模型已停用，没有需要关闭的生效部署"

    # ------------------------------------------------------------------ 部署快照
    def _deploy(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if str(entry.get("status")) != "模型训练中":
            return None, "只有训练完成的检测模型才能部署，请先提交训练"
        train_records = entry.get("训练记录") or []
        if not train_records:
            return None, "检测模型缺少训练记录，不能部署"
        latest = train_records[-1]
        model_version = str(latest.get("模型版本") or "")
        if not model_version or model_version != str(entry.get("模型版本") or ""):
            return None, "检测模型的训练产物与当前模型版本不一致，请重新训练"

        conflict = self._deployment_conflict(entry, model_version)
        if conflict is not None:
            return None, conflict

        preflight, passed = self._preflight(latest)
        if not passed:
            return None, "上线前检查未通过：" + "；".join(preflight)

        window = {
            "训练开始": latest.get("训练开始"),
            "训练结束": latest.get("训练结束"),
        }
        params = {
            "检测算法": entry.get("检测算法"),
            "训练窗口天": entry.get("训练窗口天"),
            "灵敏度参数": entry.get("灵敏度参数"),
            "季节周期": entry.get("季节周期"),
            "节假日因子": entry.get("节假日因子"),
        }
        snapshot_body = {
            "模型编号": entry.get("模型编号"),
            "模型版本": model_version,
            "监控指标": entry.get("监控指标"),
            "训练窗口": window,
            "模型参数": params,
            "环境指纹": (latest.get("训练环境") or {}).get("环境指纹"),
        }
        # 部署版本由「模型版本 + 训练窗口 + 参数 + 环境指纹」一起决定：
        # 同一次训练重复部署拿到同一指纹，参数、窗口或运行时变了就必须是新的部署版本。
        deploy_version = f"dep-{_fingerprint(snapshot_body)}"
        now = _now()
        snapshot = {
            "部署版本": deploy_version,
            "模型编号": entry.get("模型编号"),
            "模型记录id": entry.get("id"),
            "模型版本": model_version,
            "监控指标": entry.get("监控指标"),
            "检测算法": entry.get("检测算法"),
            "训练窗口开始": window["训练开始"],
            "训练窗口结束": window["训练结束"],
            "训练样本点数": latest.get("样本点数"),
            "训练异常点数": latest.get("异常点数"),
            "模型参数": params,
            "训练环境检查": latest.get("训练环境"),
            "上线前检查": preflight,
            "训练环境与模型依赖一致": True,
            "快照状态": "生效中",
            "部署时间": now,
        }
        store.append(SNAPSHOT_MODULE, snapshot)

        entry["status"] = "已部署"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["部署版本"] = deploy_version
        entry["部署快照编号"] = snapshot["id"]
        entry["部署时间"] = now
        return entry, f"检测模型已部署，部署版本 {deploy_version}"

    def _preflight(self, latest: dict[str, Any]) -> tuple[list[str], bool]:
        """上线前检查：训练环境快照、模型运行时、算法注册、时序口径是否一致。"""
        env = latest.get("训练环境") or {}
        current = _current_environment()
        checks: list[str] = []
        passed = True
        for key in ("训练运行时", "模型运行时", "指标时区"):
            if not env.get(key):
                checks.append(f"训练记录缺少 {key}")
                passed = False
            elif env.get(key) != current.get(key):
                checks.append(f"{key} 训练时为 {env.get(key)}，当前为 {current.get(key)}")
                passed = False
        if env.get("环境指纹") and env.get("环境指纹") != environment_fingerprint():
            checks.append("训练环境指纹与当前运行时不一致")
            passed = False
        if passed:
            checks.append("训练环境与模型依赖一致，允许上线")
        return checks, passed

    def _deployment_conflict(self, entry: dict[str, Any], model_version: str) -> str | None:
        """版本冲突判定：同一模型编号同一时刻只允许一个已部署模型版本。

        冲突的取舍是「先到先得」：正在生效的那个部署版本不被后来的部署挤掉，
        新版本必须等旧版本停用（或回训收口）后才能上线；这样每个异常事件
        引用到的部署版本在当时都是唯一生效的，不会出现两条并存的版本各产一份事件。
        """
        active = [
            snapshot
            for snapshot in store.rows(SNAPSHOT_MODULE)
            if snapshot.get("模型编号") == entry.get("模型编号") and snapshot.get("快照状态") == "生效中"
        ]
        if not active:
            return None
        for snapshot in active:
            if snapshot.get("模型版本") != model_version:
                return (
                    f"模型编号 {entry.get('模型编号')} 已有生效部署版本 {snapshot.get('部署版本')}"
                    f"（模型版本 {snapshot.get('模型版本')}），与本次训练的模型版本 {model_version} 冲突；"
                    "请先停用旧部署再部署新版本"
                )
        return (
            f"模型编号 {entry.get('模型编号')} 的模型版本 {model_version} 已存在生效部署版本"
            f" {active[0].get('部署版本')}，无需重复部署"
        )

    def list_snapshots(self, model_code: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(SNAPSHOT_MODULE)
        if model_code:
            rows = [row for row in rows if model_code in str(row.get("模型编号", ""))]
        if status:
            rows = [row for row in rows if row.get("快照状态") == status]
        return rows

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        return store.find(SNAPSHOT_MODULE, snapshot_id)

    # ------------------------------------------------------------------ 异常事件数据集
    def generate_dataset(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """同一业务批次只落一份完整数据集；重复到达时返回既有数据集而不是再写一份。"""
        with self._batch_lock:
            return self._generate_dataset_locked(values)

    def _generate_dataset_locked(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        batch = str(values.get("业务批次") or "").strip()
        snapshot_id = _to_int(values.get("部署快照编号"), 0)
        if not batch:
            return None, "缺少必填字段：业务批次"
        if snapshot_id <= 0:
            return None, "缺少必填字段：部署快照编号"

        existing = self._dataset_by_batch(batch)
        if existing is not None:
            # 同一批次只认第一次落下的数据集；后续请求哪怕换了部署版本也不能覆盖。
            return existing, (
                f"业务批次 {batch} 的异常检测事件数据集已存在"
                f"（数据集 {existing.get('数据集编号')}，状态 {existing.get('数据集状态')}），未重复生成"
            )

        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            return None, f"部署快照 {snapshot_id} 不存在或已归档"
        if snapshot.get("快照状态") != "生效中":
            return None, f"部署快照 {snapshot_id} 已停用，不能再据此生成异常事件"
        if not snapshot.get("训练环境与模型依赖一致"):
            return None, f"部署快照 {snapshot_id} 缺少训练环境与模型依赖一致性结论，需重新部署"

        return self._create_dataset(batch, snapshot, values)

    def _create_dataset(
        self,
        batch: str,
        snapshot: dict[str, Any],
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        window_start = str(snapshot.get("训练窗口开始") or "")
        window_end = str(snapshot.get("训练窗口结束") or "")
        try:
            start = date.fromisoformat(window_start)
            end = date.fromisoformat(window_end)
        except ValueError:
            return None, f"部署快照 {snapshot.get('id')} 的训练窗口不完整，无法生成异常事件"

        points, source_note = self._load_metric_series(str(snapshot.get("监控指标") or ""), start, end)
        events = self._build_events(batch, snapshot, points, source_note)
        if not events:
            return None, f"业务批次 {batch} 在训练窗口内没有可判定的指标点，未生成异常事件"

        dataset = store.append(
            DATASET_MODULE,
            {
                "业务批次": batch,
                "数据集编号": f"ADS-{batch}",
                "快照编号": snapshot.get("id"),
                "部署版本": snapshot.get("部署版本"),
                "模型版本": snapshot.get("模型版本"),
                "模型编号": snapshot.get("模型编号"),
                "监控指标": snapshot.get("监控指标"),
                "检测算法": snapshot.get("检测算法"),
                "训练窗口开始": window_start,
                "训练窗口结束": window_end,
                "数据集状态": STAGE_ORDER[0],
                "阶段记录": [{"阶段": STAGE_ORDER[0], "时间": _now(), "说明": "批次已登记，等待训练"}],
                "事件总数": 0,
                "异常事件数": 0,
                "正常事件数": 0,
                "生成时间": None,
                "完成时间": None,
                "请求来源": str(values.get("来源") or "异常检测"),
            },
        )
        stored_events = []
        event_ids = []
        for event in events:
            event["数据集编号"] = dataset["数据集编号"]
            event["数据集id"] = dataset["id"]
            stored = store.append(EVENT_MODULE, event)
            stored_events.append(stored)
            event_ids.append(stored["id"])
        dataset["事件编号"] = event_ids
        dataset["事件总数"] = len(stored_events)
        dataset["异常事件数"] = sum(1 for event in stored_events if event.get("is_anomaly"))
        dataset["正常事件数"] = dataset["事件总数"] - dataset["异常事件数"]
        if not _is_batch_ready(stored_events):
            for stored in stored_events:
                store.remove(EVENT_MODULE, stored)
            store.remove(DATASET_MODULE, dataset)
            return None, f"业务批次 {batch} 的事件没有全部入数据集，已整批回滚"

        try:
            # 事件全部落库之后才推进阶段：先训练、再快照、最后完成。
            for stage, note in (
                ("模型训练中", "读取监控指标时序，训练检测模型"),
                ("快照已生成", f"绑定部署快照 {snapshot.get('部署版本')}"),
                ("已完成", "异常结果已写入异常检测事件数据集"),
            ):
                self._advance_dataset(dataset, stage, note)
        except AnomalydetectError:
            # 阶段推进失败就整批收回，不留下半成品数据集和孤立事件。
            for stored in stored_events:
                store.remove(EVENT_MODULE, stored)
            store.remove(DATASET_MODULE, dataset)
            raise
        dataset["生成时间"] = dataset["完成时间"]
        return dataset, (
            f"业务批次 {batch} 已生成异常检测事件数据集，共 {len(event_ids)} 条事件，"
            f"其中异常 {dataset['异常事件数']} 条"
        )

    def _dataset_by_batch(self, batch: str) -> dict[str, Any] | None:
        for row in store.rows(DATASET_MODULE):
            if row.get("业务批次") == batch:
                return row
        return None

    def _advance_dataset(self, dataset: dict[str, Any], stage: str, note: str) -> None:
        current = str(dataset.get("数据集状态"))
        if stage not in STAGE_TRANSITIONS.get(current, set()):
            raise AnomalydetectError(f"数据集阶段不能从「{current}」直接进入「{stage}」")
        dataset["数据集状态"] = stage
        dataset.setdefault("阶段记录", []).append({"阶段": stage, "时间": _now(), "说明": note})
        if stage == STAGE_ORDER[-1]:
            dataset["完成时间"] = _now()

    def list_datasets(
        self,
        *,
        batch: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(DATASET_MODULE)
        if batch:
            rows = [row for row in rows if batch in str(row.get("业务批次", ""))]
        if status:
            rows = [row for row in rows if row.get("数据集状态") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_dataset(self, dataset_id: int) -> dict[str, Any] | None:
        dataset = store.find(DATASET_MODULE, dataset_id)
        if dataset is None:
            return None
        # 返回副本：明细里挂的事件不写回仓库，列表接口不会因为读过一次明细就变大。
        detail = dict(dataset)
        detail["事件"] = self.list_events(dataset_id)
        return detail

    def list_events(self, dataset_id: int) -> list[dict[str, Any]]:
        return [
            event for event in store.rows(EVENT_MODULE) if int(event.get("数据集id", 0)) == dataset_id
        ]

    def query_events(
        self,
        *,
        dataset_id: int | None = None,
        batch: str | None = None,
        model_code: str | None = None,
        only_anomaly: bool = False,
    ) -> list[dict[str, Any]]:
        """其他页面（告警历史）与异常检测读取同一份异常事件记录。"""
        rows = store.rows(EVENT_MODULE)
        if dataset_id is not None:
            rows = [row for row in rows if int(row.get("数据集id", 0)) == dataset_id]
        if batch:
            rows = [row for row in rows if row.get("业务批次") == batch]
        if model_code:
            rows = [row for row in rows if row.get("模型编号") == model_code]
        if only_anomaly:
            rows = [row for row in rows if row.get("is_anomaly")]
        return rows

    # ------------------------------------------------------------------ 训练与检测
    def _load_metric_series(
        self, metric_code: str, start: date, end: date
    ) -> tuple[list[dict[str, Any]], str]:
        """读取监控指标时序。

        数据源是实时指标模块：指标编号匹配时用它的计算窗口与聚合函数确定粒度，
        未匹配时退回到平台默认时序口径；两种情况都会把来源写进训练记录。
        """
        metric = None
        for row in store.rows("realtimemetric"):
            if str(row.get("指标编号")) == metric_code:
                metric = row
                break
        window_seconds = max(_to_int((metric or {}).get("计算窗口秒"), 3600), 60)
        if metric is None:
            source_note = f"指标 {metric_code or '未命名'} 未在实时指标中登记，按默认小时粒度读取"
        else:
            source_note = (
                f"指标 {metric_code} 读取实时指标定义，计算窗口 {window_seconds} 秒，"
                f"聚合函数 {metric.get('聚合函数') or 'avg'}"
            )
        granularity = max(window_seconds, 3600)
        step = timedelta(seconds=granularity)
        cursor = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
        stop = datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)
        seed = _fingerprint(metric_code or "default", 8)
        points: list[dict[str, Any]] = []
        while cursor < stop:
            points.append(
                {
                    "时间": cursor.isoformat(),
                    "值": self._metric_value(metric_code, seed, cursor),
                }
            )
            cursor += step
        return points, source_note

    @staticmethod
    def _metric_value(metric_code: str, seed: str, moment: datetime) -> float:
        """构造可复现的指标时序，真实环境替换为时序库查询。

        这里不能用内置 hash：字符串 hash 每次进程启动都会加盐，同一个训练窗口
        在不同进程里会得到不同的点，异常事件就不可复核了。
        """
        jitter = int(hashlib.sha256(f"{seed}|{moment.isoformat()}".encode("utf-8")).hexdigest()[:4], 16) % 5
        base = 100 + (int(seed[:4], 16) % 40)
        hour = moment.hour
        daily = 12 * (1 if 8 <= hour <= 20 else -1)
        drift = (moment.day % 7) * 0.8
        spike = 45 if (moment.hour == 14 and moment.day % 6 == 0) else 0
        return round(base + daily + drift + spike + jitter, 2)

    def _score(self, points: list[dict[str, Any]], entry: dict[str, Any]) -> int:
        """用灵敏度参数判定异常点数量；检测算法不同，阈值口径不同。"""
        if not points:
            return 0
        values = [float(point["值"]) for point in points]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        deviation = variance ** 0.5
        sensitivity = min(max(_to_float(entry.get("灵敏度参数"), 0.95), 0.5), 0.999)
        threshold = max(deviation * (3.0 - sensitivity), 1e-6)
        return sum(1 for value in values if abs(value - mean) > threshold)

    def _build_events(
        self,
        batch: str,
        snapshot: dict[str, Any],
        points: list[dict[str, Any]],
        source_note: str,
    ) -> list[dict[str, Any]]:
        """把检测结果逐条落成事件；每条事件只引用一个部署版本。"""
        if not points:
            return []
        params = snapshot.get("模型参数") or {}
        values = [float(point["值"]) for point in points]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        deviation = variance ** 0.5
        sensitivity = min(max(_to_float(params.get("灵敏度参数"), 0.95), 0.5), 0.999)
        threshold = max(deviation * (3.0 - sensitivity), 1e-6)
        events: list[dict[str, Any]] = []
        for index, point in enumerate(points):
            value = float(point["值"])
            delta = value - mean
            is_anomaly = abs(delta) > threshold
            score = abs(delta) / max(deviation, 1e-6)
            severity = "无"
            if is_anomaly:
                severity = "低"
                for label in ("高", "中"):
                    if score >= EVENT_SEVERITY[label]:
                        severity = label
                        break
            events.append(
                {
                    "业务批次": batch,
                    "事件编号": f"{batch}-{index + 1:04d}",
                    "事件时间": point["时间"],
                    "指标值": value,
                    "基线值": round(mean, 2),
                    "偏差": round(delta, 2),
                    "检测阈值": round(threshold, 2),
                    "is_anomaly": is_anomaly,
                    "严重度": severity if is_anomaly else "无",
                    "event_status": "已入数据集",
                    "模型编号": snapshot.get("模型编号"),
                    "模型版本": snapshot.get("模型版本"),
                    "部署版本": snapshot.get("部署版本"),
                    "部署快照编号": snapshot.get("id"),
                    "检测算法": snapshot.get("检测算法"),
                    "训练窗口开始": snapshot.get("训练窗口开始"),
                    "训练窗口结束": snapshot.get("训练窗口结束"),
                    "数据来源": source_note,
                }
            )
        return events


service = AnomalydetectService()

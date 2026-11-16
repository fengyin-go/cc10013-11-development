"""失败重试业务规则：状态流转、字段校验与筛选口径都收在这里。

重试续做的三条硬约束：
1. 退避倍数、降级端点等快照在登记失败批次时随原始请求固定，续做不再回读策略；
2. 中断后只处理未完成尝试，同一策略版本下同一原始请求最多保留一个成功结果；
3. 批次结束后收到的补报失败先撤销成功、再裁决终态，不允许留下两个成功版本。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "retry"
DATASET_MODULE = "retry_dataset"
ATTEMPT_MODULE = "retry_attempt"
QUEUE_MODULE = "retry_queue"

REQUIRED_FIELDS = ["策略编号", "关联端点", "最大重试"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用策略": "已启用", "停用策略": "已停用"}
NEGATIVE_ACTIONS = ["停用策略"]

# 重试数据集状态：只有「未完成」的两档会被续做触碰，其余一律跳过
DATASET_OPEN_STATUSES = ["待重试", "重试中"]
DATASET_TERMINAL_STATUSES = ["已成功", "已失败"]
DATASET_CONFLICT_STATUS = "冲突待裁决"

# 尝试结果
ATTEMPT_RUNNING = "执行中"
ATTEMPT_SUCCESS = "成功"
ATTEMPT_FAILURE = "失败"
ATTEMPT_INTERRUPTED = "中断"
ATTEMPT_LATE_FAILURE = "补报失败"

# 策略字段缺数时的兜底快照，保证退避边界永远可计算
SNAPSHOT_DEFAULTS = {"退避倍数": 2.0, "初始间隔秒": 5.0, "最大间隔秒": 300.0, "最大重试": 3}

RESUME_OUTCOMES = [ATTEMPT_SUCCESS, ATTEMPT_FAILURE]
ADJUDICATIONS = ["重试", "终止"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _as_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _backoff_boundary(row: dict[str, Any], attempt_no: int) -> float:
    """第 attempt_no 次尝试的固定退避边界：只由随请求固定的快照算出。"""
    base = float(row["初始间隔秒"]) * float(row["退避倍数"]) ** max(attempt_no - 1, 0)
    return round(min(base, float(row["最大间隔秒"])), 3)


def _attempt_endpoint(row: dict[str, Any], attempt_no: int) -> str:
    """首次尝试走原始端点，后续尝试走随请求固定的降级端点（未配置时仍走原始端点）。"""
    fallback = str(row.get("降级端点") or "").strip()
    if attempt_no >= 2 and fallback:
        return fallback
    return str(row.get("关联端点") or "")


def _existing_success(request_code: str, policy_version: int) -> dict[str, Any] | None:
    """同一策略版本下该原始请求是否已有未撤销的成功尝试。"""
    for attempt in store.rows(ATTEMPT_MODULE):
        if (
            str(attempt.get("请求编号")) == request_code
            and _as_int(attempt.get("策略版本"), 0) == policy_version
            and attempt.get("结果") == ATTEMPT_SUCCESS
        ):
            return attempt
    return None


def _attempts_of(request_code: str) -> list[dict[str, Any]]:
    rows = [a for a in store.rows(ATTEMPT_MODULE) if str(a.get("请求编号")) == request_code]
    return sorted(rows, key=lambda a: _as_int(a.get("尝试序号"), 0))


def _find_queue(batch_code: str, request_code: str) -> dict[str, Any] | None:
    for row in store.rows(QUEUE_MODULE):
        if str(row.get("批次编号")) == batch_code and str(row.get("请求编号")) == request_code:
            return row
    return None


def _sync_queue(row: dict[str, Any], result: str | None = None) -> None:
    """把数据集行的最新状态与尝试结果编入失败批次待处理队列。"""
    queue_row = _find_queue(str(row.get("批次编号")), str(row.get("请求编号")))
    if queue_row is None:
        return
    if result is not None:
        queue_row["已编入尝试数"] = _as_int(queue_row.get("已编入尝试数"), 0) + 1
        queue_row["最近尝试结果"] = result
    status_map = {
        "待重试": "待处理",
        "重试中": "重试中",
        "已成功": "已完成",
        "已失败": "已终结",
        DATASET_CONFLICT_STATUS: "待处理",
    }
    queue_row["状态"] = status_map.get(str(row.get("状态")), "待处理")
    queue_row["更新时间"] = _now()


def _snapshot_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """登记失败批次时把退避倍数、降级端点等随原始请求固定下来。"""
    return {
        "策略编号": str(policy.get("策略编号") or ""),
        "策略版本": _as_int(policy.get("策略版本"), 1),
        "退避倍数": _as_float(policy.get("退避倍数"), SNAPSHOT_DEFAULTS["退避倍数"]),
        "初始间隔秒": _as_float(policy.get("初始间隔秒"), SNAPSHOT_DEFAULTS["初始间隔秒"]),
        "最大间隔秒": _as_float(policy.get("最大间隔秒"), SNAPSHOT_DEFAULTS["最大间隔秒"]),
        "最大重试": max(1, _as_int(policy.get("最大重试"), SNAPSHOT_DEFAULTS["最大重试"])),
        "降级端点": str(policy.get("降级端点") or "").strip(),
    }


class RetryService:
    # ------------------------------------------------------------------
    # 重试策略（原有能力）
    # ------------------------------------------------------------------
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
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
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
        rows = store.rows(MODULE)
        entry = {"id": _next_id(rows)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ["退避倍数", "初始间隔秒", "最大间隔秒", "重试条件", "降级端点"]:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry["策略版本"] = 1
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"重试策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于失败重试可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        if action == "启用策略":
            # 每次启用都是一个新策略版本，旧版本下已成功的请求不受影晌
            entry["策略版本"] = _as_int(entry.get("策略版本"), 1) + 1
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"重试策略已{action}"

    # ------------------------------------------------------------------
    # 失败批次登记：建立重试数据集，未完成请求写入待处理队列
    # ------------------------------------------------------------------
    def register_batch(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        batch_code = str(values.get("批次编号") or "").strip()
        endpoint = str(values.get("关联端点") or "").strip()
        policy_code = str(values.get("策略编号") or "").strip()
        raw_requests = values.get("请求列表")
        if isinstance(raw_requests, str):
            requests = [item.strip() for item in raw_requests.replace("，", ",").split(",") if item.strip()]
        elif isinstance(raw_requests, list):
            requests = [str(item).strip() for item in raw_requests if str(item).strip()]
        else:
            requests = []
        missing = []
        if not batch_code:
            missing.append("批次编号")
        if not endpoint:
            missing.append("关联端点")
        if not policy_code:
            missing.append("策略编号")
        if not requests:
            missing.append("请求列表")
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"

        policy = next(
            (row for row in store.rows(MODULE) if str(row.get("策略编号")) == policy_code),
            None,
        )
        if policy is None:
            return None, f"重试策略 {policy_code} 不存在，无法固定退避快照"
        snapshot = _snapshot_policy(policy)

        dataset = store.rows(DATASET_MODULE)
        queue = store.rows(QUEUE_MODULE)
        created, duplicated, deduped = 0, 0, 0
        for request_code in dict.fromkeys(requests):
            if any(
                str(row.get("批次编号")) == batch_code and str(row.get("请求编号")) == request_code
                for row in dataset
            ):
                duplicated += 1
                continue
            row = {
                "id": _next_id(dataset),
                "请求编号": request_code,
                "批次编号": batch_code,
                "关联端点": endpoint,
                **snapshot,
                "状态": "待重试",
                "已尝试次数": 0,
                "成功尝试号": None,
                "下次间隔秒": None,
                "创建时间": _now(),
                "更新时间": _now(),
            }
            # 同一策略版本下已有成功结果的请求直接复用，不再发向上游
            success = _existing_success(request_code, snapshot["策略版本"])
            if success is not None:
                row["状态"] = "已成功"
                row["成功尝试号"] = success.get("尝试序号")
                deduped += 1
            else:
                row["下次间隔秒"] = _backoff_boundary(row, 1)
                created += 1
            dataset.append(row)
            queue.append({
                "id": _next_id(queue),
                "批次编号": batch_code,
                "请求编号": request_code,
                "关联端点": endpoint,
                "策略编号": snapshot["策略编号"],
                "策略版本": snapshot["策略版本"],
                "状态": "已完成" if success is not None else "待处理",
                "已编入尝试数": 0,
                "最近尝试结果": "—",
                "入队时间": _now(),
                "更新时间": _now(),
            })
        summary = {
            "批次编号": batch_code,
            "策略版本": snapshot["策略版本"],
            "新建待重试": created,
            "复用已成功": deduped,
            "重复跳过": duplicated,
        }
        return summary, (
            f"失败批次 {batch_code} 已登记：新建待重试 {created} 条，"
            f"复用已有成功 {deduped} 条，重复请求跳过 {duplicated} 条"
        )

    # ------------------------------------------------------------------
    # 重试数据集
    # ------------------------------------------------------------------
    def list_dataset(
        self,
        *,
        keyword: str | None = None,
        batch: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(DATASET_MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("请求编号", ""))]
        if batch:
            rows = [row for row in rows if str(row.get("批次编号")) == batch]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_dataset_detail(self, dataset_id: int) -> dict[str, Any] | None:
        """重试详情：数据集行串起完整尝试序列（执行序列）。"""
        row = store.find(DATASET_MODULE, dataset_id)
        if row is None:
            return None
        detail = dict(row)
        detail["尝试序列"] = _attempts_of(str(row.get("请求编号")))
        queue_row = _find_queue(str(row.get("批次编号")), str(row.get("请求编号")))
        detail["队列状态"] = queue_row.get("状态") if queue_row else None
        return detail

    # ------------------------------------------------------------------
    # 续做：中断后只处理未完成尝试
    # ------------------------------------------------------------------
    def _close_dangling(self, request_code: str) -> int:
        """进程中断残留的「执行中」尝试先关闭为中断，槽位在建尝试时已消耗。"""
        closed = 0
        for attempt in _attempts_of(request_code):
            if attempt.get("结果") == ATTEMPT_RUNNING:
                attempt["结果"] = ATTEMPT_INTERRUPTED
                attempt["记录时间"] = _now()
                closed += 1
        return closed

    def _resume_row(
        self,
        row: dict[str, Any],
        outcome: str,
        interrupted: bool,
    ) -> str:
        request_code = str(row.get("请求编号"))
        version = _as_int(row.get("策略版本"), 1)

        # 成功守卫：同一策略版本下已存在成功结果，绝不产生第二个
        success = _existing_success(request_code, version)
        if success is not None:
            row["状态"] = "已成功"
            row["成功尝试号"] = success.get("尝试序号")
            row["下次间隔秒"] = None
            row["更新时间"] = _now()
            _sync_queue(row)
            return f"{request_code} 已存在成功结果（尝试{success.get('尝试序号')}），不再重发"

        closed = self._close_dangling(request_code)
        for _ in range(closed):
            _sync_queue(row, ATTEMPT_INTERRUPTED)

        max_retry = _as_int(row.get("最大重试"), SNAPSHOT_DEFAULTS["最大重试"])
        used = _as_int(row.get("已尝试次数"), 0)
        if used >= max_retry:
            row["状态"] = "已失败"
            row["下次间隔秒"] = None
            row["更新时间"] = _now()
            _sync_queue(row)
            return f"{request_code} 重试额度已用尽，置为终态失败"

        attempt_no = used + 1
        attempt = {
            "id": _next_id(store.rows(ATTEMPT_MODULE)),
            "请求编号": request_code,
            "批次编号": row.get("批次编号"),
            "尝试序号": attempt_no,
            "策略版本": version,
            "使用端点": _attempt_endpoint(row, attempt_no),
            "间隔秒": _backoff_boundary(row, attempt_no),
            "结果": ATTEMPT_RUNNING if interrupted else outcome,
            "记录时间": _now(),
        }
        store.rows(ATTEMPT_MODULE).append(attempt)
        row["已尝试次数"] = attempt_no

        if interrupted:
            # 模拟进程中断：尝试悬在执行中，下次续做只补这条未完成尝试
            row["状态"] = "重试中"
            row["下次间隔秒"] = _backoff_boundary(row, attempt_no + 1) if attempt_no < max_retry else None
            row["更新时间"] = _now()
            _sync_queue(row)
            return f"{request_code} 第 {attempt_no} 次尝试中断，等待续做"

        if outcome == ATTEMPT_SUCCESS:
            row["状态"] = "已成功"
            row["成功尝试号"] = attempt_no
            row["下次间隔秒"] = None
        else:
            if attempt_no >= max_retry:
                row["状态"] = "已失败"
                row["下次间隔秒"] = None
            else:
                row["状态"] = "重试中"
                row["下次间隔秒"] = _backoff_boundary(row, attempt_no + 1)
        row["更新时间"] = _now()
        _sync_queue(row, outcome)
        return f"{request_code} 第 {attempt_no} 次尝试{outcome}"

    def resume_request(
        self,
        dataset_id: int,
        outcome: str,
        interrupted: bool,
    ) -> tuple[dict[str, Any] | None, str]:
        row = store.find(DATASET_MODULE, dataset_id)
        if row is None:
            return None, f"重试数据集记录 {dataset_id} 不存在"
        status = str(row.get("状态"))
        if status in DATASET_TERMINAL_STATUSES:
            return row, f"{row.get('请求编号')} 已是终态「{status}」，不再重发上游"
        if status == DATASET_CONFLICT_STATUS:
            return None, f"{row.get('请求编号')} 存在补报冲突，需先裁决再续做"
        if outcome not in RESUME_OUTCOMES and not interrupted:
            return None, f"续做结果「{outcome}」不在允许范围（{'、'.join(RESUME_OUTCOMES)}）"
        message = self._resume_row(row, outcome, interrupted)
        return self.get_dataset_detail(dataset_id), message

    def resume_batch(
        self,
        batch_code: str,
        outcome: str,
        interrupted: bool,
    ) -> tuple[dict[str, Any] | None, str]:
        rows = [
            row for row in store.rows(DATASET_MODULE)
            if str(row.get("批次编号")) == batch_code
        ]
        if not rows:
            return None, f"失败批次 {batch_code} 不在重试数据集里"
        if outcome not in RESUME_OUTCOMES and not interrupted:
            return None, f"续做结果「{outcome}」不在允许范围（{'、'.join(RESUME_OUTCOMES)}）"
        done, skipped = 0, 0
        for row in rows:
            if str(row.get("状态")) in DATASET_OPEN_STATUSES:
                self._resume_row(row, outcome, interrupted)
                done += 1
            else:
                skipped += 1
        summary = {"批次编号": batch_code, "本次续做": done, "跳过终态": skipped}
        return summary, f"批次 {batch_code} 续做完成：处理未完成 {done} 条，跳过终态 {skipped} 条"

    # ------------------------------------------------------------------
    # 补报与裁决：批次结束后又收到补报失败，终态取舍由裁决落地
    # ------------------------------------------------------------------
    def report_late_failure(self, dataset_id: int) -> tuple[dict[str, Any] | None, str]:
        row = store.find(DATASET_MODULE, dataset_id)
        if row is None:
            return None, f"重试数据集记录 {dataset_id} 不存在"
        success_no = row.get("成功尝试号")
        if success_no is None:
            return None, f"{row.get('请求编号')} 没有已记录的成功尝试，补报不成立"
        attempt = next(
            (
                a for a in _attempts_of(str(row.get("请求编号")))
                if _as_int(a.get("尝试序号"), 0) == _as_int(success_no, -1)
                and a.get("结果") == ATTEMPT_SUCCESS
            ),
            None,
        )
        if attempt is None:
            return None, f"{row.get('请求编号')} 的成功尝试已被撤销，无需补报"
        # 先撤销唯一的成功版本，再挂起等待裁决，任何时刻都不留两个成功版本
        attempt["结果"] = ATTEMPT_LATE_FAILURE
        attempt["记录时间"] = _now()
        row["成功尝试号"] = None
        row["状态"] = DATASET_CONFLICT_STATUS
        row["更新时间"] = _now()
        _sync_queue(row, ATTEMPT_LATE_FAILURE)
        return (
            self.get_dataset_detail(dataset_id),
            f"{row.get('请求编号')} 收到补报失败，成功结果已撤销，等待裁决终态",
        )

    def adjudicate(self, dataset_id: int, decision: str) -> tuple[dict[str, Any] | None, str]:
        row = store.find(DATASET_MODULE, dataset_id)
        if row is None:
            return None, f"重试数据集记录 {dataset_id} 不存在"
        if str(row.get("状态")) != DATASET_CONFLICT_STATUS:
            return None, f"{row.get('请求编号')} 不在冲突待裁决状态，无需裁决"
        if decision not in ADJUDICATIONS:
            return None, f"裁决「{decision}」不在允许范围（{'、'.join(ADJUDICATIONS)}）"
        request_code = str(row.get("请求编号"))
        version = _as_int(row.get("策略版本"), 1)
        # 裁决前再校验一次：同一策略版本下不允许残留第二个成功版本
        success = _existing_success(request_code, version)
        if success is not None:
            row["状态"] = "已成功"
            row["成功尝试号"] = success.get("尝试序号")
            row["下次间隔秒"] = None
            row["更新时间"] = _now()
            _sync_queue(row)
            return self.get_dataset_detail(dataset_id), (
                f"{request_code} 已存在成功结果（尝试{success.get('尝试序号')}），冲突自动按成功收口"
            )
        max_retry = _as_int(row.get("最大重试"), SNAPSHOT_DEFAULTS["最大重试"])
        used = _as_int(row.get("已尝试次数"), 0)
        if decision == "重试" and used < max_retry:
            row["状态"] = "待重试"
            row["下次间隔秒"] = _backoff_boundary(row, used + 1)
            message = f"{request_code} 裁定为重试，回到待处理队列"
        else:
            row["状态"] = "已失败"
            row["下次间隔秒"] = None
            message = (
                f"{request_code} 裁定为终止，置为终态失败"
                if decision == "终止"
                else f"{request_code} 重试额度已用尽，裁定为终态失败"
            )
        row["更新时间"] = _now()
        _sync_queue(row)
        return self.get_dataset_detail(dataset_id), message

    # ------------------------------------------------------------------
    # 待处理队列与批次汇总
    # ------------------------------------------------------------------
    def list_queue(
        self,
        *,
        batch: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(QUEUE_MODULE)
        if batch:
            rows = [row for row in rows if str(row.get("批次编号")) == batch]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def list_batches(self) -> list[dict[str, Any]]:
        """批次汇总：状态由数据集推导，残留未完成请求的批次绝不会显示成成功。"""
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in store.rows(DATASET_MODULE):
            groups.setdefault(str(row.get("批次编号")), []).append(row)
        summaries = []
        for batch_code, rows in groups.items():
            success = sum(1 for row in rows if row.get("状态") == "已成功")
            failed = sum(1 for row in rows if row.get("状态") == "已失败")
            conflict = sum(1 for row in rows if row.get("状态") == DATASET_CONFLICT_STATUS)
            open_count = sum(1 for row in rows if row.get("状态") in DATASET_OPEN_STATUSES)
            attempted = any(_as_int(row.get("已尝试次数"), 0) > 0 for row in rows)
            incomplete = open_count + conflict
            if incomplete == 0:
                status = "已完结"
            elif attempted or success or failed:
                status = "处理中"
            else:
                status = "待处理"
            summaries.append({
                "批次编号": batch_code,
                "请求数": len(rows),
                "已成功": success,
                "未完成": open_count,
                "冲突待裁决": conflict,
                "已失败": failed,
                "状态": status,
                "全部成功": incomplete == 0 and success == len(rows),
            })
        return sorted(summaries, key=lambda item: str(item["批次编号"]))

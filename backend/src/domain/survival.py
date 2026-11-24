"""按已借天数估算未还工具未来七天归还概率，并逐月回测。

口径要点：
- 估计只使用评估时点当批已收且已发生的事件；交接簿同笔事件只算一次
  （去重与顺序号冲突校验见本模块 ``_ordered_events``，台账回放同样复用）。
- 一笔借期 = 一次借出到下一次归还；退役或观察到评估点仍未归还的借期右删失，
  未还借期的已借天数只观察到评估点为止。
- 按已借天数（借出自第 0 天起）构建 Kaplan–Meier 生命表，
  条件七天归还概率 = 1 - S(d + 7) / S(d)。
- 回测答案只读取同一批次内的后续事件：晚到记录补录为新批次后另跑即可成为答案；
  七天窗口未被批次数据覆盖完全的借期判为「待观察」。
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

from .spec import SPEC


def _day(value, label):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}须为有效日期。") from None


_FIELD_BY_KEY = {field["key"]: field for field in SPEC["fields"]}


@dataclass(frozen=True)
class _Event:
    event_id: str
    asset_id: str
    asset_name: str | None
    event_date: date
    sequence: int
    event_type: str


def _event_text(row, key, index, *, required=True):
    field = _FIELD_BY_KEY[key]
    raw = row.get(key)
    if raw is None and not required:
        return None
    if isinstance(raw, str):
        value = raw.strip()
        if not value and not required:
            return None
        if value and len(value) <= field["max_length"]:
            return value
    raise ValueError(f"第 {index} 行的{field['label']}须为 1 至 {field['max_length']} 个字符。")


def _ordered_events(rows):
    """校验并按事件编号去重，同编号冲突或同日顺序号重复时报错；返回业务序事件。"""
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("交接明细为空，请导入至少一条生命周期事件。")
    by_id = {}
    by_slot = {}
    sequence_field = _FIELD_BY_KEY["sequence"]
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 行交接事件格式有误。")
        kind = row.get("event_type")
        if kind not in _FIELD_BY_KEY["event_type"]["options"]:
            raise ValueError(f"第 {index} 行事件类型无效，须为 accept、check-out、check-in 或 retire。")
        sequence = row.get("sequence")
        if type(sequence) is not int or not sequence_field["min"] <= sequence <= sequence_field["max"]:
            raise ValueError(f"第 {index} 行顺序号须为 1 至 {sequence_field['max']} 的整数。")
        event = _Event(
            _event_text(row, "event_id", index), _event_text(row, "asset_id", index),
            _event_text(row, "asset_name", index, required=kind == "accept"),
            _day(row.get("event_date"), f"第 {index} 行交接日期"), sequence, kind,
        )
        previous = by_id.get(event.event_id)
        if previous == event:
            continue
        if previous is not None:
            raise ValueError(f"事件编号 {event.event_id} 的内容冲突，请核对原始交接单。")
        slot = (event.asset_id, event.event_date, event.sequence)
        if slot in by_slot:
            raise ValueError(f"工具 {event.asset_id} 在 {event.event_date} 的顺序号 {sequence} 重复"
                             f"（{by_slot[slot]}、{event.event_id}）。")
        by_slot[slot] = event.event_id
        by_id[event.event_id] = event
    return sorted(by_id.values(), key=lambda event: (event.event_date, event.asset_id, event.sequence))

_RETURNED = "returned"
_NOT_RETURNED = "not_returned"
_PENDING = "pending"
_OUTCOME_LABELS = {_RETURNED: "已归还", _NOT_RETURNED: "未归还", _PENDING: "待观察"}

_OUTSTANDING_COLUMNS = (
    ("eval_date", "评估日"), ("asset_id", "工具编号"), ("asset_name", "工具名称"),
    ("loan_event_id", "借出事件编号"), ("loan_date", "借出日"), ("elapsed_days", "已借天数"),
    ("return_prob_7d_pct", "未来七天归还概率（%）"), ("probability_note", "概率说明"),
    ("actual_outcome", "实际归还"), ("outcome_event_id", "答案事件编号"),
    ("outcome_date", "答案事件日"), ("window_end", "观察窗口截止"),
    ("observation_status", "观察状态"),
)
_LIFE_COLUMNS = (
    ("elapsed_days", "已借天数"), ("at_risk", "风险集件数"), ("returned", "归还件数"),
    ("censored", "删失件数"), ("survival", "持续未还概率"),
)
_SUMMARY_COLUMNS = (
    ("eval_date", "评估日"), ("outstanding_count", "未还件数"),
    ("predicted_count", "已出概率件数"), ("observed_count", "答案可判件数"),
    ("returned_count", "窗口内归还"), ("not_returned_count", "窗口满未还"),
    ("pending_count", "待观察"), ("observed_return_rate_pct", "观察到的七天归还率（%）"),
    ("spells_used", "估计用借期数"),
)


@dataclass(frozen=True)
class LoanSpell:
    asset_id: str
    asset_name: str | None
    loan_event_id: str
    loan_date: date
    end_date: date | None
    end_event_id: str | None
    end_reason: str  # check-in / retire / censor


def build_spells(events):
    """从去重事件还原每笔借期；未闭合的借期以最后一笔事件日右删失。"""
    open_loans = {}
    spells = []
    last_name = {}
    last_seen = {}
    for event in events:
        last_seen[event.asset_id] = event.event_date
        if event.asset_name:
            last_name[event.asset_id] = event.asset_name
        if event.event_type == "check-out":
            if event.asset_id in open_loans:
                raise ValueError(
                    f"工具 {event.asset_id} 的借出事件 {event.event_id} 缺少对应归还，借期未闭合。")
            open_loans[event.asset_id] = event
        elif event.event_type in {"check-in", "retire"}:
            open_loan = open_loans.pop(event.asset_id, None)
            if open_loan is not None:
                spells.append(LoanSpell(
                    event.asset_id, last_name.get(event.asset_id), open_loan.event_id,
                    open_loan.event_date, event.event_date, event.event_id, event.event_type))
    for asset_id, open_loan in open_loans.items():
        spells.append(LoanSpell(
            asset_id, last_name.get(asset_id), open_loan.event_id,
            open_loan.event_date, last_seen[asset_id], None, "censor"))
    return spells


def life_table(events, eval_date, horizon_days):
    """用 eval_date 当天及之前已发生的借期，按已借天数构建 Kaplan–Meier 生命表。

    ``horizon_days`` 由调用方用于条件概率查询，生命表本身覆盖全部观察天数。
    """
    visible = [event for event in events if event.event_date <= eval_date]
    spells = build_spells(visible)
    max_day = 0
    timeline = []
    for spell in spells:
        end = spell.end_date or eval_date
        duration = (end - spell.loan_date).days
        timeline.append((duration, spell.end_reason))
        max_day = max(max_day, duration)
    rows = []
    survival = 1.0
    for day in range(max_day + 1):
        at_risk = sum(1 for duration, _ in timeline if duration >= day)
        returned = sum(1 for duration, reason in timeline if duration == day and reason == "check-in")
        censored = sum(1 for duration, reason in timeline if duration == day and reason != "check-in")
        if at_risk:
            survival *= (at_risk - returned) / at_risk
        rows.append({
            "elapsed_days": day, "at_risk": at_risk, "returned": returned,
            "censored": censored, "survival": round(survival, 6),
        })
    return {"rows": rows, "spells": spells, "spell_count": len(spells),
            "max_duration_days": max_day}


def _survival_at(table_rows, day):
    """取 S(day)；超过最大观察天数时沿用最后一个可得估计。"""
    if day < 0 or not table_rows:
        return None
    for row in table_rows:
        if row["elapsed_days"] == day:
            return row["survival"]
        if row["elapsed_days"] > day:
            break
    previous = [row for row in table_rows if row["elapsed_days"] <= day]
    return previous[-1]["survival"] if previous else None


def return_probability(table_rows, elapsed_days, horizon_days, min_sample):
    """条件概率 1 - S(d+h)/S(d)；风险集不足时不给概率。"""
    base = _survival_at(table_rows, elapsed_days)
    if base is None or base <= 0:
        return None, "该已借天数缺少可参照的存活估计，暂不出概率"
    at_risk_here = next((row["at_risk"] for row in table_rows
                         if row["elapsed_days"] == elapsed_days), 0)
    if at_risk_here < min_sample:
        return None, f"该已借天数风险集仅 {at_risk_here} 件，低于最小样本 {min_sample} 件"
    later = _survival_at(table_rows, elapsed_days + horizon_days)
    if later is None:
        return None, "观察历史不足，无法覆盖未来七天的存活估计"
    probability = max(0.0, min(1.0, 1.0 - later / base))
    return round(probability, 6), ""


def _evaluation_dates(start, cutoff):
    dates = []
    cursor = start.replace(day=1)
    while True:
        month_end = cursor.replace(day=monthrange(cursor.year, cursor.month)[1])
        point = min(month_end, cutoff)
        if point >= start and point not in dates:
            dates.append(point)
        if month_end >= cutoff:
            break
        cursor = (month_end + timedelta(days=1)).replace(day=1)
    return dates


def _asset_name(events, asset_id):
    for event in events:
        if event.asset_id == asset_id and event.asset_name:
            return event.asset_name
    return None


def backtest(rows, normalized_params):
    """生成逐月回测：未还借期清单、生命表与月度汇总。"""
    horizon = int(normalized_params.get("horizon_days", 7))
    min_sample = int(normalized_params.get("min_sample", 3))
    if horizon <= 0:
        raise ValueError("归还观察窗口须为正整数天。")
    if min_sample <= 0:
        raise ValueError("最小风险集样本须为正整数。")
    start = _day(normalized_params["start_date"], "统计首日")
    cutoff = _day(normalized_params["as_of_date"], "盘点截止日")
    if start > cutoff:
        raise ValueError("统计首日须早于或等于盘点截止日。")
    events = _ordered_events(rows)
    data_through = max((event.event_date for event in events), default=None)

    outstanding_rows = []
    life_tables = []
    summary_rows = []
    for eval_date in _evaluation_dates(start, cutoff):
        table = life_table(events, eval_date, horizon)
        table_rows = table["rows"]
        visible = [event for event in events if event.event_date <= eval_date]
        spells = build_spells(visible)
        window_end = eval_date + timedelta(days=horizon)
        future = [event for event in events
                  if eval_date < event.event_date <= window_end]

        month_outstanding = []
        returned_count = not_returned_count = pending_count = predicted_count = 0
        for spell in spells:
            if spell.end_reason != "censor":
                continue
            elapsed = (eval_date - spell.loan_date).days
            probability, note = return_probability(table_rows, elapsed, horizon, min_sample)
            if probability is not None:
                predicted_count += 1
            closing = next((event for event in future
                            if event.asset_id == spell.asset_id
                            and event.event_type in {"check-in", "retire"}), None)
            if closing is not None and closing.event_type == "check-in":
                outcome, status, outcome_id, outcome_date = (
                    _RETURNED, "答案可判", closing.event_id, closing.event_date.isoformat())
                returned_count += 1
            elif data_through is not None and data_through >= window_end:
                outcome, status, outcome_id, outcome_date = (
                    _NOT_RETURNED, "答案可判", None, None)
                not_returned_count += 1
            else:
                outcome, status, outcome_id, outcome_date = _PENDING, "待观察", None, None
                pending_count += 1
            row = {
                "eval_date": eval_date.isoformat(),
                "asset_id": spell.asset_id,
                "asset_name": spell.asset_name or _asset_name(events, spell.asset_id),
                "loan_event_id": spell.loan_event_id,
                "loan_date": spell.loan_date.isoformat(),
                "elapsed_days": elapsed,
                "return_prob_7d_pct": round(probability * 100, 4) if probability is not None else None,
                "probability_note": note,
                "actual_outcome": _OUTCOME_LABELS[outcome],
                "outcome_event_id": outcome_id,
                "outcome_date": outcome_date,
                "window_end": window_end.isoformat(),
                "observation_status": status,
            }
            month_outstanding.append(row)
            outstanding_rows.append(row)

        observed = returned_count + not_returned_count
        summary_rows.append({
            "eval_date": eval_date.isoformat(),
            "outstanding_count": len(month_outstanding),
            "predicted_count": predicted_count,
            "observed_count": observed,
            "returned_count": returned_count,
            "not_returned_count": not_returned_count,
            "pending_count": pending_count,
            "observed_return_rate_pct": round(100 * returned_count / observed, 4) if observed else None,
            "spells_used": table["spell_count"],
        })
        life_tables.append({
            "eval_date": eval_date.isoformat(),
            "rows": table_rows,
            "spell_count": table["spell_count"],
            "max_duration_days": table["max_duration_days"],
        })

    observed_total = sum(row["observed_count"] for row in summary_rows)
    returned_total = sum(row["returned_count"] for row in summary_rows)
    return {
        "horizon_days": horizon,
        "min_sample": min_sample,
        "data_through": data_through.isoformat() if data_through else None,
        "metrics": [
            {"label": "回测评估点", "value": len(summary_rows), "detail": "自然月末及截止日快照"},
            {"label": "累计未还借期观察", "value": len(outstanding_rows),
             "detail": "各评估点仍未归还的借出件次"},
            {"label": "待观察项", "value": sum(row["pending_count"] for row in summary_rows),
             "detail": "七天窗口未被当前批次覆盖完全"},
            {"label": "观察到的七天归还率（%）",
             "value": round(100 * returned_total / observed_total, 4) if observed_total else None,
             "detail": f"{returned_total} / {observed_total}" if observed_total else "暂无可判答案"},
        ],
        "tables": [
            _table("逐月未还工具归还概率回测", _OUTSTANDING_COLUMNS, outstanding_rows),
            _table("回测月度汇总", _SUMMARY_COLUMNS, summary_rows),
            *[_table(f"生命表（评估日 {item['eval_date']}）", _LIFE_COLUMNS, item["rows"])
              for item in life_tables],
        ],
        "life_tables": life_tables,
        "sections": [
            {"title": "归还概率估计与回测口径", "text": (
                "估计仅使用评估日当批已收且已发生的事件，交接簿同笔事件只回放一次；"
                "未还借期按已借天数右删失，以 Kaplan–Meier 生命表给出条件七天归还概率，"
                "风险集低于最小样本时不出概率。回测答案取同一批次内评估日后七天的归还事件；"
                "窗口已满且未归还判为未归还，窗口未被批次覆盖的判为待观察。"
                "晚到记录补录为新批次后另跑，即可补齐答案，历史运行不被改写。"
            )},
        ],
    }


def _table(title, columns, rows):
    return {"title": title,
            "columns": [{"key": key, "label": label} for key, label in columns],
            "rows": rows}

"""按日终回放交接事件，计算快照和区间勾稽。"""

from calendar import monthrange
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import timedelta

from . import survival
from .spec import SPEC


_TRANSITIONS = {
    (None, "accept"): "in_custody",
    ("in_custody", "check-out"): "checked_out",
    ("checked_out", "check-in"): "in_custody",
    ("in_custody", "retire"): "retired",
}
_STATUS_LABELS = {None: "未接收", "in_custody": "在管", "checked_out": "借出", "retired": "退役"}
_EVENT_LABELS = {"accept": "接收", "check-out": "借出", "check-in": "归还", "retire": "退役"}
_PARAM_DEFAULTS = {field["key"]: field["default"] for field in SPEC["params"]}
_BALANCE_COLUMNS = (
    ("start_date", "统计首日"), ("as_of_date", "截止日"), ("close_type", "快照类型"),
    ("opening_active", "期初有效"), ("opening_in_custody", "期初在管"),
    ("opening_checked_out", "期初借出"), ("accepted", "接收"), ("checked_out", "借出"),
    ("checked_in", "归还"), ("retired", "退役"), ("closing_active", "期末有效"),
    ("closing_in_custody", "期末在管"), ("closing_checked_out", "期末借出"),
    ("in_custody_rate_pct", "期末在管率（%）"), ("active_difference", "有效勾稽差额"),
    ("custody_difference", "在管勾稽差额"), ("checked_out_difference", "借出勾稽差额"),
)
_ASSET_COLUMNS = (
    ("asset_id", "工具编号"), ("asset_name", "工具名称"), ("status", "期末状态"),
    ("last_event_date", "末笔交接日"), ("last_sequence", "末笔顺序号"),
    ("last_event_type", "末笔交接类型"), ("last_event_id", "末笔事件编号"),
)


@dataclass(frozen=True)
class _Asset:
    name: str
    status: str
    last_event: survival._Event


def _day(value, label):
    return survival._day(value, label)


def _ordered_events(rows):
    return survival._ordered_events(rows)


def validate_rows(rows):
    """校验事件格式与重放键，返回去重后的规范事件；状态由盘点截止日决定。"""
    return [dict(asdict(event), event_date=event.event_date.isoformat())
            for event in survival._ordered_events(rows)]


class _Ledger:
    def __init__(self, events):
        self.assets = {}
        self.counts = Counter()
        self._events = iter(events)
        self._next = next(self._events, None)

    def advance(self, boundary, *, inclusive=True):
        movements = Counter()
        while (event := self._next) is not None:
            if event.event_date > boundary or (event.event_date == boundary and not inclusive):
                break
            asset = self.assets.get(event.asset_id)
            prior = asset.status if asset else None
            status = _TRANSITIONS.get((prior, event.event_type))
            if status is None:
                raise ValueError(f"事件 {event.event_id}（工具 {event.asset_id}，{event.event_date}）"
                                 f"无法执行{_EVENT_LABELS[event.event_type]}，当前状态为{_STATUS_LABELS[prior]}。")
            if asset:
                self.counts[prior] -= 1
            name = asset.name if asset else event.asset_name
            self.assets[event.asset_id] = _Asset(name, status, event)
            self.counts[status] += 1
            movements[event.event_type] += 1
            self._next = next(self._events, None)
        return movements

    def balance(self):
        in_custody = self.counts["in_custody"]
        checked_out = self.counts["checked_out"]
        return in_custody + checked_out, in_custody, checked_out


def _balance_row(start, close, close_type, opening, closing, movements):
    opening_active, opening_custody, opening_out = opening
    active, custody, out = closing
    accepted = movements["accept"]
    checked_out = movements["check-out"]
    checked_in = movements["check-in"]
    retired = movements["retire"]
    return {
        "start_date": start.isoformat(), "as_of_date": close.isoformat(), "close_type": close_type,
        "opening_active": opening_active, "opening_in_custody": opening_custody, "opening_checked_out": opening_out,
        "accepted": accepted, "checked_out": checked_out, "checked_in": checked_in, "retired": retired,
        "closing_active": active, "closing_in_custody": custody, "closing_checked_out": out,
        "in_custody_rate_pct": round(100 * custody / active, 4) if active else None,
        "active_difference": active - (opening_active + accepted - retired),
        "custody_difference": custody - (opening_custody + accepted + checked_in - checked_out - retired),
        "checked_out_difference": out - (opening_out + checked_out - checked_in),
    }


def _monthly_balances(ledger, start, cutoff):
    monthly = []
    totals = Counter()
    while True:
        month_end = start.replace(day=monthrange(start.year, start.month)[1])
        close = min(month_end, cutoff)
        opening = ledger.balance()
        movements = ledger.advance(close)
        totals.update(movements)
        close_type = "自然月末" if close == month_end else "截止日"
        monthly.append(_balance_row(start, close, close_type, opening, ledger.balance(), movements))
        if close == cutoff:
            return monthly, totals
        start = close + timedelta(days=1)


def _table(title, columns, rows):
    return {"title": title, "columns": [{"key": key, "label": label} for key, label in columns], "rows": rows}


def analyze(rows, normalized_params):
    """生成日终资产明细、逐月勾稽及整段期初期末。"""
    if not isinstance(normalized_params, dict) or normalized_params.keys() - _PARAM_DEFAULTS.keys():
        raise ValueError("盘点参数格式有误，请核对统计首日和盘点截止日。")
    params = {**_PARAM_DEFAULTS, **normalized_params}
    start = _day(params["start_date"], "统计首日")
    cutoff = _day(params["as_of_date"], "盘点截止日")
    if start > cutoff:
        raise ValueError("统计首日须早于或等于盘点截止日。")

    ledger = _Ledger(_ordered_events(rows))
    ledger.advance(start, inclusive=False)
    opening = ledger.balance()
    monthly, totals = _monthly_balances(ledger, start, cutoff)
    closing = ledger.balance()
    period = _balance_row(start, cutoff, "区间期末", opening, closing, totals)
    active, custody, out = closing
    assets = [
        {"asset_id": asset_id, "asset_name": asset.name, "status": _STATUS_LABELS[asset.status],
         "last_event_date": asset.last_event.event_date.isoformat(), "last_sequence": asset.last_event.sequence,
         "last_event_type": _EVENT_LABELS[asset.last_event.event_type], "last_event_id": asset.last_event.event_id}
        for asset_id, asset in sorted(ledger.assets.items())
    ]
    reconciled = all(row[key] == 0 for row in [period, *monthly]
                     for key in ("active_difference", "custody_difference", "checked_out_difference"))
    backtest = survival.backtest(rows, params)
    return {
        "state": "ok" if active and reconciled else "warning",
        "headline": f"截至 {cutoff}，有效工具 {active} 件，在管 {custody} 件，借出 {out} 件。",
        "note": f"统计期间为 {start} 至 {cutoff}，含首尾两日。期末取截止日日终，流量按事件发生日归属。",
        "metrics": [
            {"label": "期末有效资产", "value": active, "detail": "在管与借出工具之和"},
            {"label": "期末在管", "value": custody, "detail": "已接收或已归还且仍在站内的工具"},
            {"label": "期末借出", "value": out, "detail": "截至期末仍未归还"},
            {"label": "期末在管率（%）", "value": period["in_custody_rate_pct"],
             "detail": f"{custody} / {active}" if active else "有效资产为 0，在管率留空"},
        ],
        "tables": [
            _table("区间期初期末勾稽", _BALANCE_COLUMNS, [period]),
            _table("月末与截止日勾稽", _BALANCE_COLUMNS, monthly),
            _table("期末资产快照", _ASSET_COLUMNS, assets),
            *backtest["tables"],
        ],
        "charts": [
            {"title": title, "kind": "line",
             "series": [{"label": row["as_of_date"], "value": row[key]} for row in monthly]}
            for title, key in (("各盘点日有效资产（件）", "closing_active"), ("各盘点日在管工具（件）", "closing_in_custody"))
        ],
        "sections": [{"title": "勾稽与盘点复核", "text": (
            ("各期有效、在管及借出数量的勾稽差额均为 0。" if reconciled else "存在勾稽差额，请核对交接事件。")
            + "逐月期末存量与比率按截止日取值。复核时可从期末资产快照定位末笔交接单。"
        )}, *backtest["sections"]],
        "backtest": {key: value for key, value in backtest.items()
                     if key not in ("tables", "sections")},
    }

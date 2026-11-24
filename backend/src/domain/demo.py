"""桂桥社区交接簿，含期初结转、跨月借还、退役、重复上传和多笔已闭合借期。

前 16 个业务事件与初始台账一致，月末有效资产与在管数量不变；
其后补充 7 至 11 月的短借短还历史，供归还概率生命表积累风险集。
"""


_EVENTS = (
    ("GQ-01", "充电电钻", "2026-08-28", 1, "accept"),
    ("GQ-02", "数字万用表", "2026-08-31", 1, "accept"),
    ("GQ-03", "园艺修枝剪", "2026-09-03", 1, "accept"),
    ("GQ-01", None, "2026-09-05", 1, "check-out"),
    ("GQ-01", None, "2026-09-08", 1, "check-in"),
    ("GQ-01", None, "2026-09-12", 1, "check-out"),
    ("GQ-02", None, "2026-09-15", 1, "check-out"),
    ("GQ-02", None, "2026-09-17", 1, "check-in"),
    ("GQ-02", None, "2026-09-30", 1, "check-out"),
    ("GQ-02", None, "2026-09-30", 2, "check-in"),
    ("GQ-01", None, "2026-10-01", 1, "check-in"),
    ("GQ-04", "手持砂光机", "2026-10-02", 1, "accept"),
    ("GQ-01", None, "2026-10-06", 1, "check-out"),
    ("GQ-01", None, "2026-10-09", 1, "check-in"),
    ("GQ-03", None, "2026-10-12", 1, "check-out"),
    ("GQ-03", None, "2026-10-20", 1, "check-in"),
    ("GQ-02", None, "2026-10-31", 1, "retire"),
    ("GQ-03", None, "2026-10-31", 1, "check-out"),
    ("GQ-05", "折叠踏梯", "2026-11-01", 1, "accept"),
    ("GQ-04", None, "2026-11-03", 1, "check-out"),
    ("GQ-04", None, "2026-11-08", 1, "check-in"),
    ("GQ-01", None, "2026-11-05", 1, "check-out"),
    ("GQ-01", None, "2026-11-12", 1, "check-in"),
    ("GQ-04", None, "2026-11-15", 1, "check-out"),
    ("GQ-01", None, "2026-11-20", 1, "check-out"),
    ("GQ-03", None, "2026-11-20", 1, "check-in"),
    ("GQ-01", None, "2026-11-21", 1, "check-in"),
    ("GQ-06", "棘轮扳手套装", "2026-11-21", 1, "accept"),
)


def demo_rows() -> list[dict]:
    rows = [
        {"event_id": f"GQ-E{index:03d}", "asset_id": asset_id, "asset_name": asset_name,
         "event_date": day, "sequence": sequence, "event_type": event_type}
        for index, (asset_id, asset_name, day, sequence, event_type) in enumerate(_EVENTS, 1)
    ]
    # 模拟换班重传：10 月 31 日 GQ-03 借出事件重复上传，去重后只回放一次。
    rows.append(next(row for row in rows
                     if row["asset_id"] == "GQ-03" and row["event_date"] == "2026-10-31").copy())
    return rows

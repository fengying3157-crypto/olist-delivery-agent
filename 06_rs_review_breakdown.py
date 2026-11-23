from importlib import import_module
from pathlib import Path

import duckdb


def _review_group(included_orders, reviewed_orders, low_score_orders):
    return {
        "included_orders": included_orders,
        "reviewed_orders": reviewed_orders,
        "low_score_orders": low_score_orders,
        "review_coverage_rate": reviewed_orders / included_orders if included_orders else None,
        "low_score_rate": low_score_orders / reviewed_orders if reviewed_orders else None,
    }


def get_review_breakdown(con, state: str, month: str | None):
    """按订单统计低分率。每笔订单使用该订单的平均评价分。"""
    sql = """
        WITH order_groups AS (
            SELECT
                CASE
                    WHEN CAST(order_delivered_customer_date AS DATE)
                         > CAST(order_estimated_delivery_date AS DATE) THEN 'late'
                    ELSE 'on_time'
                END AS delivery_group,
                review_count,
                avg_review_score
            FROM olist_order_analysis_view
            WHERE customer_state = $state
              AND (
                  CAST($start AS DATE) IS NULL
                  OR (
                      order_purchase_timestamp >= CAST($start AS DATE)
                      AND order_purchase_timestamp < CAST($start AS DATE) + INTERVAL '1 month'
                  )
              )
              AND order_status = 'delivered'
              AND order_delivered_customer_date IS NOT NULL
              AND order_estimated_delivery_date IS NOT NULL
        )
        SELECT
            delivery_group,
            COUNT(*) AS included_orders,
            COUNT(*) FILTER (WHERE review_count > 0) AS reviewed_orders,
            COUNT(*) FILTER (
                WHERE review_count > 0 AND avg_review_score <= 2
            ) AS low_score_orders
        FROM order_groups
        GROUP BY delivery_group
    """
    rows = con.sql(sql, params={"state": state, "start": f"{month}-01" if month is not None else None}).fetchall()
    groups = {"late": _review_group(0, 0, 0), "on_time": _review_group(0, 0, 0)}
    for group, included, reviewed, low in rows:
        groups[group] = _review_group(included, reviewed, low)
    return groups["late"], groups["on_time"]


def get_state_review_scores(con, month: str | None):
    """已评价的有效订单各占一票，单笔订单取其评价均分。"""
    return con.sql("""
        SELECT customer_state, COUNT(*) AS reviewed_orders, AVG(avg_review_score) AS avg_score
        FROM olist_order_analysis_view
        WHERE customer_state IS NOT NULL
          AND order_status = 'delivered'
          AND order_delivered_customer_date IS NOT NULL
          AND order_estimated_delivery_date IS NOT NULL
          AND review_count > 0
          AND (
              CAST($start AS DATE) IS NULL
              OR (
                  order_purchase_timestamp >= CAST($start AS DATE)
                  AND order_purchase_timestamp < CAST($start AS DATE) + INTERVAL '1 month'
              )
          )
        GROUP BY customer_state
        ORDER BY avg_score DESC, reviewed_orders DESC, customer_state
        LIMIT 5
    """, params={"start": f"{month}-01" if month is not None else None}).fetchall()


def get_top_state_review(con, state_changes, compare_month: str):
    """用任务一的排序结果，诊断晚到订单增量最大的州。"""
    rows = state_changes["rows"]
    if not rows or rows[0][5] <= 0:
        return None

    state, _, _, _, _, delta = rows[0]
    late_group, on_time_group = get_review_breakdown(con, state, compare_month)
    return {
        "state": state,
        "month": compare_month,
        "late_delta": delta,
        "late_group": late_group,
        "on_time_group": on_time_group,
    }


def _print_review_group(label, group):
    rate = group["low_score_rate"]
    if rate is None:
        print(f"{label}无可用评价，低分率无法计算")
        return
    print(
        f"{label}低分率 {group['low_score_orders']}/{group['reviewed_orders']} = {rate:.2%}"
        f"（符合履约口径 {group['included_orders']} 单）"
    )


def main():
    module_name = f"{__package__}.06_state_attribution" if __package__ else "06_state_attribution"
    get_state_changes = import_module(module_name).get_state_changes
    data_dir = Path(r"E:\data\olist\raw")
    base_month, compare_month = "2017-07", "2017-08"

    with duckdb.connect() as con:
        con.read_csv(str(data_dir / "olist_orders_dataset.csv")).create_view("orders")
        con.read_csv(str(data_dir / "olist_customers_dataset.csv")).create_view("customers")
        con.read_csv(str(data_dir / "olist_order_reviews_dataset.csv")).create_view("reviews")

        con.sql("""
            CREATE VIEW olist_order_analysis_view AS
            WITH reviews_by_order AS (
                SELECT
                    order_id,
                    COUNT(review_score) AS review_count,
                    AVG(review_score) AS avg_review_score
                FROM reviews
                GROUP BY order_id
            )
            SELECT o.*, c.customer_state, r.review_count, r.avg_review_score
            FROM orders o
            LEFT JOIN customers c ON o.customer_id = c.customer_id
            LEFT JOIN reviews_by_order r ON o.order_id = r.order_id
        """)

        state_changes = get_state_changes(con, base_month, compare_month)
        result = get_top_state_review(con, state_changes, compare_month)

    if result is None:
        print("没有晚到订单数增加的州")
        return

    print(f"{base_month} → {compare_month} 晚到增量最大的州是 {result['state']}（{result['late_delta']:+d} 单）")
    print(f"{result['state']} 在 {result['month']} 的订单评分")
    _print_review_group("晚到组", result["late_group"])
    _print_review_group("按时组", result["on_time_group"])


if __name__ == "__main__":
    main()

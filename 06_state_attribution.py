from pathlib import Path
import duckdb

def get_state_changes(con, base_month: str, compare_month: str):
    """
    base_month, compare_month: "YYYY-MM"
    返回：
        rows: list 明细，按delta降序、州代码升序
        sum_increase: 所有delta>0的总和
        sum_decrease: 所有delta<0的总和（负数）
        net_change: sum_increase + sum_decrease
    """
    sql = """
    WITH monthly_state_stats AS (
        SELECT
            customer_state,
            strftime(order_purchase_timestamp, '%Y-%m') AS order_month,
            COUNT(CASE
                WHEN order_status = 'delivered'
                     AND order_delivered_customer_date IS NOT NULL
                     AND order_estimated_delivery_date IS NOT NULL
                THEN 1
            END) AS included_orders,
            COUNT(CASE
                WHEN order_status = 'delivered'
                     AND order_delivered_customer_date IS NOT NULL
                     AND order_estimated_delivery_date IS NOT NULL
                     AND CAST(order_delivered_customer_date AS DATE) > CAST(order_estimated_delivery_date AS DATE)
                THEN 1
            END) AS delayed_orders
        FROM olist_order_analysis_view
        WHERE order_purchase_timestamp >= $base_start
          AND order_purchase_timestamp < $base_end
          AND customer_state IS NOT NULL
        GROUP BY customer_state, order_month

        UNION ALL

        SELECT
            customer_state,
            strftime(order_purchase_timestamp, '%Y-%m') AS order_month,
            COUNT(CASE
                WHEN order_status = 'delivered'
                     AND order_delivered_customer_date IS NOT NULL
                     AND order_estimated_delivery_date IS NOT NULL
                THEN 1
            END) AS included_orders,
            COUNT(CASE
                WHEN order_status = 'delivered'
                     AND order_delivered_customer_date IS NOT NULL
                     AND order_estimated_delivery_date IS NOT NULL
                     AND CAST(order_delivered_customer_date AS DATE) > CAST(order_estimated_delivery_date AS DATE)
                THEN 1
            END) AS delayed_orders
        FROM olist_order_analysis_view
        WHERE order_purchase_timestamp >= $cmp_start
          AND order_purchase_timestamp < $cmp_end
          AND customer_state IS NOT NULL
        GROUP BY customer_state, order_month
    ),
    pivot_comparison AS (
        SELECT
            COALESCE(base.customer_state, cmp.customer_state) AS customer_state,
            COALESCE(base.included_orders, 0) AS base_included,
            COALESCE(base.delayed_orders, 0) AS base_delayed,
            COALESCE(cmp.included_orders, 0) AS cmp_included,
            COALESCE(cmp.delayed_orders, 0) AS cmp_delayed,
            COALESCE(cmp.delayed_orders, 0) - COALESCE(base.delayed_orders, 0) AS delayed_delta
        FROM (SELECT * FROM monthly_state_stats WHERE order_month = $base_month) base
        FULL OUTER JOIN (SELECT * FROM monthly_state_stats WHERE order_month = $cmp_month) cmp
          ON base.customer_state = cmp.customer_state
    )
    SELECT *
    FROM pivot_comparison
    ORDER BY delayed_delta DESC, customer_state ASC;
    """
    # 月份边界计算
    def get_month_range(ym: str):
        y, m = map(int, ym.split("-"))
        if m == 12:
            return f"{ym}-01", f"{y+1}-01-01"
        else:
            return f"{ym}-01", f"{y}-{m+1:02d}-01"
    base_start, base_end = get_month_range(base_month)
    cmp_start, cmp_end = get_month_range(compare_month)

    rows = con.sql(sql, params={
        "base_month": base_month,
        "cmp_month": compare_month,
        "base_start": base_start,
        "base_end": base_end,
        "cmp_start": cmp_start,
        "cmp_end": cmp_end
    }).fetchall()

    sum_inc = 0
    sum_dec = 0
    for r in rows:
        d = r[5]
        if d > 0:
            sum_inc += d
        elif d < 0:
            sum_dec += d
    net = sum_inc + sum_dec
    return {
        "rows": rows,
        "sum_increase": sum_inc,
        "sum_decrease": sum_dec,
        "net_change": net
    }


def get_state_late_totals(con, month: str | None):
    """按收货州统计有效订单和晚到订单，month=None 使用全部数据。"""
    return con.sql("""
        SELECT
            customer_state,
            COUNT(*) AS valid_orders,
            COUNT(*) FILTER (
                WHERE CAST(order_delivered_customer_date AS DATE)
                      > CAST(order_estimated_delivery_date AS DATE)
            ) AS late_orders
        FROM olist_order_analysis_view
        WHERE customer_state IS NOT NULL
          AND order_status = 'delivered'
          AND order_delivered_customer_date IS NOT NULL
          AND order_estimated_delivery_date IS NOT NULL
          AND (
              CAST($start AS DATE) IS NULL
              OR (
                  order_purchase_timestamp >= CAST($start AS DATE)
                  AND order_purchase_timestamp < CAST($start AS DATE) + INTERVAL '1 month'
              )
          )
        GROUP BY customer_state
    """, params={"start": f"{month}-01" if month is not None else None}).fetchall()


if __name__ == "__main__":
    # ========== 这里是你原来的建视图代码，保留不动 ==========
    con = duckdb.connect()
    data_dir = Path(r"E:\data\olist\raw")
    con.read_csv(str(data_dir / "olist_orders_dataset.csv")).create_view('orders')
    con.read_csv(str(data_dir / "olist_customers_dataset.csv")).create_view('customers')
    con.read_csv(str(data_dir / "olist_order_reviews_dataset.csv")).create_view('reviews')

    con.sql("""
        CREATE OR REPLACE VIEW v_orders_with_customers AS
        SELECT o.*, c.customer_state, c.customer_city
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.customer_id
    """)
    con.sql("""
        CREATE OR REPLACE VIEW v_order_reviews_agg AS
        SELECT
            order_id,
            COUNT(review_score) AS review_count,
            AVG(review_score) AS avg_review_score
        FROM reviews
        GROUP BY order_id
    """)
    con.sql("""
        CREATE OR REPLACE VIEW olist_order_analysis_view AS
        SELECT o.*, r.review_count, r.avg_review_score
        FROM v_orders_with_customers o
        LEFT JOIN v_order_reviews_agg r ON o.order_id = r.order_id
    """)

    # 验收调用：2017-07基准，对比2017-08
    res = get_state_changes(con, "2017-07", "2017-08")
    print("=== 各州晚到增量明细（delta降序，同delta按州升序） ===")
    for row in res["rows"]:
        print(f"州:{row[0]} | 基准纳入:{row[1]},基准晚到:{row[2]} | 对比纳入:{row[3]},对比晚到:{row[4]} | delta={row[5]}")

    print("\n===== 汇总 =====")
    print(f"增加合计: {res['sum_increase']}")
    print(f"减少合计: {res['sum_decrease']}")
    print(f"净增: {res['net_change']}")

    print("\n===== 前三名 =====")
    for i, r in enumerate(res["rows"][:3]):
        print(f"第{i+1}名 州{r[0]} 基准晚到{r[2]}，对比晚到{r[4]}，delta={r[5]}")

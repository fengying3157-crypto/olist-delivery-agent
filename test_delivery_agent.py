import json
from types import SimpleNamespace
from unittest.mock import Mock

import duckdb
import pytest

import delivery_diagnosis
from delivery_diagnosis import (
    TOOL_NAME,
    TOOL_STATE_NAME,
    chat_loop,
    load_order_analysis,
    orders_csv_path,
    reset_tool_records,
    tool_call_records,
    PENDING_STATE,
    PENDING_LATE,
)


@pytest.fixture(scope="module")
def db_conn():
    csv_path = orders_csv_path()
    if not csv_path.is_file():
        pytest.fail(f"找不到 Olist 订单 CSV，请设置 OLIST_ORDERS_CSV。当前路径 {csv_path}")
    with duckdb.connect() as con:
        con.read_csv(str(csv_path)).create_view("orders")
        yield con


@pytest.fixture(scope="module")
def state_db_conn(db_conn):
    load_order_analysis(db_conn)
    return db_conn


@pytest.fixture(autouse=True)
def isolate_calls(monkeypatch):
    monkeypatch.delenv("NUAA_AI_API_KEY", raising=False)
    reset_tool_records()


@pytest.fixture
def model_client():
    return Mock()


def model_proposes(client, base_month, compare_month, *, tool_name=TOOL_NAME):
    function = SimpleNamespace(
        name=tool_name,
        arguments=json.dumps({"base_month": base_month, "compare_month": compare_month}),
    )
    message = SimpleNamespace(tool_calls=[SimpleNamespace(type="function", function=function)])
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=message)]
    )


def one_query(base_month, compare_month, *, tool_name=TOOL_NAME):
    assert len(tool_call_records) == 1
    record = tool_call_records[0]
    assert record["func"] == tool_name
    assert (record["base_month"], record["compare_month"]) == (base_month, compare_month)
    return record["result"]


def test_case1_201708_vs_201707(db_conn, model_client):
    # 即使模型把月份顺序反过来，实际查询也要遵循用户原话。
    model_proposes(model_client, "2017-07", "2017-08")
    response, _ = chat_loop(db_conn, "以2017-08为基准，对比2017-07的晚到变化", client=model_client, pending=None)

    model_client.chat.completions.create.assert_called_once()
    result = one_query("2017-08", "2017-07")
    assert result["status"] == "ok"
    assert result["base"]["valid"] == 4193
    assert result["compare"]["valid"] == 3872
    assert result["diff"]["late"] == -14
    assert result["diff"]["rate"] == pytest.approx(108 / 3872 - 122 / 4193)
    assert "| 晚到订单数 | 122 | 108 | -14 |" in response
    assert "-0.12 个百分点" in response


def test_case2_explain_date_rule(db_conn, model_client):
    response, _ = chat_loop(db_conn, "解释为什么要用DATE而不是原始时间戳判断晚到", client=model_client, pending=None)

    assert tool_call_records == []
    model_client.chat.completions.create.assert_not_called()
    assert "DATE" in response
    assert "实际送达日期晚于预计送达日期" in response
    assert "请提供" not in response


def test_case3_agent_eval_scope(db_conn, model_client):
    question = "针对这个 Olist 晚到 Agent，最终评测除了正常月份查询，还应覆盖哪两类问题？"
    response, _ = chat_loop(db_conn, question, client=model_client, pending=None)

    assert tool_call_records == []
    model_client.chat.completions.create.assert_not_called()
    assert "口径" in response
    assert "缺少月份" in response
    assert "无数据" in response


def test_case4_missing_month_prompt(db_conn, model_client):
    response, _ = chat_loop(db_conn, "最近晚到情况怎么样？", client=model_client, pending=None)

    assert tool_call_records == []
    model_client.chat.completions.create.assert_not_called()
    assert "两个月份" in response
    assert "基准月" in response


def test_case5_out_of_range_month(db_conn, model_client):
    # 即使模型把超出范围的年份改成有数据的年份，也保留原输入。
    model_proposes(model_client, "2017-08", "2018-01")
    response, _ = chat_loop(db_conn, "比较 2017 年 8 月 和 2099 年 1 月", client=model_client, pending=None)

    result = one_query("2017-08", "2099-01")
    assert result["status"] == "no_data"
    assert result["missing_months"] == ["2099-01"]
    assert result["base"]["valid"] == 4193
    assert result["compare"]["rate"] is None
    assert result["diff"] is None
    assert "2099-01" in response and "no_data" in response


def test_case6_201807_vs_201808(db_conn, model_client):
    model_proposes(model_client, "2018-07", "2018-08")
    response, _ = chat_loop(db_conn, "比较2018年7月和8月的晚到情况", client=model_client, pending=None)

    model_client.chat.completions.create.assert_called_once()
    assert model_client.chat.completions.create.call_args.kwargs["tools"][0]["function"]["name"] == TOOL_NAME
    result = one_query("2018-07", "2018-08")
    assert (result["base"]["valid"], result["base"]["late"]) == (6156, 208)
    assert (result["compare"]["valid"], result["compare"]["late"]) == (6351, 393)
    assert result["diff"]["late"] == 185
    assert result["diff"]["rate"] == pytest.approx(393 / 6351 - 208 / 6156)
    assert "+2.81 个百分点" in response


def test_case7_state_diagnosis(state_db_conn, model_client):
    model_proposes(model_client, "2017-07", "2017-08", tool_name=TOOL_STATE_NAME)
    question = "以 2017 年 7 月为基准，对比 8 月，哪个州晚到增加最多？这个州的低分率怎么样？"
    response, _ = chat_loop(state_db_conn, question, client=model_client, pending=None)

    result = one_query("2017-07", "2017-08", tool_name=TOOL_STATE_NAME)
    assert result["status"] == "ok"
    assert result["diff"]["late"] == 14
    assert result["top_state"] == "RS"
    assert (result["base_delayed"], result["cmp_delayed"], result["delta"]) == (6, 22, 16)
    late = result["review"]["late_group"]
    on_time = result["review"]["on_time_group"]
    assert (late["low_score_orders"], late["reviewed_orders"]) == (9, 22)
    assert (on_time["low_score_orders"], on_time["reviewed_orders"], on_time["included_orders"]) == (22, 273, 274)
    for snippet in ("总体晚到净增：+14 单", "40.91%", "8.06%", "高于按时组"):
        assert snippet in response


def test_case8_state_diagnosis_no_data(db_conn, model_client, monkeypatch):
    state_changes = Mock()
    top_state_review = Mock()
    monkeypatch.setattr(delivery_diagnosis, "get_state_changes", state_changes)
    monkeypatch.setattr(delivery_diagnosis, "get_top_state_review", top_state_review)
    model_proposes(model_client, "2017-07", "2018-01", tool_name=TOOL_STATE_NAME)
    question = "以 2017 年 7 月为基准，对比 2099 年 1 月，哪个州晚到增加最多？"
    response, _ = chat_loop(db_conn, question, client=model_client, pending=None)

    result = one_query("2017-07", "2099-01", tool_name=TOOL_STATE_NAME)
    assert result["status"] == "no_data"
    assert result["missing_months"] == ["2099-01"]
    assert result["diff"] is None
    state_changes.assert_not_called()
    top_state_review.assert_not_called()
    assert "2099-01" in response and "no_data" in response


def test_case9_state_diagnosis_recomputes_rs_reviews():
    with duckdb.connect() as con:
        con.execute("""
            CREATE TABLE orders AS
            SELECT * FROM (VALUES
                ('RS', 'delivered', TIMESTAMP '2017-07-10', TIMESTAMP '2017-07-15 11:00', TIMESTAMP '2017-07-15', 1, 3.0),
                ('RS', 'delivered', TIMESTAMP '2017-08-10', TIMESTAMP '2017-08-15 11:00', TIMESTAMP '2017-08-14', 2, 4.0),
                ('RS', 'delivered', TIMESTAMP '2017-08-10', TIMESTAMP '2017-08-15 12:00', TIMESTAMP '2017-08-14', NULL, NULL),
                ('RS', 'delivered', TIMESTAMP '2017-08-10', TIMESTAMP '2017-08-15 13:00', TIMESTAMP '2017-08-15', 1, 2.0)
            ) AS row(customer_state, order_status, order_purchase_timestamp, order_delivered_customer_date,
                     order_estimated_delivery_date, review_count, avg_review_score)
        """)
        con.execute("CREATE VIEW olist_order_analysis_view AS SELECT * FROM orders")
        response, _ = chat_loop(con, "对比 2017-07 和 2017-08，哪个州晚到增加最多？", pending=None)

    result = one_query("2017-07", "2017-08", tool_name=TOOL_STATE_NAME)
    late = result["review"]["late_group"]
    on_time = result["review"]["on_time_group"]
    assert (late["low_score_orders"], late["reviewed_orders"], late["included_orders"]) == (0, 1, 2)
    assert (on_time["low_score_orders"], on_time["reviewed_orders"]) == (1, 1)
    assert "晚到组的低分率低于按时组" in response


def test_case10_multi_round_state_normal(state_db_conn, model_client):
    resp1, pending1 = chat_loop(state_db_conn, "最近哪个州晚到增加最多？", client=model_client, pending=None)
    assert tool_call_records == []
    model_client.chat.completions.create.assert_not_called()
    assert "请提供需要对比的两个月份" in resp1
    assert pending1 == PENDING_STATE

    # ✅ 新增：给第二轮预埋mock返回，和前面model_proposes逻辑一致
    model_proposes(model_client, "2017-07", "2017-08", tool_name=TOOL_STATE_NAME)
    resp2, pending2 = chat_loop(state_db_conn, "以 2017 年 7 月为基准，对比 8 月。", client=model_client, pending=pending1)
    assert len(tool_call_records) == 1
    record = tool_call_records[0]
    assert record["func"] == TOOL_STATE_NAME
    assert (record["base_month"], record["compare_month"]) == ("2017-07", "2017-08")
    assert pending2 is None

    assert "总体晚到净增：+14 单" in resp2
    assert "RS，6 → 22，增量 +16 单" in resp2
    assert "40.91%" in resp2
    assert "8.06%" in resp2


def test_case11_multi_round_state_nodata(state_db_conn, model_client):
    reset_tool_records()
    resp1, pending1 = chat_loop(state_db_conn, "最近哪个州晚到增加最多？", client=model_client, pending=None)
    assert tool_call_records == []
    assert pending1 == PENDING_STATE

    # ✅ 新增：预埋mock
    model_proposes(model_client, "2017-07", "2099-01", tool_name=TOOL_STATE_NAME)
    resp2, pending2 = chat_loop(state_db_conn, "以2017-07为基准，对比2099-01", client=model_client, pending=pending1)
    assert len(tool_call_records) == 1
    record = tool_call_records[0]
    assert record["func"] == TOOL_STATE_NAME
    assert record["result"]["status"] == "no_data"
    assert "2099-01" in resp2
    assert "no_data" in resp2
    assert pending2 is None


def test_case12_all_history_rank_after_user_clarifies(state_db_conn, model_client):
    response, pending = chat_loop(
        state_db_conn, "帮我看下哪个州晚于预定到达的最多", client=model_client
    )
    assert "全部数据" in response and "一个月份" in response
    assert tool_call_records == []

    response, pending = chat_loop(state_db_conn, "所有的", pending=pending, client=model_client)
    assert pending is None
    assert "SP 州" in response and "1,820" in response and "40,494" in response
    assert len(tool_call_records) == 1 and tool_call_records[0]["month"] is None
    assert tool_call_records[0]["func"] == "state_late_count"
    model_client.chat.completions.create.assert_not_called()


def test_case13_state_counts_and_rates_rank_independently():
    with duckdb.connect() as con:
        con.execute("""
            CREATE VIEW olist_order_analysis_view AS
            SELECT state AS customer_state, TIMESTAMP '2017-08-01' AS order_purchase_timestamp,
                   'delivered' AS order_status, TIMESTAMP '2017-08-03' AS order_delivered_customer_date,
                   CASE WHEN late THEN TIMESTAMP '2017-08-02' ELSE TIMESTAMP '2017-08-03' END
                       AS order_estimated_delivery_date
            FROM (VALUES ('SP', true), ('SP', true), ('SP', false), ('RJ', true)) AS orders(state, late)
        """)
        count_reply, _ = chat_loop(con, "2017-08 哪个州晚到最多？")
        rate_reply, _ = chat_loop(con, "2017-08 哪个州的晚到率最高？")

    assert "按晚到单数排名第一的是 SP" in count_reply
    assert "按晚到率排名第一的是 RJ" in rate_reply
    assert [record["month"] for record in tool_call_records] == ["2017-08", "2017-08"]


def test_case14_single_month_overall_and_state_reviews(state_db_conn):
    monthly, _ = chat_loop(state_db_conn, "2017-08 的总体晚到率是多少？")
    review, _ = chat_loop(state_db_conn, "RS 州 2017-08 的晚到组和按时组低分率是多少？")

    assert "4,193" in monthly and "122" in monthly and "2.91%" in monthly
    assert "9/22" in review and "22/273" in review
    assert [record["func"] for record in tool_call_records] == ["overall_late", "state_review"]


def test_case15_best_delivery_means_highest_on_time_rate(state_db_conn):
    reply, pending = chat_loop(state_db_conn, "哪个州完成的最好")
    assert pending is None
    assert "按准时率排名第一的是 AM 州" in reply
    assert "准时 141/145 单" in reply and "97.24%" in reply
    assert tool_call_records[0]["month"] is None


def test_case16_best_review_ranks_states_without_a_state_code(state_db_conn):
    reply, pending = chat_loop(state_db_conn, "哪个州的评分最好")
    assert pending is None
    assert "按订单平均评分排名第一的是 SP 州" in reply
    assert "4.247/5 分" in reply and "40,266 单" in reply
    assert tool_call_records[0]["month"] is None


def test_case17_best_state_can_inherit_the_last_single_month(state_db_conn):
    context = {}
    reply, pending = chat_loop(state_db_conn, "2017-08 的总体晚到率是多少？", context=context)
    assert pending is None and "2.91%" in reply
    reply, pending = chat_loop(state_db_conn, "哪个州的评分最好？", context=context)
    assert pending is None and reply.startswith("2017-08按订单平均评分排名")
    reply, pending = chat_loop(state_db_conn, "全部数据里哪个州完成得最好？", context=context)
    assert pending is None and "准时 141/145 单" in reply
    assert [record["month"] for record in tool_call_records] == ["2017-08", "2017-08", None]


@pytest.mark.parametrize(("question", "expected"), [
    ("同一个订单，如果预计送达日期和实际送达日期是同一天、但实际送达时间更晚，算不算晚到？", "不算晚到"),
    ("承诺当天收货，包裹当天晚上才签收，这单迟到了吗？", "不算晚到"),
    ("包裹比预计日期晚一天送到，是否算逾期？", "算晚到"),
    ("提前一天签收怎么算？", "算按时"),
    ("没有实际签收日期的包裹，要计入晚到率分母吗？", "缺任一日期"),
    ("晚到率分母都包含哪些订单？", "分母只纳入已送达"),
    ("8月下单9月签收，归到哪个月份统计？", "归入 8 月"),
    ("一张订单有多条评价，低分率怎么计算？", "先对同一订单的多条评分取平均值"),
])
def test_delivery_policy_paraphrases(question, expected, model_client):
    context = {}
    reply, pending = chat_loop(None, question, client=model_client, context=context)
    assert expected in reply
    assert pending is None and context["reply_kind"] == "rule"
    assert tool_call_records == []
    model_client.chat.completions.create.assert_not_called()


def test_rule_question_does_not_consume_pending_comparison(model_client):
    reply, pending = chat_loop(
        None, "当天晚上才签收算不算晚到？",
        client=model_client, pending=PENDING_STATE,
    )
    assert "不算晚到" in reply and pending == PENDING_STATE
    assert tool_call_records == []


def test_ambiguous_question_offers_choices_instead_of_asking_for_months():
    context = {}
    reply, pending = chat_loop(None, "帮我看看这个订单", context=context)
    assert context["reply_kind"] == "clarify"
    assert "查晚到数据" in reply and "怎么判定晚到" in reply
    assert pending is None and tool_call_records == []


def test_semantic_fallback_uses_approved_rule_only(model_client):
    model_client.chat.completions.create.return_value = SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content='{"intent":"same_day"}'))
    ])
    reply, pending = chat_loop(None, "还是那条承诺线，晚了几个小时", client=model_client)
    assert "不算晚到" in reply and pending is None
    assert tool_call_records == []
    model_client.chat.completions.create.assert_called_once()


def test_unrelated_question_with_two_months_does_not_run_delivery_query():
    context = {}
    reply, pending = chat_loop(None, "2017-07 和 2017-08 的足球比赛结果是什么？", context=context)
    assert "你想查晚到数据" in reply and pending is None
    assert context["reply_kind"] == "clarify"
    assert tool_call_records == []


def test_semantic_query_keeps_original_month(state_db_conn, model_client):
    model_client.chat.completions.create.return_value = SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(
            content='{"intent":"state_late_count","month":"2017-08"}'
        ))
    ])
    reply, pending = chat_loop(state_db_conn, "请盘点一下妥投分布 2099-01", client=model_client)
    assert "2099-01" in reply and "no_data" in reply and pending is None
    assert len(tool_call_records) == 1 and tool_call_records[0]["month"] == "2099-01"

import json
import os
import re
from importlib import import_module
from pathlib import Path

import duckdb
from openai import OpenAI, OpenAIError


MODULE_PREFIX = f"{__package__}." if __package__ else ""
state_analysis = import_module(f"{MODULE_PREFIX}06_state_attribution")
review_analysis = import_module(f"{MODULE_PREFIX}06_rs_review_breakdown")
get_state_changes = state_analysis.get_state_changes
get_state_late_totals = state_analysis.get_state_late_totals
get_top_state_review = review_analysis.get_top_state_review
get_review_breakdown = review_analysis.get_review_breakdown
get_state_review_scores = review_analysis.get_state_review_scores
delivery_questions = import_module(f"{MODULE_PREFIX}delivery_questions")
RULE_ANSWERS = delivery_questions.RULE_ANSWERS
classify_rule_question = delivery_questions.classify_rule_question

DEFAULT_ORDERS_CSV = Path(r"E:\data\olist\raw\olist_orders_dataset.csv")
DEFAULT_CUSTOMERS_CSV = Path(r"E:\data\olist\raw\olist_customers_dataset.csv")
DEFAULT_REVIEWS_CSV = Path(r"E:\data\olist\raw\olist_order_reviews_dataset.csv")
FIRST_MONTH = "2016-09"
END_MONTH = "2018-11"  # 不含此月
TOOL_NAME = "diagnose_delivery_late"
TOOL_STATE_NAME = "diagnose_state_late_top"
ASK_FOR_MONTHS = "请提供需要对比的两个月份（YYYY-MM），并说明哪个是基准月。"
PENDING_STATE = "state_diagnosis"
PENDING_LATE = "late_rate_compare"

MONTH_PATTERN = re.compile(
    r"(?<![0-9])(?P<year>[0-9]{4})\s*(?:年|[-/])\s*"
    r"(?P<month>[0-9]{1,2})(?:\s*月)?(?![0-9])"
    r"|(?<![0-9])(?P<short_month>[0-9]{1,2})\s*月"
)
BASE_BEFORE = re.compile(r"(?:以|基准月(?:份)?\s*(?:是|为|[:：])?)\s*$")
BASE_AFTER = re.compile(r"^\s*(?:为|作为)\s*基准")
EVAL_QUESTION = re.compile(r"评测|评估用例|测试用例|覆盖哪两类")
STATE_DIAG_QUESTION = re.compile(r"哪个州.*晚到.*增加最多|增量最大的州|州维度.*晚到|履约州分析|各州晚到增量")

ALL_ORDERS = re.compile(r"全部|所有|全量|全时段|整个数据集|全时期|全历史|不限时间")
LATE_TOPIC = re.compile(r"晚到|迟到|延迟|逾期|超时|晚于(?:预定|预计|预期|承诺)")
RANK_TOPIC = re.compile(r"最多|最高|排行|排名|第一|较多|靠前")
REVIEW_TOPIC = re.compile(r"低分|评分|评价")
TOTAL_TOPIC = re.compile(r"晚到率|迟到率|按时率|准时率|订单量|订单(?:总)?数|几单|多少单")
STATE_CODE = re.compile(
    r"(?<![A-Za-z])(AC|AL|AM|AP|BA|CE|DF|ES|GO|MA|MG|MS|MT|PA|PB|PE|PI|PR|RJ|RN|RO|RR|RS|SC|SE|SP|TO)(?![A-Za-z])",
    re.IGNORECASE,
)
ASK_FOR_SCOPE = "你想统计全部数据，还是指定一个月份？可以回复「全部数据」或「2017-08」。"
SCOPE_RANK_COUNT = "state_late_count"
SCOPE_RANK_RATE = "state_late_rate"
SCOPE_TOTAL = "overall_late"
SCOPE_REVIEW = "state_review"
SCOPE_RANK_ON_TIME = "state_on_time_rank"
SCOPE_RANK_REVIEW = "state_review_rank"
RANK_DEFAULT_SCOPE = {SCOPE_RANK_ON_TIME, SCOPE_RANK_REVIEW}
SCOPED_KINDS = {SCOPE_RANK_COUNT, SCOPE_RANK_RATE, SCOPE_TOTAL, SCOPE_REVIEW} | RANK_DEFAULT_SCOPE

TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "分别统计基准月和对比月的订单晚到率，变化按对比月减基准月计算。",
        "parameters": {
            "type": "object",
            "properties": {
                "base_month": {"type": "string", "description": "基准月，YYYY-MM"},
                "compare_month": {"type": "string", "description": "对比月，YYYY-MM"},
            },
            "required": ["base_month", "compare_month"],
            "additionalProperties": False,
        },
    },
}

TOOL_STATE = {
    "type": "function",
    "function": {
        "name": TOOL_STATE_NAME,
        "description": "对比基准月、对比月，找出晚到订单增量最大的州，并且返回该州对比月的晚到/按时订单低分率。",
        "parameters": {
            "type": "object",
            "properties": {
                "base_month": {"type": "string", "description": "基准月，YYYY-MM"},
                "compare_month": {"type": "string", "description": "对比月，YYYY-MM"},
            },
            "required": ["base_month", "compare_month"],
            "additionalProperties": False,
        },
    },
}

tool_call_records = []


def reset_tool_records():
    tool_call_records.clear()


def orders_csv_path():
    return Path(os.getenv("OLIST_ORDERS_CSV", str(DEFAULT_ORDERS_CSV)))
def customers_csv_path():
    return Path(os.getenv("OLIST_CUSTOMERS_CSV", str(DEFAULT_CUSTOMERS_CSV)))
def reviews_csv_path():
    return Path(os.getenv("OLIST_REVIEWS_CSV", str(DEFAULT_REVIEWS_CSV)))


def _valid_month(month):
    return (
        isinstance(month, str)
        and re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month) is not None
        and not month.startswith("0000")
    )


def _months_in_question(question):
    """识别用户给出的年月。省略的年份只从同一句中补齐。"""
    hits = list(MONTH_PATTERN.finditer(question))
    years = {hit["year"] for hit in hits if hit["year"] is not None}
    year = next(iter(years)) if len(years) == 1 else None
    months = []
    explicit_bases = set()
    for hit in hits:
        if hit["year"] is not None:
            year = hit["year"]
        if year is None:
            return [], set()
        number = hit["month"] or hit["short_month"]
        month = f"{year}-{int(number):02d}"
        months.append(month)
        if BASE_BEFORE.search(question[:hit.start()]) or BASE_AFTER.match(question[hit.end():]):
            explicit_bases.add(month)
    return months, explicit_bases


def get_month_stat(con, month):
    if month is not None and not _valid_month(month):
        raise ValueError("月份必须是有效的 YYYY-MM")

    total = valid = late = 0
    if month is None or FIRST_MONTH <= month < END_MONTH:
        start = f"{month}-01" if month is not None else None
        sql = """
            WITH month_orders AS (
                SELECT
                    order_status = 'delivered'
                        AND order_delivered_customer_date IS NOT NULL
                        AND order_estimated_delivery_date IS NOT NULL AS eligible,
                    CAST(order_delivered_customer_date AS DATE)
                        > CAST(order_estimated_delivery_date AS DATE) AS is_late
                FROM orders
                WHERE CAST($1 AS DATE) IS NULL
                   OR (
                       order_purchase_timestamp >= CAST($1 AS DATE)
                       AND order_purchase_timestamp < CAST($1 AS DATE) + INTERVAL '1 month'
                   )
            )
            SELECT
                COUNT(*),
                COUNT(*) FILTER (WHERE eligible),
                COUNT(*) FILTER (WHERE eligible AND is_late)
            FROM month_orders
        """
        total, valid, late = con.sql(sql, params=[start]).fetchall()[0]

    return {
        "total": total,
        "valid": valid,
        "excluded": total - valid,
        "late": late,
        "ontime": valid - late,
        "rate": late / valid if valid else None,
    }


def diagnose_delivery_late(con, base_month, compare_month):
    """分别统计两个月，差值统一为对比月减基准月。"""
    if not _valid_month(base_month) or not _valid_month(compare_month):
        return {"status": "invalid_input", "message": "月份必须是有效的 YYYY-MM。"}
    if base_month == compare_month:
        return {"status": "invalid_input", "message": "基准月和对比月不能相同。"}

    base = get_month_stat(con, base_month)
    compare = get_month_stat(con, compare_month)
    result = {
        "base_month": base_month,
        "compare_month": compare_month,
        "base": base,
        "compare": compare,
    }
    missing = [
        month
        for month, stats in ((base_month, base), (compare_month, compare))
        if stats["valid"] == 0
    ]
    if missing:
        return {**result, "status": "no_data", "missing_months": missing, "diff": None}

    diff = {key: compare[key] - base[key] for key in base}
    return {**result, "status": "ok", "diff": diff}


def diagnose_state_late_top(con, base_month, compare_month):
    overall = diagnose_delivery_late(con, base_month, compare_month)
    if overall["status"] != "ok":
        return overall

    changes = get_state_changes(con, base_month, compare_month)
    review = get_top_state_review(con, changes, compare_month)
    if review is None:
        return {**overall, "top_state": None}

    _, _, base_delayed, _, compare_delayed, delta = changes["rows"][0]
    return {
        **overall,
        "top_state": review["state"],
        "base_delayed": base_delayed,
        "cmp_delayed": compare_delayed,
        "delta": delta,
        "review": review,
    }


def _render_state_result(result):
    if result["status"] == "invalid_input":
        return result["message"]
    if result["status"] == "no_data":
        months = "、".join(result["missing_months"])
        return f"{months} 没有符合口径的有效订单（no_data），无法完成州级诊断。"

    lines = [
        f"基准月 {result['base_month']} → 对比月 {result['compare_month']}",
        f"总体晚到净增：{result['diff']['late']:+d} 单",
    ]
    state = result["top_state"]
    if state is None:
        lines.append("未找到晚到订单数增加的州。")
        return "\n".join(lines)

    lines.extend([
        f"晚到增量最大的州：{state}，{result['base_delayed']} → {result['cmp_delayed']}，增量 {result['delta']:+d} 单",
        f"{state} 在对比月的评价拆解：",
    ])
    review = result["review"]
    for label, group in (("晚到组", review["late_group"]), ("按时组", review["on_time_group"])):
        rate = group["low_score_rate"]
        if rate is None:
            lines.append(f"  {label}：无可用评价，低分率无法计算。")
        else:
            lines.append(
                f"  {label}：{group['low_score_orders']}/{group['reviewed_orders']}，低分率 {rate:.2%}"
            )

    late_rate = review["late_group"]["low_score_rate"]
    on_time_rate = review["on_time_group"]["low_score_rate"]
    if late_rate is not None and on_time_rate is not None:
        if late_rate > on_time_rate:
            lines.append(f"本月 {state} 晚到组的低分率高于按时组。")
        elif late_rate < on_time_rate:
            lines.append(f"本月 {state} 晚到组的低分率低于按时组。")
        else:
            lines.append(f"本月 {state} 两组低分率相同。")
    return "\n".join(lines)


def _configured_client():
    key = os.getenv("NUAA_AI_API_KEY")
    if not key:
        return None
    return OpenAI(
        api_key=key,
        base_url=os.getenv("NUAA_AI_BASE_URL", "https://token.nuaa.edu.cn/v1"),
        timeout=20,
        max_retries=0,
    )


def _confirmed_tool_arguments(client, question, expected, tool_name):
    if client is None:
        return expected

    tools = [TOOL, TOOL_STATE]
    messages = [
        {
            "role": "system",
            "content": (
                "调用 Olist 晚到对比工具，必须使用程序已从用户提问确认的月份和顺序。"
                f"目标工具 {tool_name}，已确认参数为 {json.dumps(expected)}。"
            ),
        },
        {"role": "user", "content": question},
    ]
    try:
        response = client.chat.completions.create(
            model=os.getenv("NUAA_AI_MODEL_ID", "kimi-k3"),
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
    except OpenAIError:
        return expected

    if response.choices:
        calls = response.choices[0].message.tool_calls or []
        for call in calls:
            if call.type != "function" or call.function.name != tool_name:
                continue
            try:
                args = json.loads(call.function.arguments)
            except (TypeError, ValueError):
                continue
            if args == expected:
                return args
    return expected


def _run_delivery_tool(con, args):
    result = diagnose_delivery_late(con, **args)
    tool_call_records.append({"func": TOOL_NAME, **args, "result": result})
    print(f"[TOOL LOG] 工具名 {TOOL_NAME}，参数 {args}")
    return result


def _run_state_tool(con, args):
    result = diagnose_state_late_top(con, **args)
    tool_call_records.append({"func": TOOL_STATE_NAME, **args, "result": result})
    print(f"[TOOL LOG] 工具名 {TOOL_STATE_NAME}，参数 {args}")
    return result


def _render_result(result):
    if result["status"] == "invalid_input":
        return result["message"]
    if result["status"] == "no_data":
        months = "、".join(result["missing_months"])
        return f"{months} 没有符合口径的有效订单（no_data），无法计算晚到率差值。"

    base, compare, diff = result["base"], result["compare"], result["diff"]
    lines = [
        f"| 指标 | 基准月 {result['base_month']} | 对比月 {result['compare_month']} | 对比月 − 基准月 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for key, label in (
        ("total", "总订单数"),
        ("valid", "有效订单数"),
        ("late", "晚到订单数"),
        ("ontime", "按时订单数"),
    ):
        lines.append(f"| {label} | {base[key]:,} | {compare[key]:,} | {diff[key]:+,} |")
    lines.append(
        f"| 晚到率 | {base['rate']:.2%} | {compare['rate']:.2%} "
        f"| {diff['rate'] * 100:+.2f} 个百分点 |"
    )
    return "\n".join(lines)


def _scoped_intent(question):
    if STATE_DIAG_QUESTION.search(question):
        return None
    if "州" in question:
        if re.search(r"评分|评价|均分|满意度|口碑", question) and re.search(r"最好|最高|排行|排名|第一", question) and "低分率" not in question:
            if not STATE_CODE.search(question):
                return SCOPE_RANK_REVIEW, None
        if re.search(r"(?:履约|配送|送达|完成|表现).{0,4}最好|(?:准时率|按时率).{0,6}(?:最高|最好|排名|排行|第一)|最(?:准时|按时)", question):
            return SCOPE_RANK_ON_TIME, None
    if "州" in question and LATE_TOPIC.search(question) and RANK_TOPIC.search(question):
        kind = SCOPE_RANK_RATE if "晚到率" in question or "迟到率" in question else SCOPE_RANK_COUNT
        return kind, None
    if REVIEW_TOPIC.search(question):
        match = STATE_CODE.search(question)
        return SCOPE_REVIEW, match[0].upper() if match else None
    if (LATE_TOPIC.search(question) or TOTAL_TOPIC.search(question)) and not re.search(
        r"最近|变化|对比|比较|相比|增加|增量", question
    ):
        months, _ = _months_in_question(question)
        if len(set(months)) <= 1 and (ALL_ORDERS.search(question) or TOTAL_TOPIC.search(question) or "多少" in question):
            return SCOPE_TOTAL, None
    return None


def _scoped_reply(con, question, intent, context):
    kind, state = intent
    if kind == SCOPE_REVIEW and state is None:
        match = STATE_CODE.search(question)
        if not match:
            return "请提供收货州代码，例如「RS 州 2017-08 的晚到组和按时组评分怎么样？」", intent
        state = match[0].upper()

    months, _ = _months_in_question(question)
    if any(not _valid_month(month) for month in months):
        return "月份无效，请输入有效的 YYYY-MM，例如 2017-08。", (kind, state)
    unique = set(months)
    full_history = ALL_ORDERS.search(question) is not None
    if len(unique) > 1 or (full_history and unique):
        return ASK_FOR_SCOPE, (kind, state)

    if unique or full_history:
        month = next(iter(unique), None)
    elif kind in RANK_DEFAULT_SCOPE and not re.search(r"最近|今年|去年|上月|本月", question):
        month = context.get("last_scope_month") if context is not None else None
    else:
        return ASK_FOR_SCOPE, (kind, state)
    scope = month or "全部数据"
    reply = _run_scope_query(con, kind, state, month, scope)
    if context is not None and "（no_data）" not in reply:
        context["last_scope_month"] = month
    tool_call_records.append({"func": kind, "state": state, "month": month, "result": reply})
    return reply, None


def _run_scope_query(con, kind, state, month, scope):
    if kind == SCOPE_TOTAL:
        stats = get_month_stat(con, month)
        if not stats["valid"]:
            return f"{scope} 没有符合口径的有效订单（no_data）。"
        return (
            f"{scope}的总体履约\n"
            f"总订单 {stats['total']:,} 单，有效订单 {stats['valid']:,} 单。\n"
            f"晚到 {stats['late']:,} 单，晚到率 {stats['rate']:.2%}。\n"
            f"按时 {stats['ontime']:,} 单，按时率 {1 - stats['rate']:.2%}。"
        )

    if kind == SCOPE_REVIEW:
        late, on_time = get_review_breakdown(con, state, month)
        if not (late["included_orders"] or on_time["included_orders"]):
            return f"{scope}的 {state} 州没有符合口径的有效订单（no_data）。"
        return "\n".join([
            f"{scope} · {state} 州评价拆解",
            _review_line("晚到组", late),
            _review_line("按时组", on_time),
        ])

    if kind == SCOPE_RANK_REVIEW:
        scores = get_state_review_scores(con, month)
        if not scores:
            return f"{scope}没有可用的州级评分（no_data）。"
        lines = [f"{scope}按订单平均评分排名第一的是 {scores[0][0]} 州。", "前 5 名"]
        for index, (state, reviewed, score) in enumerate(scores, start=1):
            lines.append(f"{index}. {state} 州 · {score:.3f}/5 分 · 有评价 {reviewed:,} 单")
        return "\n".join(lines)

    rows = get_state_late_totals(con, month)
    if not rows:
        return f"{scope}没有可用的州级订单（no_data）。"
    if kind == SCOPE_RANK_ON_TIME:
        rows.sort(key=lambda row: (row[2] / row[1], -row[1], row[0]))
        lines = [f"{scope}按准时率排名第一的是 {rows[0][0]} 州。", "前 5 名"]
        for index, (state, valid, late) in enumerate(rows[:5], start=1):
            lines.append(f"{index}. {state} 州 · 准时 {(valid - late):,}/{valid:,} 单 · {(valid - late) / valid:.2%}")
        return "\n".join(lines)

    rank_by_rate = kind == SCOPE_RANK_RATE
    rows.sort(key=lambda row: (-(row[2] / row[1] if rank_by_rate else row[2]), row[0]))
    if rows[0][2] == 0:
        return f"{scope}有有效订单，晚到单数为 0。"

    metric = "晚到率" if rank_by_rate else "晚到单数"
    top_state = rows[0][0]
    lines = [f"{scope}按{metric}排名第一的是 {top_state} 州。", "前 5 名"]
    for index, (state, valid, late) in enumerate(rows[:5], start=1):
        lines.append(f"{index}. {state} · 晚到 {late:,} 单 / 有效 {valid:,} 单 · {late / valid:.2%}")
    return "\n".join(lines)


def _review_line(label, group):
    rate = group["low_score_rate"]
    if rate is None:
        return f"{label}有 {group['included_orders']:,} 笔有效订单，低分率无法计算。"
    return (
        f"{label}有 {group['included_orders']:,} 笔有效订单，"
        f"低分 {group['low_score_orders']:,}/{group['reviewed_orders']:,}，低分率 {rate:.2%}。"
    )


def _semantic_intent(question, client):
    """When configured, use the model only to choose a supported intent."""
    client = client if client is not None else _configured_client()
    if client is None:
        return None
    try:
        response = client.chat.completions.create(
            model=os.getenv("NUAA_AI_MODEL_ID", "kimi-k3"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        '为 Olist 履约分析选择一个意图。只返回 {"intent":"标签"}。'
                        "规则问答可选 same_day（同日更晚）、after_day（跨日晚到）、before_day（提前）、"
                        "date_rule（日期判定）、delivery_eligibility（纳入条件）、rate_formula（晚到率公式）、"
                        "order_month（下单月份归属）、rating_rule（评分口径）。"
                        "数据查询可选 overall_late（总体统计）、state_late_count（州级晚到单数排名）、"
                        "state_late_rate（州级晚到率排名）、state_on_time_rank（准时率排名）、"
                        "state_review_rank（各州评分排名）、state_review（指定州评分）、"
                        "compare_months（总体两月对比）、compare_states（州级增量对比）。"
                        "其他问题选 unsupported。只识别意图，不计算数字，不补写日期或州名。"
                    ),
                },
                {"role": "user", "content": question},
            ],
        )
        content = response.choices[0].message.content if response.choices else None
        payload = json.loads(content or "{}")
    except (OpenAIError, TypeError, ValueError):
        return None
    intent = payload.get("intent") if isinstance(payload, dict) else None
    allowed = {*RULE_ANSWERS, *SCOPED_KINDS, "compare_months", "compare_states"}
    return intent if isinstance(intent, str) and intent in allowed else None


def _rule_reply(rule, pending, context):
    if context is not None:
        context["reply_kind"] = "rule"
    return RULE_ANSWERS[rule], pending


def chat_loop(con, user_q, *, client=None, pending=None, context=None):
    if context is not None:
        context["reply_kind"] = None
    if EVAL_QUESTION.search(user_q):
        reply = (
            "1. 口径解释类。验证 Agent 能直接解释晚到日期、分子和分母，查询工具调用 0 次。\n"
            "2. 异常与边界类。验证缺少月份时先追问、非法月份提示纠正、超范围月份返回无数据。"
        )
        return (reply, pending)
    rule = classify_rule_question(user_q)
    if rule is not None:
        return _rule_reply(rule, pending, context)

    if re.search(r"数据范围|哪些月份有数据|数据覆盖", user_q):
        return f"Olist 的下单月份覆盖 {FIRST_MONTH} 至 2018-10。", pending

    previous_scope = pending if isinstance(pending, tuple) and pending[0] in SCOPED_KINDS else None
    scope_intent = _scoped_intent(user_q)
    if scope_intent is not None or (previous_scope is not None and not STATE_DIAG_QUESTION.search(user_q)):
        return _scoped_reply(con, user_q, scope_intent or previous_scope, context)
    if previous_scope is not None:
        pending = None

    state_compare = bool(STATE_DIAG_QUESTION.search(user_q))
    if pending is not None and state_compare:
        pending = PENDING_STATE
    # 返回 (reply_str, new_pending)
    if pending is not None:
        months, explicit_bases = _months_in_question(user_q)
        if any(not _valid_month(month) for month in months):
            return ("月份无效，请输入有效的 YYYY-MM，例如 2017-08。", pending)
        unique = list(dict.fromkeys(months))
        if len(unique) == 1 and len(months) >= 2:
            return ("基准月和对比月不能相同。", pending)
        if len(unique) != 2:
            return (ASK_FOR_MONTHS, pending)

        base_month, compare_month = unique
        if compare_month in explicit_bases:
            base_month, compare_month = compare_month, base_month
        expected = {"base_month": base_month, "compare_month": compare_month}

        if pending == PENDING_STATE:
            args = _confirmed_tool_arguments(
                client if client is not None else _configured_client(), user_q, expected, TOOL_STATE_NAME
            )
            reply = _render_state_result(_run_state_tool(con, args))
        else:
            args = _confirmed_tool_arguments(
                client if client is not None else _configured_client(), user_q, expected, TOOL_NAME
            )
            reply = _render_result(_run_delivery_tool(con, args))
        return (reply, None)

    months, explicit_bases = _months_in_question(user_q)
    if any(not _valid_month(month) for month in months):
        return "月份无效，请输入有效的 YYYY-MM，例如 2017-08。", None
    unique = list(dict.fromkeys(months))
    if len(unique) == 1 and len(months) >= 2:
        return "基准月和对比月不能相同。", None

    rest = MONTH_PATTERN.sub("", user_q)
    month_only = len(unique) == 2 and re.fullmatch(
        r"(?:[\s，、。？！?/,和与及到至~～—−-]|请|以|为基准|作为基准|比较|对比|月份)*", rest
    )
    compare_requested = state_compare or month_only or (
        LATE_TOPIC.search(user_q)
        and (len(unique) == 2 or re.search(r"最近|对比|比较|相比|变化|环比|基准|增量", user_q))
    )
    if not compare_requested:
        intent = _semantic_intent(user_q, client)
        if intent in RULE_ANSWERS:
            return _rule_reply(intent, None, context)
        if intent in SCOPED_KINDS:
            return _scoped_reply(con, user_q, (intent, None), context)
        if intent not in {"compare_months", "compare_states"}:
            if context is not None:
                context["reply_kind"] = "clarify"
            return (
                "你想查晚到数据，还是了解怎么判定晚到？可以点选下面的问题，也可以说明要查的指标和范围。",
                None,
            )
        state_compare = intent == "compare_states"

    if len(unique) != 2 or len(explicit_bases) > 1:
        return ASK_FOR_MONTHS, PENDING_STATE if state_compare else PENDING_LATE

    base_month, compare_month = unique
    if compare_month in explicit_bases:
        base_month, compare_month = compare_month, base_month
    expected = {"base_month": base_month, "compare_month": compare_month}

    if state_compare:
        args = _confirmed_tool_arguments(
            client if client is not None else _configured_client(), user_q, expected, TOOL_STATE_NAME
        )
        reply = _render_state_result(_run_state_tool(con, args))
    else:
        args = _confirmed_tool_arguments(
            client if client is not None else _configured_client(), user_q, expected, TOOL_NAME
        )
        reply = _render_result(_run_delivery_tool(con, args))
    return (reply, None)


def load_order_analysis(con):
    """加载客户和评价，并在现有 orders 上建立订单级分析视图。"""
    con.read_csv(str(customers_csv_path())).create_view('customers')
    con.read_csv(str(reviews_csv_path())).create_view('reviews')

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


if __name__ == "__main__":
    with duckdb.connect() as con:
        con.read_csv(str(orders_csv_path())).create_view("orders")
        load_order_analysis(con)

        pending = None
        context = {}
        while True:
            try:
                question = input(">> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if question.lower() in {"exit", "quit"}:
                break
            if question:
                resp, pending = chat_loop(con, question, pending=pending, context=context)
                print(resp)

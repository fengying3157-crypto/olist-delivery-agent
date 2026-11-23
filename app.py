from html import escape

import duckdb
import streamlit as st

from delivery_diagnosis import SCOPE_REVIEW, chat_loop, load_order_analysis, orders_csv_path


RANK_ALL = "全部数据里，哪个州晚到订单最多？"
RATE_ALL = "全部数据里，哪个州的晚到率最高？"
TOTAL_MONTH = "2017-08 的总体晚到率是多少？"
COMPARE_STATES = "以 2017 年 7 月为基准，对比 8 月，哪个州晚到增加最多？"
REVIEW_STATE = "RS 州 2017-08 的晚到组和按时组低分率是多少？"
DATE_RULE = "为什么要用 DATE 判断晚到？"
BEST_DELIVERY = "哪个州完成得最好？"
BEST_REVIEW = "哪个州的评分最好？"
SAME_DAY = "预计日期当天晚上才签收，算不算晚到？"
ELIGIBILITY = "哪些订单会纳入晚到率分母？"
ORDER_MONTH = "8 月下单、9 月签收的订单归到哪个月份？"


def suggested_questions(pending, last_answer, reply_kind):
    if reply_kind == "rule":
        return (SAME_DAY, ELIGIBILITY, ORDER_MONTH, TOTAL_MONTH)
    if reply_kind == "clarify":
        return (DATE_RULE, TOTAL_MONTH, RANK_ALL, BEST_REVIEW)
    if isinstance(pending, tuple):
        if pending == (SCOPE_REVIEW, None):
            return ("RS 州", "SP 州", "RJ 州")
        return ("全部数据", "2017-08", "2018-08")
    if pending:
        return ("以 2017 年 7 月为基准，对比 8 月", "以 2018 年 7 月为基准，对比 8 月")
    if "排名第一" in last_answer:
        alternative = RANK_ALL if "按晚到率排名" in last_answer else RATE_ALL
        if "按订单平均评分排名" in last_answer:
            return (BEST_DELIVERY, RANK_ALL, REVIEW_STATE)
        if "按准时率排名" in last_answer:
            return (BEST_REVIEW, RANK_ALL, COMPARE_STATES)
        return (BEST_DELIVERY, BEST_REVIEW, alternative, TOTAL_MONTH)
    if "低分率" in last_answer:
        return (RANK_ALL, TOTAL_MONTH, DATE_RULE)
    if last_answer:
        return (RANK_ALL, COMPARE_STATES, REVIEW_STATE, DATE_RULE)
    return (RANK_ALL, BEST_DELIVERY, BEST_REVIEW, COMPARE_STATES)


def select_question(question):
    st.session_state.suggested_question = question


st.set_page_config(page_title="Olist 履约诊断")
st.title("Olist 履约诊断")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "pending" not in st.session_state:
    st.session_state.pending = None

if "query_context" not in st.session_state:
    st.session_state.query_context = {}

if st.button("清空会话"):
    st.session_state.messages = []
    st.session_state.pending = None
    st.session_state.query_context = {}
    st.session_state.pop("suggested_question", None)

suggestion = st.session_state.pop("suggested_question", None)
question = st.chat_input("输入履约问题，或点选建议问题") or suggestion
if question and question.strip():
    question = question.strip()
    with duckdb.connect() as con:
        con.read_csv(str(orders_csv_path())).create_view("orders")
        load_order_analysis(con)
        reply, next_pending = chat_loop(
            con, question, pending=st.session_state.pending, context=st.session_state.query_context
        )

    st.session_state.pending = next_pending
    st.session_state.messages.extend([
        ("user", question),
        ("assistant", reply),
    ])

for role, content in st.session_state.messages:
    with st.chat_message(role):
        if role == "assistant" and content.startswith("|"):
            st.markdown(content)
        elif role == "assistant":
            st.html(f'<div translate="no" class="notranslate" style="white-space:pre-wrap">{escape(content)}</div>')
        else:
            st.text(content)

last_answer = st.session_state.messages[-1][1] if st.session_state.messages else ""
st.caption("你可能还想问" if last_answer else "可以试试")
examples = suggested_questions(
    st.session_state.pending, last_answer, st.session_state.query_context.get("reply_kind")
)
columns = st.columns(2)
for index, example in enumerate(examples):
    columns[index % 2].button(
        example, key=f"suggestion:{example}", on_click=select_question, args=(example,)
    )

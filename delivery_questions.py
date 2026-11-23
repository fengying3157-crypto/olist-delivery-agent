import re


RULE_ANSWERS = {
    "same_day": (
        "不算晚到。实际签收和预计送达是同一天，即使实际时间更晚，也按时送达。"
        "例如预计 8 月 15 日送到，实际当晚 20:00 签收，仍算按时。"
    ),
    "after_day": "算晚到。只要实际送达日期晚于预计送达日期，哪怕只跨过午夜几分钟，也计为晚到。",
    "before_day": "算按时。实际送达日期早于预计送达日期，属于提前送达。",
    "date_rule": (
        "预计送达按日承诺。直接比较时间戳会把预计日期的 00:00 当成截止时间，可能误判当天送达的订单。"
        "统一转换成 DATE 后，只有实际送达日期晚于预计送达日期才算晚到。"
        "按下单月份归组，分母只纳入已送达且两个日期都存在的订单。"
    ),
    "delivery_eligibility": (
        "晚到率分母只纳入已送达、且预计送达和实际送达日期都存在的订单。"
        "取消、未送达、缺任一日期的订单会被排除，晚到率分子只数有效订单中的晚到订单。"
    ),
    "rate_formula": (
        "晚到率 = 晚到订单数 ÷ 有效订单数。有效订单须已送达，而且预计和实际送达日期都存在。"
        "实际送达日期晚于预计送达日期才计入分子。分母为 0 时，晚到率无法计算。"
    ),
    "order_month": "订单按下单日期所在的月份分组。8 月下单、9 月签收的订单，归入 8 月统计。",
    "rating_rule": (
        "先对同一订单的多条评分取平均值，再按订单统计。"
        "低分订单的平均分不高于 2 分，低分率用低分订单数除以有评价的订单数。"
        "各州平均评分也先算每单均分，再在有评价的有效订单之间取平均。"
    ),
}

DELIVERY = re.compile(r"晚到|晚了|准时|按时|送达|签收|到货|到达|配送|交付|预计|承诺")
HOW = re.compile(r"为什么|为何|怎么|如何|算(?:不算|不|是|作)|算吗|算晚|算迟|算按时|是否|什么|标准|口径|规则|区别|纳入|计入|归到|归哪|归[哪几]|哪一?个月")
DATA_REQUEST = re.compile(r"多少(?:单|笔|个订单|份)|有几(?:单|笔)|排行|排名|列出|最多|最少|增量最大|评分最高")
SAME_DAY = re.compile(r"同一(?:个自然)?(?:天|日|日期)|同天|同日|当天|日期.{0,5}(?:一样|相同|一致)")
AFTER_DAY = re.compile(r"隔天|隔日|次日|第二天|后一[天日]|跨天|过了(?:午夜|零点)|晚了?[一两二1-9]+天|比.{0,12}晚[一两二1-9]+天")
BEFORE_DAY = re.compile(r"提前.{0,5}(?:天|送达|签收|到货)|早[一两二1-9]+天|日期早于")
MISSING_OR_STATUS = re.compile(r"缺失|为空|没有.{0,7}(?:时间|日期|签收|送到)|没填|未送达|未签收|还没送到|取消|退单")


def classify_rule_question(question):
    """把口径问题归到判定、纳入条件、时间归属或评价口径。"""
    text = re.sub(r"\s+", "", question)
    how = HOW.search(text)
    if re.search(r"低分率|低分|评分|评价", text) and how and re.search(r"定义|口径|怎么|如何|算|分母|几分|有多条", text):
        if not DATA_REQUEST.search(text):
            return "rating_rule"

    if not DELIVERY.search(text):
        return None
    if DATA_REQUEST.search(text):
        return None
    if re.search(r"下单|购买|创建", text) and re.search(r"月份|月统计|哪个月|归属", text) and how:
        return "order_month"
    if re.search(r"分母.{0,10}(?:哪些|包含|纳入|包括)|哪些订单.{0,10}(?:分母|纳入|计入)", text):
        return "delivery_eligibility"
    if MISSING_OR_STATUS.search(text) and (how or "分母" in text or "吗" in text):
        return "delivery_eligibility"
    if re.search(r"分母|分子", text) or re.search(r"晚到率.{0,8}(?:怎么算|怎么计算|如何计算|定义|公式)", text):
        return "rate_formula"
    if re.search(r"为什么|为何|解释|区别", text) and re.search(r"date\b|时间戳|按日期|只看日期|按天", question, re.IGNORECASE):
        return "date_rule"
    if how or "吗" in text or "？" in text or "?" in text:
        relations = [
            key for key, pattern in (("same_day", SAME_DAY), ("after_day", AFTER_DAY), ("before_day", BEFORE_DAY))
            if pattern.search(text)
        ]
        if len(relations) == 1:
            return relations[0]
        if relations or re.search(r"判(?:定|断)晚到|什么算晚到|什么叫晚到|怎样算晚到|晚到(?:定义|口径)|算不算晚到", text):
            return "date_rule"
    return None

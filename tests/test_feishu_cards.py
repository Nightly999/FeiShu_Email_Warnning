from app.feishu_cards import build_answer_card
from app.feishu import extract_card_text


def test_business_text_containing_exception_word_is_not_an_error() -> None:
    answer = (
        "基于上传文件的销售趋势分析\n\n"
        "总销售额：2,768,495.80\n"
        "可以继续找销售异常月份。"
    )

    card = build_answer_card("帮我分析今年销售趋势", answer)
    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == "查询结果"


def test_explicit_processing_failure_uses_error_card() -> None:
    answer = "文件处理失败，请稍后重试。"

    card = build_answer_card("分析文件", answer, status="error")
    assert card["header"]["template"] == "red"
    assert card["header"]["title"]["content"] == "处理失败"


def test_answer_words_do_not_override_explicit_status() -> None:
    card = build_answer_card(
        "统计失败率和异常月份",
        "失败率为 2%，异常月份为 2026-03。",
        status="success",
    )

    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == "查询结果"


def test_scheduled_task_card_supports_custom_title_and_footer() -> None:
    card = build_answer_card(
        "#7 每日样品风险｜查询样品风险",
        "| 样品单号 | 风险等级 |\n|---|---|\n| A001 | 高风险 |",
        title="定时任务执行结果",
        footer_label="任务",
    )

    assert card["header"]["title"]["content"] == "定时任务执行结果"
    assert any(
        element.get("tag") == "table" for element in card["body"]["elements"]
    )
    assert "任务：#7" in card["body"]["elements"][-1]["content"]


def test_card_limits_native_tables_and_preserves_final_business_table() -> None:
    sections = []
    for number in range(1, 7):
        sections.append(
            "\n".join(
                [
                    f"**表格 {number}**",
                    "| 项目 | 数量 |",
                    "|---|---|",
                    f"| table-{number} | {number} |",
                ]
            )
        )

    card = build_answer_card("查询并分页展示明细", "\n\n".join(sections))
    elements = card["body"]["elements"]
    tables = [element for element in elements if element.get("tag") == "table"]

    assert len(tables) == 5
    assert any(
        row.get("col_0") == "table-6" for table in tables for row in table["rows"]
    )
    assert any(
        element.get("tag") == "markdown" and "table-5" in element.get("content", "")
        for element in elements
    )


def test_plain_text_fallback_removes_card_markdown_markup() -> None:
    card = build_answer_card("查询风险", "**总体概览**\n\n- **风险等级**：高风险")

    fallback = extract_card_text(card)

    assert "**" not in fallback
    assert "<font" not in fallback
    assert "总体概览" in fallback

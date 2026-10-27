from __future__ import annotations

from nl2sql_agent.cli import main


def test_chat_repl_rewrites_resolves_and_exits(tmp_path, monkeypatch, capsys) -> None:
    lines = iter(
        [
            "2026年6月华东地区销售订单总金额是多少？",
            "那华南呢？",
            "那库存情况怎么样？",
            "1",
            ":history",
            ":exit",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))

    code = main(["chat", "--home", str(tmp_path), "--provider", "mock"])
    out = capsys.readouterr().out

    assert code == 0
    assert "resolved: 2026年6月华南地区销售订单总金额是多少？ | source: rule" in out
    assert "这个问题需要再确认一下" in out
    assert "选择问法：库存低于安全库存的物料有哪些？" in out
    assert "当前上下文" in out
    assert "会话结束：4 轮，其中 3 轮已回答" in out


def test_chat_repl_rejects_an_out_of_range_option(tmp_path, monkeypatch, capsys) -> None:
    lines = iter(["2026年6月华东地区销售订单总金额是多少？", "那库存情况怎么样？", "9", ":exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))

    code = main(["chat", "--home", str(tmp_path), "--provider", "mock"])
    out = capsys.readouterr().out

    assert code == 0
    assert "序号超出范围（1-3）" in out
    assert "会话结束：2 轮" in out


def test_chat_repl_offers_the_narrower_readings_after_an_answer(
    tmp_path, monkeypatch, capsys
) -> None:
    # The assume tier answers *and* hands back the narrower sentences; the numbered
    # pick has to work after an ok answer too, not only after a 反问.
    lines = iter(["销售额是多少？", "2", ":exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))

    code = main(["chat", "--home", str(tmp_path), "--provider", "mock"])
    out = capsys.readouterr().out

    assert code == 0
    assert "想收窄口径，可以直接这样再问一次：" in out
    assert "选择问法：上月（2026年6月）销售额是多少？" in out
    assert "会话结束：2 轮，其中 2 轮已回答" in out

# PATCH-NOTES-A5 — 分区外改动建议

代理 A5 的独占分区是 `app/institute/whiteboard.py`、`app/institute/daily.py` 及对应测试。
以下改动属于 Small bundle 但落在分区外文件，未直接修改，仅在此给出精确补丁。

## `compact_error` 应保留首行+末行（`app/router/executor.py`）

现状（`app/router/executor.py` 第 79-86 行）：超过 cap 时把**最后一行提到最前**，再接原文**截头**——
首尾信息虽都在，但顺序颠倒、阅读顺序割裂；且当最后一行本身接近/超过 cap 时（如单行 JSON 错误），
`cap - len(head) - 5` 变为负数，`text[:负数]` 得到"去掉尾部若干字符的全文"，拼接后再 `[:cap]`
一截——输出只剩最后一行的前段，首行反而完全丢失。

建议改为对称的头+尾截断（首行天然保留在头部、末行保留在尾部）：

```python
def compact_error(text: str, cap: int = 1000) -> str:
    """Keep the head and tail (first + last lines), cap total size."""
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    head_budget = max(1, cap * 3 // 5)
    tail_budget = max(1, cap - head_budget - 3)  # 3 = len("\n…\n")
    return f"{text[:head_budget].rstrip()}\n…\n{text[-tail_budget:].lstrip()}"[:cap]
```

要点：

- 头 3/5、尾 2/5 的预算分配：CLI 错误的开头通常是命令/错误类型，结尾是真正的报错行，两端都有固定预算，不再互相挤占；`max(1, …)` 防极小 cap 下预算为负（现有调用只用默认 cap=1000，属防御）。
- 不再依赖 `splitlines()` 找"最有信息量的一行"，单行超长文本（无换行的 JSON blob）也能正确得到首+尾。
- 调用点无需变化：`_finish(..., error=compact_error(...))` 三处（第 174、205、215 行）语义不变。
  注意第 215 行调用处是 `compact_error(output[-2000:] ...)`——外层已先截尾 2000 字符，
  与新实现叠加后语义仍正确（先取尾窗口再做首尾压缩），无需改动。

建议配套测试（`tests/test_executor.py`，同样不在 A5 分区）：

```python
def test_compact_error_keeps_first_and_last_lines():
    from app.router.executor import compact_error

    text = "FIRST line marker\n" + ("x" * 2000) + "\nLAST line marker"
    out = compact_error(text, cap=200)
    assert len(out) <= 200
    assert out.startswith("FIRST line marker")
    assert out.rstrip().endswith("LAST line marker")
    assert "…" in out

    # short text passes through untouched
    assert compact_error("short", cap=200) == "short"

    # a single overlong line still yields head + tail, no duplication
    single = "y" * 3000
    out2 = compact_error(single, cap=100)
    assert len(out2) <= 100
    assert "…" in out2
```

## 备忘：`POST /api/tasks/{id}/retry`

Small bundle 的第 4 项（tasks retry 端点）涉及 `app/api/`，完全在 A5 分区外，本次未实现、未起草——留给持有该分区的代理。

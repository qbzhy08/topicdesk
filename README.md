# 主题编辑台 TopicDesk

面向主题的信息收集与高价值内容创作 Agent。

## 启动

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium                # 动态页面采集需要（Lv1 起使用）
python run.py --port 8000                            # 打开 http://127.0.0.1:8000
```

`--auto-resume`：启动时自动从检查点恢复上次被中断的任务。

### 配置 LLM（可选，后续关卡使用）

任何 OpenAI 兼容接口均可：

```bash
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_API_KEY=sk-...
export LLM_MODEL=deepseek-chat
```

### 测试

```bash
python -m unittest discover -s tests -t .
```

## 进度

- [x] 第 1 步：数据模型 + Harness（追踪 / Checkpoint / 预算 / HITL / 回退重跑）+ 任务追踪页面
- [ ] 第 2 步：Lv1 采集（5 个信息源、ReAct 检索循环、去重归并、图片、降级）
- [ ] 第 3 步：Memory（主题长期记忆、用户偏好、查看/纠正/删除）
- [ ] 第 4 步：Lv2 价值评估与重排序
- [ ] 第 5 步：Lv3 Skill 化创作、配图、HITL 审阅、导出
- [ ] 第 6 步：Lv4 音频简报（加分）

## Harness 设计

```
app/harness/core.py    编排、追踪、checkpoint、预算、HITL
app/harness/runner.py  后台线程执行器，进程重启后的恢复
app/graphs/            编排图（每张图是一组节点函数）
```

- **节点**：`fn(ctx) -> 下一个节点名`。节点用 `ctx.think()` 写决策依据、`ctx.observe()` 写观察结果，
  前端时间线据此展示“已有结果怎样影响下一步”。
- **工具**：`@tool` 注册，`ctx.tool(name, **input)` 调用。每次调用记录输入、输出、耗时、成本并计入预算；
  同一任务内相同输入的成功结果会被缓存，崩溃后重跑节点不会重复抓取。
- **Checkpoint**：每个节点结束后保存 `state + usage + next_node`。节点异常、被中断或等待人工时，
  状态回滚到节点开始前的快照，避免“执行了一半”的修改被重复应用。
- **预算**：步数、工具调用次数、时长、成本。耗尽时跳到图声明的收尾节点（`on_budget`），
  输出带“结果可能不完整”标注的结果，而不是直接失败。
- **HITL**：节点调用 `ctx.human(key, ...)`，没有答复就暂停；答复写入后生成新 checkpoint（状态更新），
  从该节点继续。`rewind` 可回退到任意节点局部重跑，并指定哪些中间结果失效。
- **失败重试**：节点异常自动重试（默认 1 次）；仍失败则任务进入 failed，可手动“继续执行”。

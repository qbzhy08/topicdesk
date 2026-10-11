---
id: timeline
name: 脉络梳理
version: 1.0.0
summary: 按时间把事情怎么一步步走到今天讲清楚，标出转折点和原因
reader: 中途才关注到这个主题、想补上前因后果的读者；不需要专业背景
length: 1000-1500 字
material:
  list: "30"
  max_docs: 10
  prefer: [primary, update, new]
  facts: all
references:
  min_sources: 3
  min_primary: 1
  max_per_source: 3
  prefer: [primary, media]
  dated: true             # 每个节点都要有日期，日期必须来自资料
structure:
  - id: start
    heading: 起点
    goal: 这件事从哪里开始、当时要解决什么问题，带日期和出处
    words: 180
  - id: nodes
    heading: 关键节点
    goal: 用列表按时间排 4-7 个节点，每条“日期｜发生了什么｜为什么重要”，每条带出处；日期不确定的写“约”或不写，不要猜
    words: 450
  - id: turn
    heading: 转折在哪里
    goal: 指出 1-2 个改变走向的节点，说明前后发生了什么变化、依据是什么
    words: 250
  - id: now
    heading: 现在走到哪一步
    goal: 最新进展和各方当前状态，区分已发生的事实和计划/宣称
    words: 200
  - id: next
    heading: 接下来看什么
    goal: 以【分析】开头，给出 2-3 个值得关注的信号，以及它们为什么重要
    words: 180
style:
  - 时间顺序清楚，同一节点内先事实后意义
  - 日期统一写成“2025 年 3 月”或“2025-03-12”，不混用
  - 不用“随着……的发展”“在……的背景下”这类空泛过渡
  - 节点之间写清因果，不要只罗列
images:
  cover: 1
  body_min: 2
  prefer: [timeline, chart, product]
  placement: 时间线示意图放在“关键节点”之后；数据变化图放在“转折在哪里”
---
写作要求：

- 这是“前因后果”，不是新闻汇总：每个节点都要回答“它为什么让下一步成为可能”。
- 资料里时间互相矛盾的，以一手资料为准，并在节点里注明另一种说法。
- 没有资料支持的节点宁可不写，也不要用常识补全。

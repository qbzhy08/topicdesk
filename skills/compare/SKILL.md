---
id: compare
name: 观点交锋
version: 1.1.0
summary: 围绕争议，把正反双方的说法、证据和分歧点摆在一起
reader: 看到不同说法、想知道该信谁的读者
length: 1000-1500 字
material:
  list: "30"
  max_docs: 10
  prefer: [disputed, primary]
  facts: disputed_first
structure:
  - id: question
    heading: 争的是什么
    goal: 一句话说清争议问题，以及为什么它重要
    words: 150
  - id: side_a
    heading: 一方的说法
    goal: 支持方（或官方）的主张、给出的证据和数据，带出处
    words: 300
  - id: side_b
    heading: 另一方的说法
    goal: 质疑方的主张、复现或反例，带出处
    words: 300
  - id: diff
    heading: 分歧点对照
    goal: 用 Markdown 表格对照双方在关键维度（数据、测试条件、来源独立性）上的差异，每格带出处
    words: 250
  - id: verdict
    heading: 现在能下结论吗
    goal: 以【分析】开头，说明证据强弱、还缺什么证据、读者现在应该怎么看
    words: 250
style:
  - 对双方用同样的标准，不替任何一方辩护
  - 每个主张都写明出自谁
  - 没有反方资料时，如实说明“目前没有找到公开的反驳”，不要虚构对立观点
references:
  min_sources: 4
  min_primary: 1
  max_per_source: 2
  prefer: [primary, academic, community]
  dated: true
  both_sides: true        # 正反两方各至少一个独立来源；没有就如实说明
images:
  cover: 1
  body_min: 2
  prefer: [chart, table, scene]
  placement: 数据对比图放在分歧点对照附近
---
写作要求：

- 分歧点对照表至少 3 行，每行一个维度。
- 如果争议已经被裁决（记忆里有“争议已裁决”），在开头说明，并解释依据。

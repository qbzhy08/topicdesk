---
id: background
name: 背景补充
version: 1.0.0
summary: 先讲新进展，再补上理解它必需的、有据可查的背景知识
reader: 看到新闻却缺少背景、读不懂意义的普通读者；愿意花 8 分钟
length: 1200-1800 字
material:
  list: "30"
  max_docs: 12
  prefer: [primary, new]
  facts: verified_first
references:
  min_sources: 4
  min_primary: 2          # 背景知识优先引用论文、官方文档等一手资料
  max_per_source: 3
  prefer: [primary, academic, media]
  dated: true
  background_cited: true  # 背景段落也必须带出处，不能凭常识写
structure:
  - id: news
    heading: 新进展是什么
    goal: 两三句话说清这次发生了什么，带出处
    words: 180
  - id: background
    heading: 你需要知道的背景
    goal: 补充 2-3 条理解这条新闻必需的背景（以前的情况、行业现状、规则或技术原理），每条都要有资料出处，不能凭常识写
    words: 450
  - id: concept
    heading: 一个关键概念
    goal: 解释新闻里最影响理解的一个概念，用资料中的具体例子或数字说明
    words: 280
  - id: position
    heading: 放回背景里看
    goal: 对照背景说明这次进展新在哪里、改变了什么，哪些只是延续
    words: 280
  - id: limits
    heading: 还要打个问号的地方
    goal: 以【分析】开头，说明资料的局限、单一来源的说法、还没有答案的问题
    words: 180
style:
  - 背景只写和这条新闻直接相关的部分，不写百科
  - 每段第一句就说清这段要回答的问题
  - 术语第一次出现时用一句大白话解释
  - 背景里的数字、规则、时间同样必须带出处
images:
  cover: 1
  body_min: 2
  prefer: [diagram, chart, product]
  placement: 原理示意图放在“一个关键概念”；对比数据放在“放回背景里看”
---
写作要求：

- “你需要知道的背景”是本文的重点：读者读完应该能自己判断这条新闻的分量。
- 背景资料不足时，直接写“公开资料里没有找到……”，并说明缺的是什么。
- 不要把背景写成新闻的复述。

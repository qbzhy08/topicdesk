---
id: faq
name: 问答速查
version: 1.0.0
summary: 用读者最可能问的 6-8 个问题组织全文，每个回答都能单独看懂
reader: 第一次接触这个主题、带着具体问题来查的普通读者
length: 900-1300 字
material:
  list: "5"
  max_docs: 8
  prefer: [primary, new]
  facts: verified_first
references:
  min_sources: 3
  min_primary: 1
  max_per_source: 2
  prefer: [primary, media]
  dated: true
structure:
  - id: tldr
    heading: 一句话回答
    goal: 用一两句话回答“这件事到底是什么、和我有什么关系”，带出处
    words: 100
  - id: qa
    heading: 你可能想问的
    goal: 6-8 个问答，每个问题用加粗的一行写成读者的口吻（如“**这和以前有什么不同？**”），回答 2-4 句、带出处；先问基础问题，再问细节和影响
    words: 750
  - id: unknown
    heading: 目前还没有答案的问题
    goal: 列出 2-3 个资料里还回答不了的问题，说明为什么还不知道
    words: 150
  - id: take
    heading: 我们怎么看
    goal: 以【分析】开头，一段话给出判断和依据
    words: 120
style:
  - 问题用读者会问的大白话，不用术语提问
  - 每个回答第一句直接回答问题，再补充依据
  - 术语出现时顺手用半句话解释
images:
  cover: 1
  body_min: 1
  prefer: [diagram, product, chart]
  placement: 图放在和它最相关的那个问答之后
---
写作要求：

- 每个问答都要能单独看懂，不要写“如上所述”。
- 不确定的就说不确定，不要为了凑问题编答案。
